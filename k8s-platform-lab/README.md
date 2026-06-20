# k8s-platform-lab

Hands-on lab that exercises four infrastructure areas end-to-end on a single
local cluster:

- **Cloud-Native Depth** — Kubernetes internals, CRI, CNI (Cilium/eBPF)
- **Linux & Kernel Knowledge** — eBPF, bpftool, Hubble observability
- **Infrastructure Strategy** — multi-cluster cost & placement optimization
- **AI/ML Infrastructure** — GPU scheduling, taints/tolerations, gang scheduling

Single-node kind cluster (Cilium CNI replacing kube-proxy) so the whole
cluster fits in 2 vCPU / 15 GB RAM. Multi-cluster exercises live in
`scripts/cost-sim.py` standalone.

---

## Quick start

```bash
# 1. bring up cluster + Cilium + Hubble + lab namespaces (~60-90s)
bash scripts/up.sh

# 2. (optional) start a port-forward for the dashboard
kubectl --context kind-platform-lab -n kube-system port-forward ds/cilium 9965:9965 &

# 3. (optional) serve the dashboard locally
python3 scripts/dashboard-data.py
bash scripts/bpf-prog-count.sh
cd dashboards && python3 -m http.server 8000 --bind 127.0.0.1
# then open http://127.0.0.1:8000/cost.html

# 4. run a lab
cat labs/01-cri-deep-dive.md     # and follow the steps
# ...
cat labs/06-drain-chaos.md

# 5. tear down
bash scripts/down.sh
```

## Architecture

```
              ┌──────────────────────────────────────────────────────────────┐
              │                  kind single-node cluster                   │
              │                                                              │
              │  ┌────────────────────────────────────────────────────────┐  │
              │  │  control-plane (platform-lab-control-plane)            │  │
              │  │  zone=az-a, workload=gpu, gpu=true (taint)            │  │
              │  │                                                        │  │
              │  │  ┌──────────┐  ┌──────────┐  ┌────────────┐          │  │
              │  │  │ kubelet  │  │  Cilium  │  │  Hubble    │          │  │
              │  │  │ (1.30)   │  │  agent   │  │  relay     │          │  │
              │  │  │          │  │ (1.15.4) │  │            │          │  │
              │  │  │  crictl  │  │  232 eBPF│  │  metrics   │          │  │
              │  │  │  ↕       │  │  programs│  │  :9965     │          │  │
              │  │  │ containerd│  │          │  │            │          │  │
              │  │  │  ↕       │  │  90 sched_cls (Cilium TC)            │  │
              │  │  │  runc    │  │  14 cgroup_skb                      │  │
              │  │  └──────────┘  └──────────┘  └────────────┘          │  │
              │  └────────────────────────────────────────────────────────┘  │
              │                                                              │
              │  Namespaces:                                                  │
              │    default      — for ad-hoc pods                            │
              │    lab          — general experiments                         │
              │    observability — for eBPF/Hubble visualizations             │
              │    ai-workloads — for GPU training simulations               │
              │    envoy        — for ingress experiments                    │
              └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────┐         ┌──────────────────────────┐
  │  simulated cluster #1    │         │  simulated cluster #2    │
  │  on-prem-eu (eu-central-1)│         │  aws-us-east-1           │
  │  20× general.large        │         │  9999× general.large     │
  │  8×  general.xlarge       │         │  9999× general.xlarge    │
  │  2×  gpu.a100 ($3.10/hr)  │         │  9999× gpu.a100 ($6.20)  │
  │  $0.18 / general.large    │         │  $0.40 / general.large   │
  └──────────────────────────┘         └──────────────────────────┘
              ▲                                       ▲
              │         scripts/cost-sim.py
              │         (greedy multi-cluster optimizer)
              └───────────────────────────────────────┘
```

## Labs

| #  | Topic                                    | Lab                                    | Verified |
|----|------------------------------------------|----------------------------------------|----------|
| 01 | CRI: kubelet → containerd → runc         | [labs/01-cri-deep-dive.md](labs/01-cri-deep-dive.md)       | ✅ run end-to-end |
| 02 | CNI: Cilium replaces kube-proxy          | [labs/02-cni-cilium.md](labs/02-cni-cilium.md)             | ✅ run end-to-end |
| 03 | eBPF: bpftool, Hubble metrics, drops     | [labs/03-ebpf-observability.md](labs/03-ebpf-observability.md) | ✅ run end-to-end |
| 04 | Cost-sim: multi-cluster placement        | [labs/04-cost-placement.md](labs/04-cost-placement.md)     | ✅ 4 scenarios |
| 05 | GPU: taints, tolerations, nvidia.com/gpu | [labs/05-gpu-scheduling.md](labs/05-gpu-scheduling.md)     | ✅ run end-to-end |
| 06 | Chaos: PDB-blocked drain, evict          | [labs/06-drain-chaos.md](labs/06-drain-chaos.md)           | ✅ run end-to-end |

