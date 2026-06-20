# Exercise 04 — StatefulSet with a broken StorageClass

**Linked lab:** `labs/04-statefulset-zone-loss.md`
**Linked chapter:** `05-storage.md` (volumeBindingMode + topology)

**Time:** ~20 min
**What you'll break:** the postgres workload by deploying a new
StatefulSet that uses a `volumeBindingMode: Immediate` StorageClass.
The new DB will hang in Pending, and the lesson is what you can/can't
do to recover without losing data.

---

## Before

The capstone postgres is up using the cluster's default `standard`
StorageClass, which is `WaitForFirstConsumer`:

```bash
kubectl get sc standard -o jsonpath='{.volumeBindingMode}'
# WaitForFirstConsumer

kubectl -n capstone-postgres get pod postgres-0 -o wide
# Running on one of the workers, with PVC bound

kubectl -n capstone-postgres get pvc data-postgres-0
# Bound, in use
```

This is the **safe** configuration. If the worker postgres is on
dies, the pod reschedules to a different worker and the PVC follows
it, because the binding waited until the pod was scheduled and could
honor the topology.

## Break — deploy a wrong-SC StatefulSet

```bash
kubectl apply -f broken-manifests/wrong-sc.yaml

# Wait a bit
sleep 15
kubectl -n capstone-broken-sc get pods,pvc -o wide
```

**Observe:**

```bash
# The new StatefulSet's pod is Pending forever:
kubectl -n capstone-broken-sc describe pod db-0 | grep -A5 Events
# Expected: events related to PVC binding failures (local-path provisioner
# doesn't actually support Immediate binding, so the PVC stays Pending)

# The PVC is also Pending:
kubectl -n capstone-broken-sc get pvc
# NAME          STATUS    VOLUME   CAPACITY   ...
# data-db-0     Pending                                      ...

# The new SC exists with the wrong binding mode:
kubectl get sc broken-immediate -o jsonpath='{.volumeBindingMode}'
# Immediate
```

**Why this matters:** if this were a production cloud cluster (EBS,
Persistent Disk, etc.), the PVC WOULD eventually bind (cloud CSI
drivers do support Immediate). The pod would then be stuck Pending
because:
1. PVC is bound to a specific zone
2. Pod has a topology constraint that can't find a node in that zone
3. Scheduler can't reconcile — the topology constraint and the SC's
   binding mode are in conflict

In kind with local-path, we don't even get that far — local-path's
provisioner refuses to bind with Immediate.

## Observe — what's recoverable, what isn't

Try each of these in order and observe what happens:

```bash
# 1. Try to delete the broken PVC and let the StatefulSet recreate it
kubectl -n capstone-broken-sc delete pvc data-db-0
# The StatefulSet controller recreates the PVC automatically.
# It comes back Pending. No improvement.

# 2. Try to delete the broken SC
kubectl delete sc broken-immediate
# Pending PVCs remain Pending (they're orphaned without their SC).
# StatefulSet can't make progress.

# 3. Try to delete the StatefulSet
kubectl -n capstone-broken-sc delete statefulset db
# Pod and PVC are gone. No data lost because we never wrote anything.

# 4. Try to recreate the StatefulSet with the SAFE SC
kubectl apply -f broken-manifests/wrong-sc.yaml
# Wait, then check:
sleep 20
kubectl -n capstone-broken-sc get pods,pvc -o wide
# Same broken state — but you have to manually edit the StatefulSet to
# point at `standard` instead of `broken-immediate`.
```

The point: **once a StatefulSet's PVC is bound (or stuck Pending
attempting to bind), the recovery path is destructive** unless you
can change the topology constraint. In production this is "delete the
StatefulSet, accept the data loss, restore from backup."

## Recover

```bash
kubectl delete namespace capstone-broken-sc
kubectl delete sc broken-immediate
```

The capstone's `capstone-postgres/postgres-0` is unaffected — it uses
the safe SC and continues running.

## What the lab demonstrated (vs lab 04)

This is lab 04's exact failure mode, applied to the integrated
system. The difference: in lab 04 we *patched* an SC (which fails
because the field is immutable). Here we created a *new* SC with
the wrong mode, which is the realistic production mistake (someone
copies a config from a tutorial that uses Immediate).

## Postmortem prompt (`postmortems/04-capstone-sc.md`)

1. **What does the production migration story look like?** You have
   StatefulSets using SC-X (WaitForFirstConsumer). Someone creates
   SC-Y (Immediate) for a new workload. How do you migrate? (Hint:
   the right answer involves `WaitForFirstConsumer` everywhere; the
   new SC should be `WaitForFirstConsumer` too.)
2. **What's the detection story?** Run this query periodically:
   `kubectl get sc -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.volumeBindingMode}{"\n"}{end}'`
   Alert on any SC with `Immediate` that has StatefulSets using it.
   Why "that has StatefulSets using it" instead of just "any
   Immediate SC"?
3. **What's the backup story?** If a PVC is stuck Pending because
   of a topology mismatch, do you have a recent backup? Can you
   restore? This is why chapter 9 hammers on etcd + PVC backups.
4. **How would an LLM agent fail here?** If asked "deploy a
   Postgres with persistent storage," it might generate a manifest
   using `WaitForFirstConsumer` — or it might generate one using
   `Immediate` because that's what some Stack Overflow answer used.
   How do you detect the wrong mode in a code review? What's the
   one-line check?