# Lab 02 — CNI: from kube-proxy to eBPF (Cilium)

> **Topic:** Cloud-Native Depth (Kubernetes internals, CNI, networking)
> **Goal:** Prove that Cilium has *replaced* kube-proxy in this cluster, and
> show what that means in the kernel: no iptables, no IPVS, just eBPF maps.

---

## Why this matters

Default Kubernetes networking uses `kube-proxy` in iptables mode: every Service
gets an iptables rule on every node, and the ruleset grows linearly with Service
count. In a 5,000-Service cluster, the iptables ruleset can take minutes to
update and dominate conntrack CPU.

Cilium replaces kube-proxy by using **eBPF programs** attached to kernel hooks.
Service lookup happens in a kernel eBPF map (O(1) hash lookup), not a chain of
iptables rules. The performance and observability gains are significant:
  - Service updates are immediate, not eventual
  - You can `hubble observe` every flow with a single CLI command
  - The eBPF data plane runs in the kernel, not in user space

This lab shows you what to *look for* in the kernel to confirm it's actually
happening (not just claimed in a Helm chart).

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl --context kind-platform-lab -n kube-system get ds cilium
# Use --brief to get the one-line summary, and 2>/dev/null to suppress the
# cilium-agent's harmless "too many open files" log line.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- cilium status --brief 2>/dev/null
# Hubble should report 'Ok' on the unix socket.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- hubble status 2>/dev/null
```

Note: the cilium CLI lives at `/usr/bin/cilium` *inside* the agent container.
Use `kubectl -n kube-system exec ds/cilium -- cilium ...` — the agent pod has
many init containers, so output will be prefixed with "Defaulted container
cilium-agent out of: ...". That is expected.

## Steps

### 1. Prove kube-proxy is *gone* in the kernel data plane

```bash
# These commands run inside the kind node, where the kernel sees the network.
# iptables NAT chain is the home of kube-proxy Service rules.
docker exec platform-lab-control-plane iptables -t nat -L PREROUTING -n 2>&1 | head -5
echo "---"
docker exec platform-lab-control-plane iptables -t nat -L KUBE-SERVICES -n 2>&1 | head -5
# If Cilium replaced kube-proxy, you'll see NO KUBE-SERVICES chain.
```

If you see "Chain KUBE-SERVICES (policy ACCEPT)" with rows, kube-proxy is still
running. If the chain is missing or empty, Cilium owns the data plane.

### 2. Look at the eBPF maps that replaced iptables

```bash
# eBPF maps live inside the cilium-agent's mount namespace.
# `docker exec` from the host will NOT see them (different pinned FS).
# Run bpftool from inside the agent.
docker exec platform-lab-control-plane ls /sys/fs/bpf/cilium 2>/dev/null | head -10
echo "---cilium eBPF maps (from inside agent)---"
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  bpftool map show 2>/dev/null | grep -E '(cilium|lb4|service)' | head -15
echo "---total cilium maps loaded---"
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  bpftool map show 2>/dev/null | grep -ci 'cilium\|lb4'
```

You're looking for maps named like `cilium_services_v2`, `cilium_lb4_se`,
`cilium_ipcache_v2` — these are the eBPF structures that replace iptables
service rules + conntrack. A healthy replacement has ~50+ cilium maps.

### 3. Create a real Service and watch traffic through eBPF

```bash
kubectl --context kind-platform-lab create deployment web --image=nginx:1.27 --replicas=2
kubectl --context kind-platform-lab expose deployment web --port=80
kubectl --context kind-platform-lab wait --for=condition=Ready pod -l app=web --timeout=60s
SVC_IP=$(kubectl --context kind-platform-lab get svc web -o jsonpath='{.spec.clusterIP}')
echo "Service IP: $SVC_IP"

# Confirm the Service is registered in the eBPF datapath (NOT just in etcd).
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  cilium service list 2>/dev/null | grep "$SVC_IP"
```

Now **observe every flow to that Service** in real time:

```bash
# Curl from one of the web pods to its own Service IP. (kubectl run --rm in
# non-attached mode errors out; exec from the deployment is simpler and
# exercises the same Service routing path.)
kubectl --context kind-platform-lab exec deploy/web -- \
  curl -s -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" "http://${SVC_IP}"
