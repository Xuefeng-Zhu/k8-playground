# Part IX — Reliability & Day-2 Operations

This is the part that determines whether your on-call is survivable. Day-2 covers upgrades, backups, disaster recovery, chaos testing, capacity planning, and the human practices.

## 9.1 Upgrades

### Cluster upgrades

**Cluster minor versions** are released ~3x/year. Support window is ~14 months (for the most recent minors).

**Production upgrade cadence:** every 3-6 months. Lateness compounds (you can't skip minors).

Pre-upgrade checklist:
1. Read release notes for every minor between current and target.
2. Run `pluto` (Fairwinds) and `kubectl-versions` to find deprecated APIs.
3. Read CNI, ingress, and operator upgrade notes (often breaking).
4. Upgrade control plane first (cloud-managed: click button; self-managed: kubeadm upgrade).
5. Upgrade node pools / node groups one at a time. Drain, upgrade, uncordon.
6. Verify after each step (smoke tests, dashboards, alerts).

### Kubernetes minor version support

| Version | Released | End of support (typical) |
|---------|----------|--------------------------|
| 1.29 | Dec 2023 | ~Feb 2025 |
| 1.30 | Apr 2024 | ~Jun 2025 |
| 1.31 | Aug 2024 | ~Oct 2025 |
| 1.32 | Dec 2024 | ~Feb 2026 |

(Check [Kubernetes releases](https://kubernetes.io/releases/) for current dates.)

### Node upgrades

Two patterns:
- **In-place:** drain → upgrade OS/kubelet → uncordon. Slow, brief capacity loss.
- **Replace:** add new nodes, drain old, remove. Faster, requires spare capacity.

Karpenter and Cluster Autoscaler handle node replacements cleanly. With kubeadm-style clusters, you do it manually with `kubectl drain --ignore-daemonsets --delete-emptydir-data`.

### Application upgrades

Helm/Kustomize + GitOps gives you atomic rollbacks. Always:
- Define a rollout strategy (RollingUpdate, Recreate, Blue/Green).
- Use `maxSurge`/`maxUnavailable` that respect capacity.
- Use `PodDisruptionBudget` to limit concurrent disruption.
- **Test in staging first.** Real prod is not where you find out.

## 9.2 Backups and disaster recovery

### What you back up

| Component | Why | Tool |
|-----------|-----|------|
| **etcd** | Source of truth for cluster state | `etcdctl snapshot` |
| **Persistent volumes** | Application data | CSI snapshots, Velero |
| **Git repos** | Config, manifests | GitHub/GitLab backup |
| **Secrets** | Encrypted secrets | Vault backup, cloud KMS |
| **Operator state** | Postgres operators etc. | Native (CloudNativePG PGBackrest) |

### Velero

The de facto Kubernetes backup tool. Backs up:
- Cluster resources (Deployments, Services, ConfigMaps, Secrets, CRDs).
- Persistent volume snapshots (via CSI).

```bash
velero backup create daily --include-namespaces prod
velero backup create full --all-namespaces --include-cluster-resources=true
```

Schedule daily; retain 7 daily + 4 weekly + 12 monthly.

**Velero gotchas:**
- Snapshots are per-CSI-driver; cross-cloud portability is limited.
- Restoring into a different cluster works; restoring into the same cluster with new namespaces works.
- **Test restores.** Quarterly at minimum.

### etcd backups

Critical. Run via:
```bash
ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-$(date +%F).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key
```

Restore:
```bash
etcdctl snapshot restore /backup/etcd-2024-01-15.db \
  --data-dir=/var/lib/etcd-restore
# Then update etcd.yaml static pod manifest to use --data-dir
```

**Production rules:**
- Snapshot every 15 minutes to object storage.
- Encrypt backups (KMS).
- Test restore in a sandboxed cluster quarterly.
- Document the restore procedure as a runbook.

### Disaster Recovery patterns

| RPO | RTO | Strategy |
|-----|-----|----------|
| 5 min | 1 hour | Hot standby cluster + async replication + Velero |
| 1 hour | 4 hours | Backup to object store + Velero + runbook restore |
| 24 hours | 24 hours | Daily backups + cold restore |

**DR is not the same as HA.** HA = fail within a region, no data loss. DR = fail across regions, may have data loss.

## 9.3 Capacity planning

### What to measure

- **CPU and memory usage** at node level (cluster utilisation ~70% before scaling).
- **Pod count** vs node capacity (room for one node's worth of failure).
- **Storage usage** trends.
- **Network bandwidth** (often forgotten; cheap to monitor, expensive to discover late).

### Forecasting

Use the last 90 days of metrics. Plot trend. If CPU is growing 10% month-over-month, you have a capacity problem in 6 months. Plan now.

### Burstable vs baseline

Most prod clusters should run at:
- **~65-75% cluster CPU utilised** (scaling room).
- **~70% cluster memory utilised** (OOM risk above this).
- **5-10% node headroom** for unscheduled Pods during node failure.

If you're at 90%+ CPU and the autoscaler hasn't scaled out, something's wrong (config, quotas, budget).

### Cost optimisation

- **Right-sizing:** use VPA recommendations or analyse p95 resource use.
- **Spot / preemptible nodes** for fault-tolerant workloads (batch, stateless).
- **Bin-packing:** tighter resource requests = better packing = fewer nodes.
- **Scale-to-zero** with KEDA for dev environments.
- **Cluster consolidation:** Karpenter (AWS) replaces node groups with cheaper mixes.
- **Compute savings plans / committed use:** commit for 1-3 years for big discounts.

## 9.4 Chaos engineering

### Why

You don't find out your system has no resilience until production breaks. Chaos engineering finds it earlier, in a controlled way.

### Tools

| Tool | Use |
|------|-----|
| **Chaos Mesh** | Pod kill, network partition, IO fault, time skew |
| **LitmusChaos** | Broader, with experiments and probes |
| **Gremlin** | SaaS, broad fault library |
| **AWS Fault Injection Service** | AWS-native, region/zone faults |
| **k6 / vegeta** | Load testing (chaos-adjacent) |

### Production chaos rules

1. **Start in non-prod.** Validate experiments work.
2. **Start small.** One Pod killed, not 100%.
3. **Have a blast radius cap.** Limit by namespace, label, time window.
4. **Stop conditions.** If SLO is burning, abort automatically.
5. **Hypothesis-driven.** "If we lose Pod X, latency stays under 200ms because Y." Don't just shoot things.
6. **Game days.** Schedule monthly chaos days with the team.

### Steady-state hypothesis

The cardinal rule: before injecting failure, define what "healthy" looks like and how you'd measure it. Inject. Verify deviation. Inject more.

## 9.5 Incident response

### On-call reality

Production Kubernetes incidents fall into:
- **Pod-level:** CrashLoopBackOff, OOMKilled, image pull errors. Diagnose with `kubectl describe pod`, logs, events.
- **Node-level:** NotReady, disk pressure, memory pressure. Drain if needed.
- **Cluster-level:** API server slow, etcd issues, controller failures. Page platform team.
- **Workload-level:** Service errors, latency, saturation. Standard debugging + Kubernetes context.
- **Cloud-level:** Cloud provider issues. Outside your control but visible in the cluster.

### Runbooks

Every alert needs a runbook. The runbook should:
- Link from the alert.
- Tell you what to check first.
- Tell you what to do.
- Tell you when to escalate.

```
Title: ApiServerHighErrorRate
Severity: page
On-call: platform-eng

## Symptoms
- 5xx rate > 5% for 10 minutes
- Customer reports of failures

## First checks
1. Grafana dashboard: api-server-overview
2. Recent deploys: argocd app history api-server
3. Upstream deps: db, cache, queue

## Mitigation
- Roll back: argocd app rollback api-server
- Scale up: kubectl scale deploy/api-server --replicas=20

## Escalation
- If rollback fails, page platform-lead
- If it's cloud-side, page cloud-eng
```

### Postmortems

Blameless postmortems. Focus on:
- **Timeline** (facts, not feelings).
- **Root cause** (not "human error" — that's a process gap).
- **Contributing factors** (what made it worse).
- **Action items** (specific, owned, dated).

Avoid:
- "We should communicate better" (not actionable).
- "Be more careful" (not actionable).
- "It was a perfect storm" (means you didn't model risk).

## 9.6 Maintenance windows

**Drain windows for upgrades:** schedule during low traffic. With PDBs set and decent traffic patterns, maintenance is invisible.

**Database migrations:** the trickiest. Plan for:
- Online schema migration (gh-ost, pgroll, expand-contract).
- Backwards-compatible changes only.
- Roll-forward over rollback (don't try to undo a schema change).

**Breaking-change upgrades:** deprecate the old API, support both versions, monitor usage of the old, switch over, remove.

## 9.7 Documentation and knowledge

- **Architecture diagrams** in your wiki. Hand-drawn, Mermaid, or proper diagrams. Updated yearly.
- **Runbooks** linked from alerts. Reviewed when the alert changes.
- **Decision records** (ADRs) for major choices (CNI, ingress, GitOps tool, etc.).
- **Postmortems library** searchable.
- **Onboarding guide** for new on-call engineers. Tested.

## 9.8 Production failure modes (Part IX scope)

- **No upgrade plan.** Stuck on a version that's out of support, security patches unavailable.
- **No etcd backup tested.** Cluster down → silent panic.
- **No chaos testing.** First real outage reveals N+1 was actually N+0.
- **Runbooks out of date.** Linked from alerts, missing the new field name.
- **Capacity plans in spreadsheets.** Not enforced; org grows faster than capacity.
- **No cost alerts.** Surprised by a 5x bill increase at the end of the month.
- **PDB unset.** Maintenance = outage.
- **No game days.** Team doesn't know how to operate the system during chaos.
- **Blameless postmortems, blameful culture.** Engineers stop sharing.

## 9.9 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| Upgrade every 3 months | Disciplined team, can keep up | Understaffed, falling behind already |
| Upgrade every 6 months | Default | — |
| Upgrade once a year | Only for very stable non-prod | Anything customer-facing |
| Hot standby DR | Critical workloads, low RPO/RTO | Cost-sensitive |
| Velero for backups | Default for prod | Cloud-native DBs handle their own |
| Chaos Mesh / LitmusChaos | Adopted chaos practice | Not adopted — don't install it |
| Game days monthly | Mature team | Quarterly chaos is the floor |

## 9.10 Further reading

- [Kubernetes Release Cycle](https://kubernetes.io/releases/)
- [Velero Documentation](https://velero.io/docs/)
- [etcd Disaster Recovery](https://etcd.io/docs/v3.5/op-guide/recovery/)
- [LitmusChaos](https://litmuschaos.io/)
- [Google SRE Book](https://sre.google/sre-book/table-of-contents/)
- [EKS Best Practices Guide](https://aws.github.io/aws-eks-best-practices/)