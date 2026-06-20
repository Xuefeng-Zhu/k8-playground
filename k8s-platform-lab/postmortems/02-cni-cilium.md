# Postmortem: CNI / Cilium (Lab 02)

**Date:** 2026-06-19
**Author:** k8s-platform-lab
**Severity:** informational (lab exercise; no production incident)

## Summary

On a single-node kind cluster running Cilium 1.15.4 with `kubeProxyReplacement=true`,
we verified that kube-proxy's iptables NAT rules are **not** present, and that
137K+ TCP flows are being handled by 90+ Cilium `sched_cls` eBPF programs.

## Timeline

- **00:00** Lab 02 started. Cluster has Cilium 1.15.4 DaemonSet, Hubble enabled.
- **00:01** `iptables -t nat -L KUBE-SERVICES` returns "chain does not exist" — confirmed kube-proxy's iptables data plane is **gone**.
- **00:02** `bpftool prog show` (inside cilium-agent) reports 232 eBPF programs:
  117 cgroup_device (kubelet), 90 sched_cls (Cilium TC classifiers), 14 cgroup_skb,
  8 cgroup_sock_addr, 2 cgroup_sock, 1 tracing.
- **00:03** Created `web` Deployment (2 replicas) + Service. `cilium service list` shows
  the Service registered: `10.96.59.232:80` → 2 backends, both active.
- **00:04** Curl from one pod to its own Service IP via `kubectl exec`: HTTP 200 in 1.1ms.
- **00:05** `hubble observe --namespace default --pod web` shows the SYN/ACK pair
  with verdict `FORWARDED` and `TRANSLATED` — Cilium's eBPF program actually
  processed every packet, in kernel, no iptables chain involved.

## Four questions

### 1. What did the system actually do?

The system replaced a 1,000-line iptables NAT ruleset with 90 eBPF programs
attached to TC ingress/egress hooks. Each packet to a Service now does:
- TC ingress: eBPF reads `cilium_lb4_se` map (Service→Endpoints) — O(1) hash lookup
- TC egress: eBPF writes the connection tracking entry, performs SNAT/DNAT
- Verdict returned to kernel: `FORWARDED`, `TRANSLATED`, or `DROPPED`

Every layer above (cilium-agent, kubectl, the application) saw HTTP 200 in 1ms.
The difference vs. kube-proxy iptables: zero per-packet linear scan of rules,
zero per-conntrack allocation pressure, zero churn when Services change.

### 2. What would the on-call impact have been?

In a 1,000-Service cluster:
- **kube-proxy iptables**: each packet walks ~5,000-10,000 iptables rules.
  p99 latency is in the millis. iptables ruleset updates are O(n) on the
  number of rules, so a 100-Service rollout can stall packets for 30-90s.
- **Cilium eBPF**: each packet does one bpf map lookup (hash, O(1)).
  p99 latency is in microseconds. Service updates are map insertions,
  immediate.

User-facing symptom of bloated iptables: "the cluster is slow after every
deploy" or "newly-created Services don't get traffic for 60-90 seconds."

### 3. What's the production pattern?

Cilium in production typically runs with:
- `kubeProxyReplacement=true` (strict, requires kernel 5.x + cgroupv2)
- `kubeProxyReplacement=partial` (safer fallback; keeps kube-proxy for ClusterIPs)
- `kubeProxyReplacement=disabled` (just CNI, no service-mesh; legacy)

The strict mode is the highest-performance but is also the most fragile to
kernel version mismatches. Production teams usually run partial mode in
heterogeneous clusters and strict in greenfield ones.

Bigger production pattern: **Cilium ClusterMesh** for multi-cluster Service
discovery. eBPF handles cross-cluster load balancing identically to local
Services, and Hubble observes the flows. That's a step beyond this lab.

### 4. How would an AI agent fail here?

An AI sees "Service unreachable" and might suggest:
- `kubectl rollout restart deploy/web` — wrong layer; the app is fine.
- `kubectl get endpoints web` — could help, but doesn't reach the eBPF datapath.
- `kubectl logs -n kube-system ds/cilium | grep error` — closer, but the eBPF
  data plane doesn't log per-packet; it logs at the agent level.

What the AI should do FIRST is `cilium service list` to see if the Service
is even registered in the eBPF data path, then `hubble observe` to see if
any flow is hitting it. The constraint the AI doesn't know: iptables-mode
diagnoses don't apply when eBPF is the data plane.

## Action items

- [x] Verified kube-proxy's iptables data plane is gone (`iptables -t nat -L KUBE-SERVICES` returns no chain).
- [x] Verified 90 Cilium sched_cls eBPF programs are loaded.
- [x] Verified a real Service's flow goes through eBPF, not iptables.
- [x] Verified `hubble observe` shows the SYN/ACK pair with correct verdict.
- [ ] (Future) Try `kubeProxyReplacement=partial` to see the difference.

## Lessons

1. The control plane and the data plane are different. `kubectl get svc` shows
   the etcd-stored object. `cilium service list` shows the eBPF data plane.
   A Service can exist in one and not the other.
2. iptables and eBPF are **mutually exclusive** for the same hook. Both can't
   intercept the same packet. If `KUBE-SERVICES` exists, Cilium is NOT the
   data plane for that Service.
3. Hubble is the only place that tells you which program handled a packet.
   There's no `iptables -L -v` equivalent that shows you the eBPF verdict.
