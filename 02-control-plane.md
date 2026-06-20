# Part II — Control Plane Internals

The control plane is what makes Kubernetes *Kubernetes*. If Part I was about contracts, this part is about who's enforcing them.

## 2.1 Components

### `kube-apiserver`

The front door. Every `kubectl`, every controller reconciliation, every `kubelet` heartbeat flows through it.

Responsibilities:
- Validates, mutates, and authenticates requests.
- Persists objects to etcd.
- Serves the watch stream — controllers subscribe and react to changes.
- Exposes the OpenAPI spec used by CRDs and client-gen.

It is **stateless** (all state lives in etcd) and **horizontally scalable**. In production you typically run 2 or 3 instances behind a load balancer.

**Why it matters:** it is the chokepoint. Every pathological condition (slow controllers, runaway admission webhooks, debug loops calling the API) manifests here first. It is the only place where global rate limiting and audit logging happen.

### `etcd`

Distributed KV store using Raft consensus. Holds the cluster state. **The cluster's source of truth.**

Properties you must internalise:
- Strongly consistent. Reads are linearised across the quorum.
- Slow disk = slow etcd = slow cluster. Use SSDs; ideally dedicated NVMe with low fsync latency.
- Backing up etcd is backing up the cluster. The `etcdctl snapshot save` cadence determines your RPO.
- Writes go through Raft, so they cost network round-trips to a majority. Don't write huge objects.
- Default object size limit: **1.5 MiB**. Real hard limit set by `--max-request-bytes` (default 1 MiB request). Push big data to object storage, not Kubernetes objects.

**Production rule:** never co-locate workloads on control-plane nodes (unless your distro mandates it and you understand the consequences).

### `kube-scheduler`

Watches for Pods with no `nodeName` and assigns them. Two phases:
1. **Filtering** — drop nodes that can't run the Pod (insufficient resources, taints, affinity).
2. **Scoring** — rank the survivors; pick the highest score.

Scheduling is **not** retroactive. A Pod that becomes unschedulable after admission stays unschedulable until something changes (a node is added, a Pod is deleted, an event fires). This is why Cluster Autoscaler exists.

**Production gotcha:** the scheduler is single-instance by default and stateless. It's rarely the bottleneck, but the **scheduler queue** is a real production surface — many pending Pods plus slow filter plugins equals minutes to schedule.

### `kube-controller-manager`

A binary that runs dozens of controllers in one process: ReplicaSet, Deployment, StatefulSet, Job, NodeLifecycle, ServiceAccount, Token, EndpointSlice, GarbageCollection, etc.

Each controller is an independent reconciliation loop:
```
   watch spec ──► observe reality ──► compute diff ──► call API to converge
```

This is the "Kubernetes is a collection of control loops" claim made concrete.

**Production note:** you can split the controller-manager into `--cloud-controller-manager` for cloud-specific logic (node lifecycle on AWS/GCP/Azure, LoadBalancer Service provisioning). In managed Kubernetes this happens for you.

### `cloud-controller-manager` (optional split)

Cloud-specific controllers: node lifecycle against cloud provider, `Service` of type `LoadBalancer` provisioning, routes for cloud networks.

## 2.2 The control loop pattern

Every Kubernetes controller follows the same shape:

```go
for {
    desired := getSpec()
    actual := getStatus()
    if !reflect.DeepEqual(desired, actual) {
        patch := computePatch(desired, actual)
        client.Patch(patch)
    }
    sleep(someInterval)
    // OR: event-driven via informers + workqueue
}
```

The real implementations use **informers** (local caches with watch streams from the API), **workqueues** (with rate-limited retries), and **predicates** (filters to skip irrelevant events).

**Why this matters in production:**
- **Reconciliation loops can thrash.** If your controller has a bug that flips a field back and forth, you'll see API server load climb without visible user action.
- **Rate-limited retries on errors are not optional.** A controller that calls the API in a tight loop during a transient outage will get itself throttled, then ignored.
- **Status updates are eventually consistent.** `kubectl get pod` is reading a `Status` field that some controller (kubelet) wrote moments ago. It can lag.

## 2.3 The API server in detail

### Request lifecycle

```
kubectl/client ──► kube-apiserver
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   Authentication   Admission       Validation
   (authn)          (mutating +     (schema)
                    validating)
                        │
                        ▼
                  etcd write
                        │
                        ▼
              object available to watchers
```

### Authentication

