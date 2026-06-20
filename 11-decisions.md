# Part XI — Production Decision Framework

This is the part you bookmark. It's the field guide: when someone proposes a change in an RFC, run it past these checks.

## 11.1 The maturity model

A useful frame for "where are we?" — five levels. Most teams aim for 3; few need 5.

### Level 0: Chaotic

- Single dev cluster. No prod cluster. Manual everything.
- No monitoring, no backups, no CI/CD.
- Cost: low. Risk: high.

### Level 1: Basic production

- Managed Kubernetes (EKS/GKE/AKS).
- Helm or Kustomize for deployment.
- Some metrics, maybe Prometheus. Logs to stdout.
- No GitOps; manual promotion.
- Backups happen (maybe); never tested.

### Level 2: Operational

- GitOps in place (Argo or Flux).
- Observability stack (Prometheus + Grafana + Loki + Tempo, or Datadog).
- CI builds images, signs them, deploys to staging automatically.
- RBAC and NetworkPolicy enforced.
- PodSecurity Standards `restricted` in prod namespaces.
- PDBs set on critical services.
- Cost: moderate. Risk: moderate.

### Level 3: Reliable

- Multi-AZ HA, disaster recovery tested.
- Progressive delivery (canary with metrics-driven promotion).
- Chaos engineering in practice (game days).
- SLOs defined and tracked; error budgets enforced.
- Compliance posture: RBAC review, secret encryption, audit logging.
- On-call rotation, runbooks, blameless postmortems.

### Level 4: Resilient and cost-efficient

- Active-active multi-region or tested DR.
- Cluster API or similar declarative cluster lifecycle.
- Spot/preemptible, Karpenter or equivalent, VPA recommendations applied.
- Auto-scaling across all axes (HPA, CA, VPA, KEDA).
- Multi-cluster GitOps with per-cluster overrides.

### Level 5: Predictive

- Continuous profiling driving performance and cost.
- ML-driven autoscaling.
- Self-healing infrastructure (operator-driven).
- Fully automated compliance evidence collection.

**Most production teams should aim for Level 3.** Levels 4-5 are for mature platform teams with a real product to defend.

## 11.2 The RFC checklist

Before approving a production change, ask:

### Architecture
- [ ] What does the cluster topology look like? Multi-AZ? Multi-region?
- [ ] Does this fit existing patterns (Deployment + HPA + PDB + Service + Ingress)?
- [ ] Does this need a CRD / Operator? If yes, which?
- [ ] How does this interact with the CNI policy and NetworkPolicies?

### Workload
- [ ] Is it stateless? If stateful, what's the storage strategy?
- [ ] What are the resource requests and limits, and how were they chosen?
- [ ] Are health checks (startup, readiness, liveness) defined correctly?
- [ ] Is there a PDB? Are `maxSurge`/`maxUnavailable` set?
- [ ] What's the rollout strategy? Can we roll back?

### Scaling
- [ ] What metric drives scaling? CPU, custom, queue depth?
- [ ] What's the minimum replica count? Enough to survive one node failure?
- [ ] Does this interact with the cluster autoscaler / Karpenter?
- [ ] Is there a scale-down stabilisation window?

### Security
- [ ] What Pod Security Standard level? `restricted`?
- [ ] What ServiceAccount? Specific or default?
- [ ] Are Secrets mounted as files or env vars? Where do they come from?
- [ ] Are images signed? Scanned?
- [ ] Does the workload need privileged? Can it be avoided?
- [ ] What NetworkPolicies apply? Egress allowed?

### Observability
- [ ] What are the SLIs (rate, errors, duration, saturation)?
- [ ] Are alerts defined? Runbook linked?
- [ ] Are logs structured? Trace context propagated?
- [ ] What dashboards will this show up on?

### Reliability
- [ ] What's the SLO target?
- [ ] What's the rollback plan?
- [ ] What's the failure mode? (kill -9 the Pod? Network partition? Full zone loss?)
- [ ] Has chaos testing been done? Game day planned?
- [ ] Are dependencies monitored? What if the DB is slow?

### Cost
- [ ] What node type does this run on?
- [ ] Right-sized or oversized? VPA recommendations?
- [ ] Spot-eligible?
- [ ] What's the per-month cost?

### Day-2
- [ ] Upgrade path documented?
- [ ] Backup covered? Tested?
- [ ] Runbook for common failures?
- [ ] Who owns this on-call?

## 11.3 The "Should we even Kubernetes this?" decision tree