kubectl --context kind-platform-lab exec deploy/web -- \
  curl -s -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" "http://${SVC_IP}"

# Now read the flows Cilium's eBPF program captured for that Service IP.
# Filter to just the `web` namespace to avoid DNS health-probe noise.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  hubble observe --namespace default --pod web --last 10 2>/dev/null
```

You should see lines like:
```
Jun 19 23:09:55.861: default/web-xxx:51258 -> default/web-yyy:80 to-endpoint FORWARDED (TCP Flags: SYN)
Jun 19 23:09:55.861: default/web-xxx:51258 <- default/web-yyy:80 to-endpoint FORWARDED (TCP Flags: SYN, ACK)
```

That L4 SYN/ACK pair was *intercepted at the kernel hook* by Cilium's eBPF
program. The kernel never had to walk a 5000-line iptables chain. Note the
`pre-xlate-rev TRACED (TCP)` and `post-xlate-rev TRANSLATED (TCP)` lines
that show Cilium's bpf programs running before and after the NAT decision.

### 4. Service-to-pod load balancing: where did the request go?

```bash
# Make 6 requests, see which pod answered each one.
for i in 1 2 3 4 5 6; do
  kubectl --context kind-platform-lab exec deploy/web -- \
    sh -c "echo request from pod \$(hostname)" &
done
wait
# The X-Real-IP/Forwarded chain shows kube-proxy-style behavior is now coming
# from cilium-agent, which is the eBPF datapath.
kubectl --context kind-platform-lab logs -n kube-system ds/cilium --tail=20 2>&1 | grep -i 'service' | head -5
```

### 5. Verify CNI plugin identity (Cilium, not kindnet)

```bash
# The CNI binary on the node should be cilium, not kindnet/flannel/calico.
docker exec platform-lab-control-plane ls /opt/cni/bin/ | grep -E '(cilium|kindnet|flannel|calico)'
# And the CNI config on the node should reference cilium.
# (The file is `05-cilium.conflist`, not `10-...` — Helm picks a number
# based on load order. The `5` is well below 10 so it wins over a leftover
# kindnet config if one ever appears.)
docker exec platform-lab-control-plane cat /etc/cni/net.d/05-cilium.conflist 2>/dev/null
```

## What to observe

| Observation | Confirms |
|------------|----------|
| `iptables -t nat -L KUBE-SERVICES` is empty or missing | kube-proxy's iptables chain was never created — Cilium owns the data plane |
| `bpftool map show` shows `cilium_*` maps | eBPF is actually loaded and attached, not just promised |
| `hubble observe` shows SYN/ACK for the Service IP | eBPF is intercepting the kernel hook on this node |
| `/etc/cni/net.d/10-cilium.conflist` exists | kubelet is wired to Cilium as the CNI plugin |

## Restore

```bash
kubectl --context kind-platform-lab delete deploy web --ignore-not-found
kubectl --context kind-platform-lab delete svc web --ignore-not-found
```

## Postmortem prompts

Answer these in `postmortems/02-cni-cilium.md`:

1. **What did the system actually do?**
   - Did `iptables -L KUBE-SERVICES` show anything? If yes, the replacement
     isn't complete. Where would you investigate?
2. **What would the on-call impact have been?**
   - In a 1,000-Service cluster, what's the latency difference between
     iptables-rule-eval (linear scan) and eBPF-map-lookup (hash)?
     What's the user-facing symptom when iptables ruleset is bloated?
3. **What's the production pattern?**
   - When teams run Cilium in production, do they keep kube-proxy around as a
     fallback? What does `kubeProxyReplacement=true` mean in the Helm values
     vs `kubeProxyReplacement=partial`?
4. **How would an AI agent fail here?**
   - An AI looking at a "service unreachable" incident might `kubectl rollout
     restart deploy/web` without checking whether kube-proxy is healthy or
     whether the eBPF data plane is even loaded. What's the first signal
     you'd check?

## Stretch (optional)

Run a Service with `sessionAffinity: ClientIP` and watch Hubble: the eBPF
datapath honors it natively (one eBPF map lookup per packet), while
iptables-mode kube-proxy would need a whole different rule strategy.
