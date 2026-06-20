# Part IV — Workloads & Scaling

The objects that actually run your code. Where most operational decisions live.

## 4.1 Deployments and the rolling update contract

A Deployment manages a ReplicaSet, which manages Pods. Rolling updates replace ReplicaSets in waves:

```
v1 (Replicaset A, 5 replicas)
   ──►  v2 (Replicaset A scaled down 1, Replicaset B scaled up 1)
       ──►  ... continue until A=0, B=5
```

The Deployment controller enforces the **maxUnavailable** and **maxSurge** constraints.

```yaml
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%       # how many above desired we can run
      maxUnavailable: 25% # how many below desired we can go
```

**Production rule:** set `maxSurge` and `maxUnavailable` explicitly. Don't rely on defaults (25%/25%).

### Health checks: the contract for "ready"

```yaml
spec:
  containers:
    - name: app
      livenessProbe:
        httpGet: { path: /healthz, port: 8080 }
        initialDelaySeconds: 10
        periodSeconds: 10
        failureThreshold: 3
      readinessProbe:
        httpGet: { path: /ready, port: 8080 }
        periodSeconds: 5
        failureThreshold: 2
      startupProbe:
        httpGet: { path: /healthz, port: 8080 }
        failureThreshold: 30
        periodSeconds: 5
```

- **startupProbe** — disables liveness/readiness until startup succeeds. Critical for slow-starting apps. Without it, liveness kills your app on boot.
- **readinessProbe** — controls Service membership. Failing readiness = Pod removed from endpoints. **Most production outages that look like "service is dying" are readiness failures.**
- **livenessProbe** — restarts the container. Don't use it for "I'm overloaded" or "I'm having a bad day." A liveness probe should only fail when the process is unrecoverable. Otherwise you'll see cascading restarts during partial failure.

**Hard rule:** liveness ≠ readiness. Liveness restarts the container; readiness just stops traffic. If liveness fails during a transient backend outage, every Pod restarts in a sync — thundering herd.

### PodDisruptionBudget (PDB)

PDB caps how many Pods can be down *voluntarily* (drains, cluster autoscaler scale-down, node upgrades).

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api
spec:
  minAvailable: 2          # or "50%"
  selector:
    matchLabels:
      app: api
```

**If you don't set a PDB, the cluster will happily drain every Pod of your service at once.** This is one of the most common production outages from "innocent" node maintenance.

## 4.2 Resource management

### Requests and limits

```yaml
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

- **Requests** = what the scheduler reserves. Used for bin-packing.
- **Limits** = what the kernel enforces (cgroups).

**Production rule:** set both. For memory, **always set a limit** — without one, a leak takes down the node.

### Quality of Service classes

The kubelet assigns QoS based on requests/limits:

| QoS | When | Behavior on node pressure |
|-----|------|----------------------------|
| **Guaranteed** | requests == limits for both CPU and memory | Last to be killed |
| **Burstable** | requests set, limits > requests or one of them missing | Killed after BestEffort |
| **BestEffort** | No requests or limits | Killed first |

**BestEffort is dangerous in production.** A noisy-neighbor Pod can be killed to free memory for a Guaranteed Pod. Don't run production workloads BestEffort.

### CPU vs memory behavior

- **CPU** is compressible. A limit caps the rate but doesn't OOM-kill.
- **Memory** is incompressible. Hit the limit → kernel OOM-killer fires → Pod restarts.
- **Memory limits need headroom** for heap, GC, page cache. Profile your app.

### LimitRange and ResourceQuota

- **LimitRange** — defaults per container in a namespace; bounds what one Pod can ask for.
- **ResourceQuota** — caps total resources in a namespace.

Apply both at the namespace level. They prevent "someone asked for 64 CPUs in dev."

## 4.3 Horizontal Pod Autoscaler (HPA)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  minReplicas: 3
  maxReplicas: 50
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Pods
      pods:
        metric:
          name: requests_per_second
        target:
          type: AverageValue
          averageValue: "100"
