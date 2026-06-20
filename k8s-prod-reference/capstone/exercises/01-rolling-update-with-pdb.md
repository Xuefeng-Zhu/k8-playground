# Exercise 01 — Rolling update + drain, with and without a PDB

**Linked lab:** `labs/01-drain-during-rollout.md`
**Linked chapter:** `09-reliability-day2.md` (upgrades, PDB math)

**Time:** ~15 min
**What you'll break:** the guarantee that `minAvailable: 1` backend
replicas are always up during voluntary disruption, by deploying a
parallel backend with NO PDB and draining it.

---

## Before

The capstone has a backend with `PodDisruptionBudget: minAvailable: 1`
(see `manifests/02-backend.yaml`). Verify it's there:

```bash
kubectl -n capstone-backend get pdb
# NAME      MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE
# backend   1               N/A               1                     ...
kubectl -n capstone-backend get pods -o wide --label-columns=node.kubernetes.io/hostname
# 2 backend pods, ideally spread across two workers
```

## Break — deploy a no-PDB sibling

```bash
kubectl -n capstone-backend apply -f broken-manifests/no-pdb.yaml
# Wait for it to come up
kubectl -n capstone-backend wait --for=condition=available deployment/backend-no-pdb --timeout=60s
kubectl -n capstone-backend get pods -l variant=no-pdb -o wide
```

Now you have **two backends side by side**:
- `backend` (the capstone one) — has a PDB
- `backend-no-pdb` — no PDB, same shape

## Observe — drain the worker holding backend-no-pdb

```bash
# Find which worker is holding a backend-no-pdb pod
NO_PDB_NODE=$(kubectl -n capstone-backend get pod -l variant=no-pdb \
  -o jsonpath='{.items[0].spec.nodeName}')
echo "draining $NO_PDB_NODE"

# Find which worker is holding the PDB-backed backend
PDB_NODE=$(kubectl -n capstone-backend get pod -l variant=,app=backend \
  -o jsonpath='{.items[0].spec.nodeName}')
echo "with PDB, backend is on $PDB_NODE"
```

**Drain the no-PDB backend first:**

```bash
time kubectl drain $NO_PDB_NODE --ignore-daemonsets --delete-emptydir-data --timeout=60s
# Watch what happens to the pod on that node:
kubectl -n capstone-backend get pods -l variant=no-pdb -w
```

You should see: drain completes in **a few seconds**, and during that
window the no-PDB backend has **0 replicas**. Anyone hitting the
service gets connection refused.

**Now drain the PDB-backed backend:**

```bash
# Uncordon first so the worker is available again
kubectl uncordon $NO_PDB_NODE
time kubectl drain $PDB_NODE --ignore-daemonsets --delete-emptydir-data --timeout=120s
kubectl -n capstone-backend get pods -l app=backend -w
```

You should see: drain takes **much longer** (often the full
`terminationGracePeriodSeconds: 30`, or longer if the PDB has to wait
for a replacement pod to be Ready). At no point does the PDB-backed
backend go below 1 ready replica.

## Recover

```bash
kubectl uncordon $NO_PDB_NODE $PDB_NODE
kubectl -n capstone-backend delete deployment backend-no-pdb
```

## What this proved

The PDB is the difference between:
- **Disruption budget = ∞** (no PDB): drain is fast, service has 0 replicas
- **Disruption budget = 1**: drain is gated, service keeps 1 replica serving

In production, the choice of `minAvailable` is a **product decision**
(some disruption budget vs. always serving N-1) that an LLM agent
**cannot make on your behalf** — it depends on what your users can
tolerate and how your SLA is structured.

## Postmortem prompt (`postmortems/01-capstone-pdb.md`)

1. **What was the actual downtime** of each backend during its respective
   drain? Quantify in seconds, not hand-wavy "a few seconds."
2. **What's the right `minAvailable` for a 3-replica backend?**
   1, 2, or 3? What's the trade-off? (Hint: rolling updates and drains
   share the same budget.)
3. **What would an LLM agent do badly here?** Specifically: if told
   "drain node X to upgrade," would it know to check the PDB? Would it
   suggest raising replicas before draining to keep the budget intact?
   What would you need to tell it?
4. **How does this connect to chapter 9's upgrade patterns?** PDB +
   maxSurge + maxUnavailable together control disruption budget across
   *both* rolling updates AND voluntary drains. They share the same
   budget.