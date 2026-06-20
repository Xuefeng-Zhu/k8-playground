# Part X — Multi-Cluster & Federation

Most production setups eventually need more than one cluster. The question is *why*, and what pattern fits.

## 10.1 Why multiple clusters?

| Reason | Examples |
|--------|----------|
| **Region/zone isolation** | Reduce blast radius; data residency |
| **Compliance** | GDPR data stays in EU; FedRAMP workloads segregated |
| **Separation of concerns** | Prod vs staging; per-team clusters |
| **Cost** | Different node types, spot vs on-demand |
| **Burst capacity** | Primary region + overflow |
| **Tear-down flexibility** | Sandbox clusters spun up/down per branch |

If your reason is "HA," a single multi-AZ cluster usually suffices. **Multiple clusters add operational cost; have a real reason.**

## 10.2 The spectrum

```
   One cluster (multi-AZ) ──── Multi-cluster ──── Multi-cloud
                                                       │
                              Single control plane ◄───┘
                                  vs
                              Independent control planes
```

You can:
- Run **one control plane, multiple clusters' worth of nodes** (Cluster API, KubeFed).
- Run **multiple independent clusters**, each with its own control plane.
- Run **one logical cluster spanning data centers** (rare, complex).

The default in production: **multiple independent clusters**, each with its own control plane, GitOps pulling from the same repo or per-cluster repos.

## 10.3 Patterns

### Pattern 1: Independent clusters, same Git

Each cluster has its own Argo/Flux installation. Git repo has per-cluster directories.

```
infra/
├── clusters/
│   ├── prod-us-east-1/
│   │   └── apps.yaml (Argo ApplicationSet)
│   ├── prod-eu-west-1/
│   └── staging/
└── apps/
    └── ...
```

**Pros:** full isolation. Failure of one cluster doesn't touch others. Easy to tear down.
**Cons:** N × control plane cost; multiple etcds to back up.

### Pattern 2: Cluster API (CAPI)

Cluster API is the Kubernetes-native way to manage clusters: a `Cluster` CRD, a control-plane `KubeadmControlPlane`, a `MachineDeployment`. The management cluster creates and reconciles workload clusters.

```
Management cluster (your laptop, your prod)
    │
    ▼
CAPI controllers
    │
    ▼
Workload clusters (prod, staging, dev)
```

**Pros:** declarative cluster lifecycle; KubeVirt/vSphere/AWS providers; GitOps for cluster itself.
**Cons:** significant operational complexity; learning curve; some providers less mature.

### Pattern 3: Hub-and-spoke with Submariner or Skupper

Multiple clusters that need network connectivity at the application layer. Submariner (L3) and Skupper (L7) provide cross-cluster Pod-to-Pod networking.

**Use case:** multi-region active-active, services span clusters.

### Pattern 4: Multi-cluster service mesh

Istio and Linkerd can span multiple clusters as one mesh. Complicated but enables:
- Cross-cluster failover.
- Geographic traffic routing.
- Shared identity (single mTLS CA).

## 10.4 Multi-cluster service discovery

How do services in cluster A find services in cluster B?

- **ExternalDNS** — DNS records per cluster; service `foo.prod.svc.cluster.local` may have a public DNS A record.
- **Global load balancer** — cloud DNS-based routing with health checks (Route 53, Cloud DNS).
- **Service mesh with multi-cluster** — direct cluster-to-cluster.
- **Gateway API + multi-cluster** — emerging standard, still maturing.

## 10.5 Multi-cluster state

Databases don't run across clusters; you replicate them. Common patterns:

### Active-passive

```
Cluster A (primary DB) ◄──replication── Cluster B (standby DB)
       │                                       │
   Reads/Writes                            Reads only
```

Failover: B is promoted. DNS / LB repointed.

### Active-active

```
Cluster A (DB primary for user_id%2==0)  Cluster B (DB primary for user_id%2==1)
       │                                       │
   Writes from sharded users              Writes from sharded users
```

Harder. Conflict resolution needed. Use managed DBs (Spanner, CockroachDB, Aurora Global, Yugabyte) rather than rolling your own.

### Backup-based

For RPO-tolerant workloads: nightly snapshots → S3, restore in DR region.

## 10.6 Disaster recovery patterns

| Tier | RPO | RTO | Approach |
|------|-----|-----|----------|
| 0 | Hours-days | Hours-days | Backups + Velero + manual runbook |
| 1 | Minutes | < 1 hour | Async cross-region replication + runbook failover |
| 2 | Seconds | Seconds | Sync replication + automated failover |
| 3 | Zero | Zero | Active-active multi-region |

**Most companies don't need tier 3.** Tier 0 or 1 covers >90% of real-world needs. Tier 2-3 are for fintech, healthcare, or compliance-mandated workloads where downtime = breach.

## 10.7 Cluster lifecycle

### Cluster bootstrap

`kubeadm`, `kops`, `Cluster API`, or managed (EKS/GKE/AKS). The new normal is:

1. Define cluster as code (terraform, Cluster API, clusterawsadm).
2. Bootstrap via GitOps (Argo/Flux installed by bootstrap script, then takes over).
3. Day-2 managed by the same GitOps repo.

### Cluster deletion

Often forgotten but important:
- Delete CSI volumes (they cost money).
- Delete load balancers (also cost money).
- Decommission DNS records.
- Update monitoring (don't keep scraping a dead cluster).

### Cluster upgrades at scale

With 5+ clusters, manual upgrades are painful. Cluster API's KubeadmControlPlane handles control-plane upgrades automatically. Node rollouts go through MachineDeployments. GitOps the cluster manifests.

## 10.8 Cost considerations

**Per-cluster cost:** control plane instances, etcd storage, observability stack per cluster, secrets per cluster, RBAC complexity.

**Optimisation:**
- Shared observability stack (one Prometheus can scrape many clusters with federation).
- Shared secrets store (External Secrets Operator to a central Vault or KMS).
- Cluster consolidation: fewer, larger clusters beat many small ones, *until* blast radius / team separation outweighs.

## 10.9 Production failure modes (Part X scope)

- **Multi-cluster added without a real reason.** Pure cost without benefit.
- **Cross-cluster dependencies that create coupling.** "Cluster A fails → Cluster B fails." Now you have one bigger problem.
- **Different Kubernetes versions per cluster.** Drift in API support, tooling compat.
- **No cluster inventory.** Sprawl — forgotten clusters still running, still costing.
- **Inconsistent security posture.** Some clusters locked down, some wide open.
- **DNS as a single point of failure.** Cross-cluster discovery dies with DNS.

## 10.10 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| Single multi-AZ cluster | HA, simple | Compliance, data residency |
| Multi-cluster independent | Real isolation, easy ops | Few workloads, high coupling |
| Cluster API | Many clusters, want declarative lifecycle | Single cluster or two |
| Cross-cluster service mesh | Active-active, many services spanning | Simpler topologies |
| Active-passive DR | Cost-sensitive, RPO > minutes | Real-time failover needed |
| Active-active DR | Critical, compliance | High cost, complexity |
| Tier 0-1 DR | Default | Tier 2-3 specific cases |

## 10.11 Further reading

- [Cluster API](https://cluster-api.sigs.k8s.io/)
- [Submariner](https://submariner.io/)
- [Multi-cluster Sig](https://github.com/kubernetes-sigs/about-multicluster)
- [Istio Multi-cluster](https://istio.io/latest/docs/setup/install/multicluster/)
- [AWS EKS Multi-region patterns](https://aws.github.io/aws-eks-best-practices/karpenter/)