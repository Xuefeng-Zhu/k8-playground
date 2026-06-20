# Lab 01 — Draining a node mid-rolling-update

**Chapter tie-in:** `09-reliability-day2.md` — upgrades, PDBs, eviction behaviour.
**Time:** ~25 min.
**Cluster:** `kind-prod-lab` (3 nodes).

---

## Goal

Watch what happens when you try to drain a node while a Deployment is
mid-rollout, and learn how `PodDisruptionBudget` + `terminationGracePeriodSeconds`
change the outcome.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab
kubectl get nodes -o wide   # expect 3 Ready
```

## Step 1 — Deploy a workload with no PDB

```bash
kubectl create namespace lab-01
kubectl -n lab-01 create deployment web --image=nginx:1.27 --replicas=6
# Spread across nodes so we have something to move
kubectl -n lab-01 patch deployment web --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/topologySpreadConstraints",
       "value":[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname",
                 "whenUnsatisfiable":"ScheduleAnyway","labelSelector":{"matchLabels":{"app":"web"}}}]}]'
sleep 5
kubectl -n lab-01 get pods -o wide --label-columns=node.kubernetes.io/hostname
```

You should see pods on all three nodes.

## Step 2 — Start a slow rolling update

```bash
kubectl -n lab-01 patch deployment web \
  --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"nginx:1.27.1"}]'
```

## Step 3 — Pick a worker and try to drain it

```bash
kubectl drain prod-lab-worker --ignore-daemonsets --delete-emptydir-data
```

**Observe:** the command blocks. Watch `kubectl -n lab-01 get pods -w` in
another shell. You'll see one of:

- Drain waits up to `terminationGracePeriodSeconds` (default 30s) per pod.
- If the new pod can't schedule (resource pressure, affinity, etc.) the
  drain will hang for `--pod-selector-timeout` × remaining pods.

## Step 4 — Add a PDB and re-run

```bash
cat <<EOF | kubectl -n lab-01 apply -f -
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web
spec:
  minAvailable: 4
  selector:
    matchLabels:
      app: web
EOF
# Wait for the previous rolling update to finish first
kubectl -n lab-01 rollout status deployment/web
# Now trigger another rolling update + drain
kubectl -n lab-01 patch deployment web --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"nginx:1.27.2"}]'
kubectl drain prod-lab-worker --ignore-daemonsets --delete-emptydir-data
```

**Observe:** the drain now blocks *longer* — the eviction budget is the
binding constraint, not `terminationGracePeriodSeconds`. With `minAvailable: 4`
and 6 replicas, only 2 pods may be unavailable at once, so eviction is gated.

## Step 5 — Force through and write the postmortem

```bash
kubectl -n lab-01 delete pod --field-selector=status.phase=Running --wait=false
# or, on the drain side:
kubectl drain prod-lab-worker --ignore-daemonsets --delete-emptydir-data --force
```

**Restore the cluster:**

```bash
kubectl uncordon prod-lab-worker
kubectl -n lab-01 delete deployment web
kubectl -n lab-01 delete pdb web
kubectl delete namespace lab-01
```

---

## Write up (in `labs/postmortems/01-drain-during-rollout.md`)

Answer these — this is the part that builds intuition:

1. **What did the drain actually wait on?** PDB budget vs grace period vs
   scheduling failure. How did you tell which?
2. **What would the on-call impact have been?** Pick a realistic number for
   `minAvailable` and replicas and compute the worst-case drain time.
3. **What's the right way to upgrade this cluster?** `kubectl drain` is not
   it — describe the production pattern (PDB + maxSurge/maxUnavailable + cordon).
4. **How would an AI agent handle this badly?** Specifically, what's the
   failure mode if an LLM is told "drain node-3" during an update with no
   PDB awareness?

The last question is the one that matters for "learning K8s in an AI world" —
recognising the case where the AI's automation hits a constraint only a human
who's seen it before knows to set up first.

## Bonus: prod reading

- [`PodDisruptionBudget` semantics](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [`kubectl drain` docs](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/)
- Google SRE workbook chapter on safe rollouts (linked in `09-reliability-day2.md`)