```

**Production rules:**

- **HPA needs a metrics pipeline** (metrics-server for CPU/memory; Prometheus Adapter for custom metrics). No metrics = no autoscaling.
- **HPA doesn't scale from zero by default.** Use KEDA for that (cron, queue depth, Kafka lag).
- **Don't autoscale on raw CPU alone.** Pair with custom business metric (request rate, queue lag).
- **Set min > 1** for prod services. A single Pod with a node failure = zero replicas.
- **HPA decision latency is 15s default** (`--horizontal-pod-autoscaler-sync-period`). Plan for traffic spikes that exceed that.
- **Behaviour block** lets you configure scale-up/down windows. Use it to prevent flapping.

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 0
    policies:
      - type: Percent
        value: 100
        periodSeconds: 30
  scaleDown:
    stabilizationWindowSeconds: 300
    policies:
      - type: Percent
        value: 10
        periodSeconds: 60
```

## 4.4 Vertical Pod Autoscaler (VPA)

VPA watches historical usage and updates requests/limits.

- **Modes:** `Off` (recommendations only), `Initial` (set on Pod creation), `Auto` (live-update, which evicts Pods).
- **VPA in `Auto` mode conflicts with HPA on CPU/memory.** Pick one or scope HPA to custom metrics only.
- **Production usage:** run VPA in `Off` mode as a recommendation engine. Apply recommendations manually after review.

VPA is **not** a replacement for HPA. It's for workloads where the right resource size is unknown and HPA isn't suitable (stateful services, batch jobs).

## 4.5 Cluster Autoscaler vs Karpenter

Both add/remove nodes. They differ significantly.

### Cluster Autoscaler (CA)

- Per-Cloud-Provider implementation (AWS, GCP, Azure, etc.).
- Works on node groups (ASG, MIG, VMSS).
- Considers Pods pending scheduling.
- Conservative scaling; ~30s to react.
- Older, well-understood.
- Adds/removes whole node groups at a time.

### Karpenter

- AWS-first (now expanding).
- Selects instance types based on Pod requirements (CPU, memory, arch, GPU).
- Faster (15–30s to provision).
- Consolidates by replacing node groups with cheaper mixes.
- Better for spot, mixed instance types, fast scale.
- Doesn't work with all CNIs (matters for VPC-native setups; AWS VPC CNI is fine).

**Decision:**

| Situation | Pick |
|-----------|------|
| Multi-cloud | Cluster Autoscaler |
| AWS, fast scaling, cost-optimization | Karpenter |
| GPU workloads, mixed instance | Karpenter |
| Conservative, well-trodden path | Cluster Autoscaler |

## 4.6 StatefulSets

StatefulSets give you:
- **Stable, ordered network IDs** (`pod-0`, `pod-1`, ...).
- **Stable, per-Pod storage** (each gets its own PVC).
- **Ordered rolling updates** (default `OrderedReady`).

Use for:
- Databases (Postgres, MongoDB, MySQL).
- Queues (Kafka, RabbitMQ — usually with a custom operator).
- Zookeeper, etcd, Consul.
- Anything that can't tolerate peer swapping.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db          # headless service for stable DNS
  replicas: 3
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: postgres
          image: postgres:15
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: 100Gi
```

**Production gotchas:**
- **Ordered scaling is slow.** Adding a replica of a 3-replica StatefulSet waits for each step. Plan for this in DR.
- **No auto-rebalancing across nodes.** A StatefulSet Pod stays on its node.
- **Default update strategy is `OrderedReady`.** Consider `Parallel` if your app can handle concurrent restart.

For real databases, use a dedicated operator (CloudNativePG, Zalando, MongoDB Operator, Strimzi for Kafka). They handle the operational concerns (backups, failover, scaling, upgrades) that bare StatefulSets don't.

## 4.7 DaemonSets

One Pod per node. Used for:
- Logging agents (Fluent Bit, Vector).
- Node metrics exporters.
- CNI components.
- Storage daemons.

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluentbit
spec:
  selector:
    matchLabels:
      app: fluentbit
  template:
    metadata:
      labels:
        app: fluentbit
    spec:
      tolerations:
        - operator: Exists  # run on every node including masters
```

**Production rule:** tolerations are critical. Without them, your logging agent won't run on tainted nodes (and you'll have a blind spot exactly where you need logs).

