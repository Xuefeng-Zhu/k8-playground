# Part I — Foundations & Mental Models

Before you run anything in production, internalise what Kubernetes is, what it deliberately is *not*, and the contracts your workloads are signing up for.

## 1.1 What Kubernetes actually is

Kubernetes is a **declarative, eventually-consistent cluster manager** with:

- A **control plane** that observes the cluster's actual state and reconciles it toward a desired state.
- An **API server** as the single mutation surface — every read and write flows through it.
- A **scheduler** that assigns workloads to nodes based on constraints and resources.
- A **set of controllers** that each watch a slice of the API and drive state to match spec.
- A **CNI plugin** that wires networking; a **CSI plugin** that wires storage; a **CRI runtime** that runs containers.

That's it. Everything else (Ingress, Service mesh, autoscaling, GitOps) is built on top of these primitives by you, by projects, or by your cloud provider.

### The mental model to carry everywhere

```
   ┌────────────────────────────────────────────────┐
   │              Declarative spec (YAML)           │
   │  "I want 3 replicas of this image running"     │
   └─────────────────────┬──────────────────────────┘
                         │  submitted via API
                         ▼
   ┌────────────────────────────────────────────────┐
   │                kube-apiserver                  │
   │         (validation, authn, authz, audit)      │
   └─────────────────────┬──────────────────────────┘
                         │  persists to etcd
                         ▼
   ┌────────────────────────────────────────────────┐
   │              Controllers (the brains)          │
   │  ReplicaSet, Deployment, StatefulSet, ...      │
   │  watch spec vs reality → call API to converge  │
   └─────────────────────┬──────────────────────────┘
                         │
                         ▼
   ┌────────────────────────────────────────────────┐
   │                Nodes & kubelet                 │
   │   "apiserver said run this → I run it"         │
   └────────────────────────────────────────────────┘
```

**The most important fact:** the cluster is *eventually* consistent. There is no synchronous "deploy complete" event. There is only "spec submitted" and "spec observed" — and you choose how to interpret the gap.

## 1.2 What Kubernetes is *not*

Knowing the boundary saves you pain:

| It's NOT | What you'll trip over |
|----------|------------------------|
| A deployment system for VMs | Pods are cattle, not pets. Long-running stateful services need extra plumbing. |
| A platform | It's a substrate. The platform (builds, releases, secrets, observability, policy) is what you build on top. |
| Self-healing for everything | It restarts Pods. It does not retry in-flight requests, drain queues, or fix bad config. |
| A database | etcd is a database, but your workloads don't get transactions across Pods and the API server. |
| Secure by default | EKS/GKE/AKS apply some sane defaults. Self-managed clusters require explicit hardening. |
| A scheduler for batch jobs (out of the box) | `kube-scheduler` schedules Pods. Job queues, cron, DAGs need Volcano, Argo Workflows, Kueue, etc. |

## 1.3 The three contracts you sign when you adopt it

When you put a workload on Kubernetes, you are agreeing to:

1. **The Pod lifecycle contract.** Pods are mortal. IPs change. Disks attached via `emptyDir` vanish on restart. Files in the container's writable layer vanish with the container. If your app assumes a stable identity, stable IP, or stable disk, you must design around that.
2. **The control plane as single point of mutation.** You don't SSH to nodes and edit state. You don't write files to `/etc/kubernetes/`. You talk to the API server. Anything outside that discipline is invisible to the reconcilers and will be undone.
3. **Eventually consistent reconciliation.** "3 replicas" means "the controller will keep working to make 3 replicas exist." It does not mean "at this exact moment, exactly 3 are running." Treat observed-state reads (from `kubectl get` or the API) as a snapshot, not a fact.

If any of these contracts is unacceptable for a workload, you either need to:
- Change the workload (make it stateless, make it idempotent, make it retryable), or
- Wrap it in a higher-level controller (an Operator) that enforces your invariants.

## 1.4 The shape of a production cluster