```
Do you have many services that share infra needs?
├── No → consider PaaS (Fly, Railway, Cloud Run, ECS Fargate)
└── Yes
    │
    Do you have a platform team willing to own the control plane?
    ├── No → use a managed Kubernetes (EKS/GKE/AKS), accept the premium
    └── Yes
        │
        Do you have heterogeneous workloads (services + batch + stateful)?
        ├── No → single-purpose cluster or simpler
        └── Yes
            │
            Do you need cluster-wide policy, mTLS, multi-tenancy?
            ├── No → single cluster, simple stack
            └── Yes → Kubernetes, multi-cluster if needed
```

## 11.4 The "managed vs self-managed" decision

| Factor | Managed (EKS/GKE/AKS) | Self-managed |
|--------|----------------------|---------------|
| Cost | Premium for control plane | Cheaper compute, more ops cost |
| Control | Version, networking somewhat constrained | Full control |
| Upgrades | One-click, version cadence | You drive, you decide |
| Security | Cloud patches control plane | You patch |
| Networking | Cloud-native (VPC, IAM) | BYO CNI, complex |
| Compliance | Cloud controls most | You're the auditor's target |
| Ops cost | Low | High |
| Vendor lock | Some | None |

**Default:** managed. Switch to self-managed only when you have a real, articulable reason (compliance, cost at scale, custom control plane).

## 11.5 Anti-patterns summary

A quick checklist of "things I see go wrong":

| Anti-pattern | Cost |
|--------------|------|
| No PDBs | Outages during routine maintenance |
| No priority classes | Critical apps evicted for dev |
| Liveness probe too aggressive | Cascading restarts |
| Memory limits without profiling | OOM kills during normal load |
| HPA on CPU only | Doesn't scale for actual demand |
| No NetworkPolicy | Lateral movement on compromise |
| Privileged containers | Container escape = node root |
| Default ServiceAccount | Token leak = cluster-wide access |
| Secrets as env vars | Visible in describe, crash dumps |
| Image pull policy `Always` + mutable tags | Unpredictable deploys |
| No etcd backups tested | Cluster down = unrecoverable |
| Self-managed cluster without ops team | 2am pages for control-plane issues |
| Hardcoded config in YAML | Per-env explosion, no override |
| No admission policy | Anyone can deploy anything |
| No SLO | Alerts without priority, alert fatigue |
| Logs to stdout without structure | Unsearchable |
| CronJob without `concurrencyPolicy: Forbid` | Overlapping runs |
| StatefulSet without operator | Manual failover, manual backups |
| Single cluster without DR | Region failure = company failure |
| DR plan not tested | First real failure = discovery moment |

## 11.6 The questions to ask before every production decision

These come up in design reviews, postmortems, and incident calls. Memorise them.

### For a new service
1. What's its SLO? (Don't answer "high availability" — give me a number.)
2. What's its blast radius? (How many users are affected if it's down?)
3. What's its failure mode? (What happens when this dies?)
4. Who owns it on-call?

### For a new dependency
1. What does it cost if it's slow?
2. What does it cost if it's wrong?
3. How do we know it's broken before users do?

### For an architectural change
1. What's the rollback story?
2. What's the failure mode? (Worst case?)
3. Have we tested the failure mode?
4. Does this need a game day?

### For an incident
1. What's the user impact right now? (Not "what's broken" — what do users see?)
2. What's the smallest mitigation?
3. Can we rollback?
4. What's the trigger? (What alert, what symptom?)

### For a cost decision
1. What's the per-month cost? Per-year? Per-user? Per-request?
2. Is this an optimisation or a tax?
3. Who pays? Who benefits?

## 11.7 The "production-ready" checklist

A service is production-ready when:

- [ ] Deployed via GitOps.
- [ ] Has resource requests and limits set.
- [ ] Has PDB.
- [ ] Has priority class.
- [ ] Has startup, readiness, liveness probes.
- [ ] Logs are structured JSON with trace_id.
- [ ] Exposes Prometheus metrics (RED + saturation).
- [ ] Alerts tied to SLO burn.
- [ ] Runbook exists.
- [ ] Image scanned and signed.
- [ ] ServiceAccount scoped.
- [ ] NetworkPolicy in place.
- [ ] Secrets from external store (Vault, cloud KMS).
- [ ] Health checked at least weekly (synthetic).
- [ ] Owner and on-call rotation defined.
- [ ] Capacity forecast exists.

## 11.8 Closing

Kubernetes is a substrate, not a product. It gives you a powerful set of primitives and an operational model. Whether it works for you depends entirely on what you build on top.

The architecture and design decisions in this reference aren't the only path. They're the path that survived production. When you deviate, document why; the next engineer on the team needs to know.

When something goes wrong — and it will — the answer is rarely "more Kubernetes." It's usually "fewer things, tested, observable, with humans who know what to do."

Good luck.