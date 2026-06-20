# Lab 03 — eBPF observability: see the kernel, not just the app

> **Topic:** Linux & Kernel Knowledge (eBPF, observability)
> **Goal:** See what the *kernel* is doing — not what apps report — by reading
> the eBPF programs, maps, and Hubble-derived metrics that Cilium and the
> kubelet have attached to it.

---

## Why this matters

When something is slow in Kubernetes, the app says "it was slow" and the
control plane says "everything is fine". The kernel is the only place that
hasn't lied to you yet. eBPF lets you instrument the kernel safely — without
loading kernel modules or recompiling — and Cilium is one of the heaviest
eBPF users in any production cluster.

This lab teaches you to read four eBPF observability surfaces:
1. **bpftool prog show** — the programs attached to kernel hooks
2. **bpftool map show** — the data those programs read/write
3. **Hubble observe** — per-flow L3/L4 visibility from a single CLI
4. **Hubble metrics (Prometheus)** — aggregate counters from the same eBPF datapath

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"

# (1) bpftool is on the host.
docker exec platform-lab-control-plane bpftool version | head -2

# (2) Cilium exposes its eBPF tools *inside* the agent container.
#     Run from inside, not the host.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  bpftool prog show 2>/dev/null | head -3

# (3) Hubble-metrics needs a port-forward from cilium-agent:9965.
#     We'll start it in step 2 below.
```

## Steps

### 1. The eBPF programs attached to this node's kernel

```bash
# Cilium's eBPF programs are visible ONLY inside the agent's mount
# namespace — `bpftool` on the host won't see them (kind runs on the
# container's own bpf FS, not the host's). Run bpftool from inside.
#
# If you're on the kind node itself, `bpftool` is at /usr/local/bin.
# When shelling through the agent, prefix with that path.
echo "=== host view (kubelet programs only) ==="
docker exec platform-lab-control-plane bpftool prog show 2>/dev/null | head -5
echo
echo "=== agent view (full Cilium datapath) ==="
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  /usr/local/bin/bpftool prog show 2>/dev/null | head -5

# All eBPF programs loaded, categorized by hook type.
# `cgroup_device`    = kubelet's device-CSI programs (allow/block per cgroup)
# `sched_cls`        = Cilium's TC (traffic control) classifier programs
# `cgroup_skb`       = Cilium's per-cgroup socket-buffer classifier
# `cgroup_sock_addr` = Cilium's connect() hook (policy + service LB)
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  /usr/local/bin/bpftool prog show 2>/dev/null \
  | awk '/^[0-9]+:/ {print $2}' | sort | uniq -c | sort -rn

echo "---total eBPF programs loaded on this node---"
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  /usr/local/bin/bpftool prog show 2>/dev/null | grep -c '^'
```

Healthy single-node platform-lab has ~200-250 programs (mostly cilium TC
classifiers and kubelet's cgroup_device programs).

```bash
# Pick one cilium TC program and dump its instructions.
# That literal output IS the bytecode the kernel is executing per packet.
PROG_ID=$(kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
            /usr/local/bin/bpftool prog show 2>/dev/null \
            | awk '/ sched_cls / {print $1}' | tr -d ':' | head -1)
echo "Dumping program $PROG_ID..."
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  /usr/local/bin/bpftool prog dump xlated id "$PROG_ID" 2>/dev/null | head -20
```

You'll see something like:
```
int tail_nodeport_nat_egress_ipv4(struct __sk_buff * ctx):
; int tail_nodeport_nat_egress_ipv4(struct __ctx_buff *ctx)
   0: (71) r6 = *(u8 *)(r1 +126)
   1: (54) w6 &= 1
```
That's actual eBPF bytecode the kernel JIT-compiled and is running for
every packet that hits this hook. No `node_modules`, no Python — just
registers and a small instruction set.

### 2. The eBPF maps that hold the datapath state

```bash
# Cilium's maps live inside the agent's mount namespace — go there.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  bpftool map show 2>/dev/null | wc -l
echo "total maps in agent namespace"

echo "---the maps that actually do Service load balancing---"
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  bpftool map show 2>/dev/null | grep -E 'cilium_(lb4|services|ipcache|policy)' | head -10
```

Look for `cilium_lb4_se` (the Service→Endpoints hash map) and `cilium_lb4`
(the actual load-balancing state). These replace the iptables NAT rules
that kube-proxy would have written.

### 3. Hubble flow observation (per-packet visibility)

```bash
kubectl --context kind-platform-lab create deployment web --image=nginx:1.27 --replicas=2
kubectl --context kind-platform-lab expose deployment web --port=80
kubectl --context kind-platform-lab wait --for=condition=Ready pod -l app=web --timeout=60s
SVC_IP=$(kubectl --context kind-platform-lab get svc web -o jsonpath='{.spec.clusterIP}')
echo "Service IP: $SVC_IP"