Order matters; first to claim an identity wins. Configured via `--authentication-config` / flags. Common methods:
- X.509 client certs (the historical default for `kubectl`).
- Bearer tokens (ServiceAccount JWTs for in-cluster, OIDC for humans).
- Webhook token authenticators (delegate to an external IdP).

In production, you almost always wire OIDC for humans and ServiceAccount tokens for Pods.

### Authorisation

After authn, every request is checked against an authoriser. The modes are `Node`, `ABAC`, `RBAC`, `Webhook`. **RBAC is the default and the right answer.** ABAC exists for legacy. Webhook is for custom policy engines (OPA, Cerbos).

**Authoriser chain:** all configured authorisers are consulted. The first to deny wins. If all allow, request proceeds. Be careful with Webhook authorsers that timeout — a slow policy decision is a security hole.

### Admission control

Two phases:
1. **Mutating admission** — runs first. May modify the object. Examples: `MutatingAdmissionWebhook`, Istio sidecar injector, cert-manager.
2. **Validating admission** — runs second. May reject, may not modify. Examples: `ValidatingAdmissionWebhook`, PodSecurity admission, quota.

Admission is **synchronous**. Every request to the API server pays the latency of all configured admission chains. This is why every production incident post-mortem eventually mentions "slow admission webhook."

**Hard rules:**
- Admission webhooks must be HA. If the webhook is down, all Pod creation hangs.
- Webhooks must have a short timeout (default 10s, set yours to 3s).
- Never block on external IO in an admission webhook unless you can survive it being down.
- `failurePolicy: Fail` is the safe default. `Ignore` is for emergencies only.

### Audit

Every request can be logged to a structured audit log. In production: send this to your SIEM. Audit is the last line of defence when authorisation is misconfigured.

### API server scaling

The apiserver scales horizontally. Bottlenecks:
- **etcd write throughput.** Hard limit. Tune by object size and write rate.
- **Watch cache memory.** Every informer holds a cache. Lots of controllers with broad watches = lots of RAM.
- **Network bandwidth** between apiserver and etcd.
- **Admission latency.** Stack them up and every request gets slow.

Tuning flags you'll touch:
- `--max-requests-inflight` (default 400/200 — burst/sustained).
- `--max-mutating-requests-inflight` (default 200).
- `--request-timeout` (default 60s — be careful, callers wait this long).
- `--enable-priority-and-fairness` — **turn this on for production**. It isolates high-priority traffic (system components) from low-priority (your controllers, your users).

## 2.4 etcd deep-dive

### Quorum and durability

etcd uses Raft. A 3-node cluster tolerates 1 failure. A 5-node cluster tolerates 2. More nodes ≠ more durable; it just adds latency to writes.

**Rule:** use an odd number of nodes, sized to your RTO. 3 nodes is the production sweet spot. 5 is for control-plane-as-a-service with regional failure tolerance.

### Storage layout

etcd writes ahead to a WAL (`wal/`), then snapshots when the WAL grows. Backups:
- `etcdctl snapshot save /backup.db` — point-in-time snapshot. **This is your disaster recovery artefact.**
- Restore requires a fresh data dir; you cannot merge snapshots.

**Cadence in production:** snapshot every 15 minutes for typical clusters, more often for change-heavy clusters. Push to object storage. Encrypt at rest. Test the restore quarterly.

### Defragmentation and quotas

etcd has a backend DB (`db`) that grows on writes and shrinks only via `etcdctl defrag`. A long-lived cluster accumulates fragmentation. Schedule defrag during low-traffic windows.

**Hard quota:** `--quota-backend-bytes` (default 8 GiB). When you hit this, etcd returns `etcdserver: mvcc: database space exceeded`. The cluster is dead. Monitor `etcd_debugger_mvcc_db_total_size_in_bytes`.

### Network and disk

- Use dedicated SSDs with low fsync latency. etcd is fsync-heavy.
- Isolate etcd traffic on a private network. Cross-AZ etcd traffic adds latency.
- **Never** run other workloads on etcd nodes.

## 2.5 The scheduler in detail

### Scheduling framework

Since 1.19, the scheduler is a plugin framework with these phases:
1. **PreFilter** — early reject; can mutate Pod spec.
2. **Filter** — must-pass predicates (resources, node selector, taints, affinity).
3. **PostFilter** — runs if no node passed; preempts if configured.
4. **PreScore** — precomputes data for scoring.
5. **Score** — soft preferences.
6. **NormaliseScore** — flattens scores.
7. **Reserve** — placeholder on the chosen node.
8. **Permit** — final gate (e.g., volume binding).
9. **PreBind / Bind** — actually assigns the Pod.