```
   ┌─────────────────── CONTROL PLANE ───────────────────┐
   │                                                     │
   │  ┌──────────────┐    ┌──────────────┐    ┌───────┐  │
   │  │ kube-apiserver│    │ kube-scheduler│   │ cmgr  │  │
   │  └──────┬───────┘    └──────────────┘    └───────┘  │
   │         │                                            │
   │  ┌──────▼───────┐                                    │
   │  │    etcd      │   (quorum, Raft-consensus KV)     │
   │  └──────────────┘                                    │
   └─────────────────────────────────────────────────────┘
                          │
                ───────────┴───────────
                │                       │
   ┌────────────▼─────────┐   ┌─────────▼────────────┐
   │       NODE A         │   │       NODE B         │
   │ ┌──────┐ ┌────────┐  │   │ ┌──────┐ ┌────────┐  │
   │ │kubelet│ │kube-proxy│ │   │ │kubelet│ │kube-proxy│ │
   │ └───┬──┘ └────────┘  │   │ └───┬──┘ └────────┘  │
   │     │   container runtime│    │  container runtime│
   │ ┌───▼──────────────┐  │   │ ┌───▼──────────────┐  │
   │ │ Pod   Pod   Pod  │  │   │ │ Pod   Pod        │  │
   │ └──────────────────┘  │   │ └──────────────────┘  │
   └───────────────────────┘   └───────────────────────┘
                │                       │
                └───────────┬───────────┘
                            ▼
                   ┌────────────────┐
                   │  CNI overlay   │  (Pod-to-Pod network)
                   └────────────────┘
```

Every node runs:
- `kubelet` — registers node, runs Pods, reports status.
- `kube-proxy` — programs iptables/IPVS for `Service` VIPs.
- A container runtime (containerd, CRI-O) — actually runs containers.
- A CNI plugin — attaches Pods to the cluster network.

## 1.5 Workload objects — the core vocabulary

| Object | Lifetime | Use it for |
|--------|----------|------------|
| **Pod** | Ephemeral | One or more co-scheduled containers sharing network and volumes |
| **Deployment** | Long | Stateless services; manages a ReplicaSet, supports rolling updates |
| **StatefulSet** | Long | Stable identity, stable storage, ordered scaling (databases, queues, ZK) |
| **DaemonSet** | Node-bound | One Pod per node (logging agents, CNI, node-local caches) |
| **Job** | Finite | Run-to-completion batch work |
| **CronJob** | Scheduled | Time-triggered Jobs |
| **ReplicaSet** | Behind Deployment | Maintains N replicas (you usually don't write these directly) |

**Rule of thumb:** start with Deployment. Move to StatefulSet only when you need stable network IDs or per-replica storage. Reach for DaemonSet for node-level agents.

## 1.6 Service discovery and the IP story

Every Pod gets an IP. Pods are routable from each other across nodes via the CNI.

`Service` objects give you a stable virtual IP (ClusterIP) that load-balances across a set of Pods selected by labels. The kube-proxy on each node programs the data plane (iptables or IPVS) to make the ClusterIP work.

```
    client ──► Service ClusterIP:80 ──► kube-proxy
                                          │
                          ┌───────────────┼───────────────┐
                          ▼               ▼               ▼
                      Pod A           Pod B           Pod C
                    10.244.1.5      10.244.2.7      10.244.3.2
```

The Pod IPs are real, routable across the cluster. Service IPs are virtual — they only exist because every node's data plane knows about them.

## 1.7 Production failure modes (Part I scope)

- **Treating the cluster like a server.** SSHing to nodes, hand-editing config, running things in `docker run`. The control plane will eventually revert it, often silently.
- **Designing apps that need stable IPs or stable hostnames.** Use `Service` for clients and `StatefulSet` + headless Service for stable identities.
- **Assuming synchrony.** A "successful" `kubectl apply` means the spec was persisted. Not that Pods are running. Not that they are healthy.
- **Mixing concerns in one Pod.** A Pod is a unit of co-scheduling and shared fate. It is not a VM. Don't run unrelated processes in one container for convenience.

## 1.8 Decision table — when *not* to use Kubernetes

| Situation | Better alternative |
|-----------|---------------------|
| 3 long-running services, no need to scale | VMs or PaaS (Fly, Railway, ECS Fargate, Cloud Run) |
| Batch jobs that don't need cluster scheduling | Lambda, Cloud Functions, dedicated batch services |
| Latency-sensitive single binary | Bare metal / single VM |
| ML training that doesn't need scheduling | Dedicated GPU cluster (RunPod, Lambda Cloud) |

Kubernetes earns its operational cost when you have:
- Many services, multiple teams, frequent deploys.
- Heterogeneous workloads needing the same cluster (services + batch + stateful).
- A platform team willing to own the control plane, OR willingness to pay a managed service premium.

## 1.9 Further reading

- [Kubernetes Components](https://kubernetes.io/docs/concepts/overview/components/)
- [Kubernetes API Concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/)
- [The "Why Kubernetes" talk by Brendan Burns (KubeCon 2018)](https://www.youtube.com/watch?v=3KHuVyo5qXQ)
- [Kubernetes The Hard Way — Kelsey Hightower](https://github.com/kelseyhightower/kubernetes-the-hard-way) — read for understanding, not for ops