# Generate a request and watch the eBPF path it took.
kubectl --context kind-platform-lab exec deploy/web -- \
  curl -s -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" "http://${SVC_IP}"

# Hubble observe shows every L3/L4 event for that Service.
# The verdict column tells you which eBPF program handled the packet.
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  hubble observe --namespace default --pod web --last 8 --verdict DROPPED 2>/dev/null
echo "---"
kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  hubble observe --namespace default --pod web --last 8 2>/dev/null | tail -10
```

You should see verdicts like `FORWARDED`, `TRANSLATED`, `TRACED`, or
`DROPPED`. Each is a different eBPF datapath code path.

### 4. Hubble Prometheus metrics (aggregate counters from the same eBPF data)

```bash
# Start a port-forward to hubble-metrics. Background it.
# (Use `background: true` in the tool config, or:
#   kubectl port-forward -n kube-system ds/cilium 9965:9965 &
# )
```

The metrics are at `http://127.0.0.1:9965/metrics`. After traffic has
been generated, the interesting ones are:

```bash
curl -s http://127.0.0.1:9965/metrics | grep -E '^hubble_(drop|flows_processed|icmp|dns)_total' | head -20
```

Things to look for:
- `hubble_drop_total{reason="..."}` — kernel-level drops with named reason
  (e.g. `POLICY_DENIED`, `NO_CONFIGURATION`, `UNSUPPORTED_L3_PROTOCOL`)
- `hubble_flows_processed_total{protocol="TCP" verdict="FORWARDED"}` — total flows
- `hubble_dns_queries_total` — DNS-level visibility (every query the cluster makes)
- `hubble_icmp_total{type="..."}` — even ICMP you didn't know was happening

### 5. Drop a packet and watch the metric move

```bash
# Block egress from the default namespace at the Cilium policy level.
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: block-egress
  namespace: default
spec:
  endpointSelector: {}
  egressDeny:
    - toEntities: ["world"]
EOF

sleep 3
# Try to reach the Service from outside the cluster. It'll be denied.
kubectl --context kind-platform-lab exec deploy/web -- \
  curl -s --max-time 3 -o /dev/null -w "external HTTP: %{http_code}\n" \
  "http://1.1.1.1" || echo "blocked (good)"
sleep 2
echo "---hubble drops since the policy was applied---"
curl -s http://127.0.0.1:9965/metrics | grep -E 'hubble_drop_total.*POLICY_DENIED' | head -5
```

The `hubble_drop_total{reason="POLICY_DENIED"}` counter will increase by the
number of egress attempts the policy denied — visible to ops as a Prometheus
counter, root-caused to the exact `CiliumNetworkPolicy` that denied it.

## Restore

```bash
kubectl --context kind-platform-lab delete ciliumnetworkpolicy block-egress -n default --ignore-not-found
kubectl --context kind-platform-lab delete deploy web svc web --ignore-not-found
# Stop the port-forward when done.
pkill -f "port-forward ds/cilium" 2>/dev/null || true
```

## Postmortem prompts

Answer these in `postmortems/03-ebpf-observability.md`:

1. **What did the system actually do?**
   - How many eBPF programs are loaded? What types? Which ones would *not*
     exist without Cilium? (Hint: compare with a fresh kind cluster.)
2. **What would the on-call impact have been?**
   - You see `hubble_drop_total{reason="POLICY_DENIED"}` climbing. What is
     the user-facing symptom, and what's the next 3 commands to run?
3. **What's the production pattern?**
   - Why do production teams run eBPF-based observability instead of (or in
     addition to) Prometheus-app-metrics? Think about: kernel events the
     app *can't* see, overhead of sidecars vs in-kernel hooks, the
     differences between eBPF and ptrace.
4. **How would an AI agent fail here?**
   - An AI sees high `hubble_drop_total` and might recommend "remove the
     network policy." What it should do FIRST is determine: is the drop
     intentional (policy), a misconfig (no identity), or a bug
     (UNSUPPORTED_L3_PROTOCOL)?

## Stretch (optional)

If you have `bpftrace` installed (`apt install bpftrace` or via a static
binary), trace live syscall latency on a specific pod cgroup:

```bash
bpftrace -e '
  kprobe:do_sys_openat {
    @start[tid] = nsecs;
  }
  kretprobe:do_sys_openat /@start[tid]/ {
    @usecs = hist((nsecs - @start[tid]) / 1000);
    delete(@start[tid]);
  }
'
```

This gives you a per-syscall-call latency histogram, per-CPU, kernel-level,
zero-app-cooperation. Real production eBPF observability starts here.