**Plugins enabled by default (key ones):**
- `NodeResourcesFit` — CPU/memory fit.
- `NodeAffinity` — required/preferred node selectors.
- `TaintToleration` — taints/tolerations.
- `PodTopologySpread` — spread across zones/nodes.
- `VolumeBinding` — waits for PV.
- `NodeUnschedulable` — ignores unschedulable nodes.

### Scheduling extensions in production

- **Scheduler Extenders:** HTTP hooks for custom scoring. Legacy; replace with framework plugins.
- **Scheduler Profiles:** multiple schedulers in one binary, selected via `spec.schedulerName`. Useful for separating batch and latency-sensitive workloads.
- **Custom plugins:** Go-only, requires a forked scheduler. Heavy. Use only when no built-in works.
- **Volcano / Kueue:** for gang scheduling, queueing, fair-share, batch-aware. Add when you have meaningful batch workloads.

### Scheduling performance

With many pending Pods and tight constraints, scheduling can take seconds. Mitigations:
- Reduce hard constraints (prefer soft constraints).
- Pre-spread with `PodTopologySpread`.
- Avoid wildcards in selectors.
- For large clusters, run **multiple scheduler profiles** or a custom scheduler for hot workloads.

## 2.6 High availability patterns

### Control-plane HA

Production control plane: 3 apiserver instances behind an LB, 3 etcd members (can be co-located or separate), scheduler and controller-manager as 2 instances each with leader election.

Most managed services do this. If self-managed:
- Distribute apiserver instances across AZs.
- Keep etcd on dedicated nodes or VMs with provisioned IOPS.
- Use a load balancer with health checks on `/healthz`.

### Node failure

Kubernetes does **not** rebalance automatically when a node disappears. The kubelet stops heartbeating. The `node-controller` (in kube-controller-manager) marks the node `NotReady` after the threshold (default 40s — controlled by `--node-monitor-grace-period`). Then Pods are evicted (after `--pod-eviction-timeout`, default 5 minutes).

**For stateful workloads:** eviction is the right behavior for stateless. For databases, the application must handle losing a replica gracefully.

### Upgrade strategies

Control-plane upgrades (kubeadm, kOps, GKE):
- **In-place** (default): drain control plane, upgrade, restart. Brief unavailability per node.
- **Blue/green**: stand up new control plane, swap traffic. Done by managed services or by clever use of load balancer.

**Always upgrade one minor version at a time.** Kubernetes does not support skipping minor versions for the control plane.

## 2.7 Production failure modes (Part II scope)

- **etcd running on spinning disks.** Random write latency = random cluster-wide stalls.
- **Admission webhooks with no HA, slow responses, or tight timeouts.** They will be the cause of your next 2am page.
- **API Priority and Fairness disabled.** A misbehaving controller will starve critical system traffic.
- **Watches with broad selectors.** A single `watch * *` on the API can hold a watch cache of GB.
- **No etcd backups tested.** Restores that have never been tested don't restore.
- **Co-locating etcd and workloads.** IO contention kills the cluster.
- **Skipping minor versions.** You'll need to reinstall.
- **Single-instance scheduler/controller-manager with no leader election.** Crash → downtime.

## 2.8 Decision table

| Choice | When | When not |
|--------|------|----------|
| Managed control plane (EKS/GKE/AKS) | You don't have a dedicated platform team | You need k8s version pinned to a specific minor for compliance |
| Self-managed (kubeadm) | You want full control, you have a strong platform team | You only have 2 platform engineers |
| API Priority & Fairness on | Production clusters with mixed workloads | Single-tenant dev clusters |
| Default PodSecurity `baseline` | Production | Workloads that need privileged; use a `restricted` baseline + per-namespace exceptions |
| Defrag etcd nightly | Production control plane | Dev clusters (it adds latency) |

## 2.9 Further reading

- [kube-apiserver internals](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)
- [etcd operation guide](https://etcd.io/docs/v3.5/op-guide/)
- [Kubernetes Scheduler Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
- [API Priority and Fairness](https://kubernetes.io/docs/concepts/cluster-administration/flow-control/)
- [Auditing](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/)
- [Kelsey Hightower — etcd fundamentals (KubeCon)](https://www.youtube.com/results?search_query=kelsey+hightower+etcd)