## 4.8 Jobs and CronJobs

### Job

Run-to-completion. Use for one-off batch work.

```yaml
spec:
  completions: 10         # total successful runs needed
  parallelism: 3          # max concurrent
  backoffLimit: 4         # retries before failure
  activeDeadlineSeconds: 3600
  ttlSecondsAfterFinished: 86400  # GC after a day
```

**Production rules:**
- Set `ttlSecondsAfterFinished` — Job objects accumulate and clutter the API.
- Set `activeDeadlineSeconds` — stuck Jobs otherwise live forever.
- For "do this once" use Jobs. For "do this N times in parallel" use Jobs with parallelism. For "do this N times sequentially" use completion mode.

### CronJob

Schedule Jobs via cron syntax.

```yaml
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid   # Don't run if previous still running
  startingDeadlineSeconds: 200
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
```

**Production gotchas:**
- CronJobs miss their window if a node is down. `startingDeadlineSeconds` covers this.
- `concurrencyPolicy: Allow` can pile up overlapping runs. Use `Forbid` unless you specifically want overlap.
- For complex DAGs, use **Argo Workflows** or **Dagster** instead of nested CronJobs.

## 4.9 Init containers and sidecars

### Init containers

Run before the main container starts. Used for:
- Database migrations.
- Waiting for dependencies (`wait-for-it`).
- Fetching config/secrets.
- Setting up volumes.

They run sequentially; the Pod isn't "ready" until all init containers succeed. Failure → restart the Pod.

### Sidecars

Sidecar containers run alongside the main container in the same Pod.

Sidecars were historically a hack (Istio injected one). **As of 1.28, native sidecars are supported** (`spec.containers[*].restartPolicy: Always` for non-init containers).

**Use cases:**
- Log shippers (read stdout, forward to Loki/ES).
- Service mesh proxies.
- Metrics exporters.
- Dapr sidecars.

**Production gotcha:** sidecars that fail can kill the Pod. With native sidecars, you can configure them with `restartPolicy: Always` so they don't kill the main container.

## 4.10 Priority classes and preemption

Priority classes determine scheduling order and eviction order.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: production-critical
value: 1000000
globalDefault: false
description: "Critical prod services"
preemptionPolicy: PreemptLowerPriority
```

**Production rule:** use priority classes to protect critical workloads. Cluster Autoscaler and Karpenter honor them.

A `system-cluster-critical` priority is reserved for system components. `system-node-critical` is higher (kubelet, CNI). Anything else is application-tier.

## 4.11 Production failure modes (Part IV scope)

- **No PDB set.** Node maintenance = service outage.
- **Liveness probe too aggressive.** Cascading restarts during transient failures.
- **Memory limits too tight.** OOM kills during normal load spikes.
- **HPA on CPU only, no custom metric.** Doesn't scale for actual user load.
- **CA/Karpenter misconfigured.** Doesn't scale up during demand, doesn't scale down at night.
- **StatefulSet without an operator.** Manual scaling, manual failover, manual backups.
- **CronJob without `concurrencyPolicy: Forbid`.** Overlapping runs clobber state.
- **No startup probe on slow apps.** Liveness kills them on boot.
- **No priority class.** Production workloads evicted to make room for dev.

## 4.12 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| HPA + CA | Standard web services | Batch, scale-to-zero |
| HPA + Karpenter | AWS, fast scaling, cost | Non-AWS or need multi-cloud |
| VPA in `Off` mode | Need request/limit recommendations only | Don't run on critical workloads |
| VPA in `Auto` | Stateful services without HPA | Combining with HPA on CPU/memory |
| KEDA | Scale-to-zero, event-driven (Kafka, SQS, cron) | Plain HTTP services |
| StatefulSet + Operator | Real database/queue | Stateless service |
| Native sidecars | 1.28+, want lifecycle control | Older clusters |
| Priority classes | Multi-tenant, mixed criticality | Single-tenant dev |

## 4.13 Further reading

- [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [HPA Walkthrough](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale-walkthrough/)
- [Karpenter Docs](https://karpenter.sh/)
- [PodDisruptionBudget](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [KEDA](https://keda.sh/)