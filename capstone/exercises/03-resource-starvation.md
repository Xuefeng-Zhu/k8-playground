# Exercise 03 — Resource starvation via overcommit

**Linked lab:** `labs/03-cidr-exhaustion.md` (the symptom, not the cause)
**Linked chapter:** `03-networking.md` (CNIs / IPAM) + `04-workloads-scaling.md`

**Time:** ~15 min
**What you'll break:** the scheduler's ability to place new pods by
asking for more CPU than the cluster has. The interesting thing is
that the symptom (Pending pods, no pressure conditions) looks identical
to a network/IPAM exhaustion issue.

---

## Before

The capstone has every container with `resources.requests` set:

```bash
kubectl -n capstone-frontend get pod -l app=frontend \
  -o jsonpath='{.items[0].spec.containers[0].resources}'
# requests: cpu=50m, memory=64Mi; limits: cpu=200m, memory=128Mi

kubectl -n capstone-backend get pod -l app=backend \
  -o jsonpath='{.items[0].spec.containers[0].resources}'
# requests: cpu=100m, memory=128Mi; limits: cpu=500m, memory=256Mi

kubectl -n capstone-postgres get pod -l app=postgres \
  -o jsonpath='{.items[0].spec.containers[0].resources}'
# requests: cpu=250m, memory=256Mi; limits: cpu=1000m (no mem limit)
```

This is the resource budget of the capstone. Total requests:
- ~100m + 200m + 250m = **550m CPU**
- 128 + 256 + 256 = **640Mi memory**

The kind cluster has 2 CPUs and 16GB. Plenty of room.

## Break — request too much

```bash
kubectl -n capstone-backend apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: greedy
  namespace: capstone-backend
  labels:
    app: greedy
spec:
  replicas: 10
  selector:
    matchLabels:
      app: greedy
  template:
    metadata:
      labels:
        app: greedy
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
      - name: app
        image: nginxinc/nginx-unprivileged:1.27
        ports:
        - containerPort: 8080
        resources:
          requests:
            cpu: "1500m"     # 1.5 cores; we have 2 total
            memory: "4Gi"
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities:
            drop: ["ALL"]
EOF

sleep 10
kubectl -n capstone-backend get pods -l app=greedy -o wide
```

**Observe:** some pods will start, the rest will be `Pending`. The
counts depend on what's already scheduled, but expect 1-2 Running and
8-9 Pending.

## Observe — diagnose the failure

```bash
# 1. Standard conditions on the nodes: do any report pressure?
kubectl describe nodes | grep -E "MemoryPressure|DiskPressure|PIDPressure" | sort -u
# Expected: all "False" — no pressure. The cluster's metric system doesn't
# know that scheduling is failing for capacity reasons.

# 2. Scheduler events: what does it actually say?
kubectl -n capstone-backend get pods -l app=greedy -o jsonpath='{.items[0].metadata.name}' | \
  xargs -I {} kubectl -n capstone-backend describe pod {} | grep -A2 Events | head -20
# Expected: "FailedScheduling: 0/3 nodes are available: 2 insufficient cpu,
#           1 node(s) had taint {node.kubernetes.io/unschedulable...}"

# 3. Compare with the working capstone pods — they all started because
#    their requests fit in the cluster's capacity.
kubectl -n capstone-frontend get pods -l app=frontend
kubectl -n capstone-backend get pods -l app=backend
kubectl -n capstone-postgres get pods -l app=postgres
```

**The trap:** if you're an on-call engineer and you see "Pending pods"
without "node pressure = True", you might spend an hour debugging the
CNI, the image pull, the Service account, or any of a dozen other
things. The actual answer is in the **events**, not the **conditions**.
Always check events first.

## What this looks like for real IPAM exhaustion

Lab 03 reproduced this same symptom by exhausting pod CIDR addresses.
The scheduler can't place pods, you get Pending, no node pressure
condition fires (because node pressure is for memory/disk/PID only).
Same root symptom, very different root cause.

**This is the central confusion of on-call K8s debugging**: the
symptoms of capacity exhaustion, IPAM exhaustion, and scheduler bugs
all look like "Pending pods." The cure is the same: read the events.

## Recover

```bash
kubectl -n capstone-backend delete deployment greedy
# Wait a moment, then verify
sleep 10
kubectl -n capstone-backend get pods -l app=backend
```

## Postmortem prompt (`postmortems/03-capstone-resources.md`)

1. **How many pods got scheduled before the cluster ran out?**
   Calculate: 2 CPUs total - 550m already requested for capstone
   pods = 1450m available. Each greedy pod asks for 1500m. So
   **exactly 0 should have scheduled** if all capstone pods were
   running on one worker, or **1 if they were spread**.
2. **Why didn't the node pressure condition fire?** Connect this to
   what metrics are tracked (MemoryPressure = cgroup memory, not
   request capacity) vs what isn't tracked (request saturation).
3. **What alert would you write?** Specifically: alert on the
   difference between requested capacity and available capacity, not
   on standard node conditions.
4. **How is this connected to chapter 11 (capacity planning)?** The
   cluster's total capacity minus the sum of all requests minus some
   headroom is the **effective schedulable capacity**. If you don't
   track that, you'll get bitten by this in production.