Each lab follows the same shape:
1. **Goal** — what you should be able to do after the lab
2. **Why this matters** — the production pain the lab teaches you to recognize
3. **Pre-flight** — confirm the cluster is in a known state
4. **Steps** — copy-paste-able commands with expected output
5. **What to observe** — a table mapping observation to the lesson it teaches
6. **Restore** — undo the lab's side effects
7. **Postmortem prompts** — the 4 questions every postmortem answers:
   - What did the system actually do?
   - What's the on-call impact?
   - What's the production pattern?
   - How would an AI agent fail here?

## Postmortems (filled-in examples)

- [postmortems/02-cni-cilium.md](postmortems/02-cni-cilium.md) — Cilium replaces kube-proxy
- [postmortems/06-drain-chaos.md](postmortems/06-drain-chaos.md) — PDB blocks a real drain

Use these as templates for the other four.

## Dashboard

`dashboards/cost.html` is a single self-contained page that:

- **Cost topology** (left): reads `dashboards/data.json` (refreshed by
  `scripts/dashboard-data.py`) and shows cluster costs, workload placement,
  and unschedulable workloads.
- **eBPF / Hubble metrics** (right): reads from
  `http://127.0.0.1:9965/metrics` (a `kubectl port-forward` to
  `cilium-agent:9965`) and shows the top `hubble_drop_total`,
  `hubble_flows_processed_total`, and DNS counters live.
- **eBPF program counts**: refreshed by `scripts/bpf-prog-count.sh` into
  `dashboards/bpf-prog-counts.json`. Shows the breakdown of eBPF program
  types currently attached to the kernel.

Bring-up:
```bash
python3 scripts/dashboard-data.py      # writes dashboards/data.json
bash scripts/bpf-prog-count.sh          # writes dashboards/bpf-prog-counts.json
kubectl --context kind-platform-lab -n kube-system port-forward ds/cilium 9965:9965 &
cd dashboards && python3 -m http.server 8000 --bind 127.0.0.1
# open http://127.0.0.1:8000/cost.html
```

For continuous refresh while studying:
```bash
watch -n 30 'python3 scripts/dashboard-data.py && bash scripts/bpf-prog-count.sh'
```

## Layout

```
k8s-platform-lab/
├── README.md                    ← this file
├── cluster.yaml                 ← kind single-node config
├── data/
│   └── cluster-pricing.json     ← on-prem-eu + aws-us-east-1 + 3 workloads
├── scripts/
│   ├── up.sh                    ← bring up cluster + Cilium + Hubble
│   ├── down.sh                  ← teardown
│   ├── cost-sim.py              ← region-aware multi-cluster optimizer
│   ├── dashboard-data.py        ← refresh dashboards/data.json
│   └── bpf-prog-count.sh        ← refresh dashboards/bpf-prog-counts.json
├── manifests/
│   └── namespaces.yaml          ← lab, observability, ai-workloads, envoy
├── labs/
│   ├── 01-cri-deep-dive.md
│   ├── 02-cni-cilium.md
│   ├── 03-ebpf-observability.md
│   ├── 04-cost-placement.md
│   ├── 05-gpu-scheduling.md
│   └── 06-drain-chaos.md
├── dashboards/
│   ├── cost.html                ← the live dashboard
│   ├── data.json                ← refreshed by dashboard-data.py
│   └── bpf-prog-counts.json     ← refreshed by bpf-prog-count.sh
└── postmortems/
    ├── 02-cni-cilium.md         ← filled-in
    └── 06-drain-chaos.md        ← filled-in
```

## Environment notes (this VM)

- **2 vCPU / 15 GB RAM** is enough for a single-node kind cluster but not
  for 2+ nodes. Multi-node labs are simulated via `cost-sim.py`.
- **No real GPU.** Lab 05 uses `nvidia.com/gpu: 1` resource requests as
  the *scheduling* mechanism; the scheduler rejects with
  `Insufficient nvidia.com/gpu` (verified), which is the real error in
  a GPU cluster.
- **`bpftool`** is at `/usr/local/bin/bpftool` *inside* the cilium-agent
  pod, not in the host's PATH. The labs use the full path.
- **Hubble metrics** endpoint is `cilium-agent:9965` (set by
  `hubble-metrics-server` in the cilium ConfigMap). Port-forward to
  `127.0.0.1:9965` to read it.
- **`kubectl run --rm`** doesn't work in non-attached mode. The labs
  use `kubectl exec deploy/X -- curl ...` instead.

## What's NOT in this lab

- **Envoy ingress** — the `envoy` namespace is set up but no controller
  is installed. Add `helm install envoy-gateway` if you want to extend
  the CNI lab with Gateway API exercises.
- **Cluster autoscaler / Karpenter** — would be the natural follow-up
  to the cost-sim labs. Karpenter's spot/ondemand/capacity-type
  selection is the real-world version of `cost-sim.py`.
- **CSI / StatefulSets** — out of scope for the four topic areas, but
  the `local-path-storage` provisioner is installed and could be
  exercised.

## Citation

If you find this lab useful, the patterns are documented in the upstream
docs these projects maintain:
- [kind](https://kind.sigs.k8s.io/) — local Kubernetes clusters
- [Cilium](https://docs.cilium.io/) — eBPF-based CNI / service mesh
- [Hubble](https://docs.cilium.io/en/stable/gettingstarted/hubble_intro/)
- [bpftool / libbpf](https://github.com/libbpf/libbpf)
