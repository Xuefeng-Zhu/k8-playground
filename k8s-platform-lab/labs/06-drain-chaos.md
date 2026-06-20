# Lab 06 — Chaos: drain a "GPU node" mid-training

> **Topic:** Reliability (combines: taints, PDBs, drain, scheduling, observability)
> **Goal:** Drain a node that hosts a training job, watch what happens to
> the pod, observe the gap that PDBs (and the lack thereof) create.

---

## Why this matters

"Cordoning and draining" is the standard way to take a node out of service
in Kubernetes — but it's also a great way to learn:

1. How the scheduler responds to a NotReady node
2. What `PodDisruptionBudget` (PDB) protects against and what it doesn't
3. Why `kubectl drain --force` is sometimes the only option
4. How a real platform team rehearses this before a node replacement

This lab simulates the canonical 3am scenario: a GPU node needs to come
out of service, the training job is mid-flight, the on-call engineer has
15 minutes to either keep the job alive or kill it cleanly.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl --context kind-platform-lab get nodes
kubectl --context kind-platform-lab get pods -A | grep -v kube-system | head -10
```

## Steps

### 1. Set the stage: a training job + a "general" pod, both running

```bash
# Taint the node as GPU-only so the training pod has to tolerate.
kubectl --context kind-platform-lab taint nodes --all gpu=true:NoSchedule
# Taint the node as "general" too, so web has a special place to land.
# (We'll undo this so the web pod can't run.)
# Actually no — let's give the web pod a non-existent nodeSelector so
# it CAN'T run on this node, simulating a multi-node cluster.
```

```bash
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: train-job
  namespace: default
  labels: { app: training }
spec:
  restartPolicy: Never
  tolerations:
    - key: gpu
      operator: Equal
      value: "true"
      effect: NoSchedule
  nodeSelector:
    workload: gpu
  containers:
    - name: trainer
      image: nginx:1.27
      resources:
        requests:
          nvidia.com/gpu: 1
EOF
sleep 5
kubectl --context kind-platform-lab get pod train-job -o wide
```

(Note: as in lab 05, this pod will be `Pending` because no node has the
`nvidia.com/gpu` resource advertised. That's fine — the lab is about the
*drain behavior*, not the pod actually running. We'll remove the
resource request for the actual chaos steps so the pod can be Running.)

```bash
# Re-apply without the nvidia.com/gpu resource, so it can actually run.
kubectl --context kind-platform-lab delete pod train-job --ignore-not-found
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: train-job
  namespace: default
  labels: { app: training }
spec:
  restartPolicy: Never
  tolerations:
    - key: gpu
      operator: Equal
      value: "true"
      effect: NoSchedule
  nodeSelector:
    workload: gpu
  containers:
    - name: trainer
      image: nginx:1.27
EOF
sleep 5
kubectl --context kind-platform-lab get pod train-job -o wide
echo "---should now be Running---"
```

Also launch a `general` workload (in the lab namespace) so we can see what
drain does to *other* pods too:

```bash
kubectl --context kind-platform-lab create deploy web --image=nginx:1.27 --replicas=2 -n lab
sleep 8
kubectl --context kind-platform-lab -n lab get pods -o wide
```

### 2. (Optional) Add a PodDisruptionBudget to protect the training job

```bash
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: training-pdb
  namespace: default
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: training
EOF
echo "PDB applied. With minAvailable=1, drain will wait for the pod to be evicted gracefully."
```

In a multi-node cluster, a PDB with `minAvailable: 1` would *block* `kubectl
drain` until the pod moves naturally. On single-node, the pod has nowhere
to move, so drain will hang — that's the failure mode the postmortem
prompts will dig into.

### 3. Cordon the node (mark it unschedulable, but keep running pods)

```bash
kubectl --context kind-platform-lab cordon platform-lab-control-plane
kubectl --context kind-platform-lab describe nodes | grep -i 'unschedulable\|taints' | head -5
echo "---new pods should now stay Pending---"
kubectl --context kind-platform-lab run test --image=nginx:1.27 --restart=Never
sleep 5
kubectl --context kind-platform-lab get pod test
kubectl --context kind-platform-lab delete pod test --ignore-not-found
```

### 4. Drain the node (evict all running pods)

```bash
# Use --ignore-daemonsets because the cilium DaemonSet (and any other
# node-level pods) MUST stay on the node. --delete-emptydir-data is
# safe here because all our pods use emptyDir.
echo "---draining (PDB will likely block this; we'll time it out at 60s)---"
timeout 60 kubectl --context kind-platform-lab drain platform-lab-control-plane \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \
  --grace-period=10 2>&1 | tail -20
echo
echo "---node state after timeout---"
kubectl --context kind-platform-lab get nodes
echo
echo "---what happened to our pods?---"
kubectl --context kind-platform-lab get pods -A | grep -v kube-system
```

What you should see:
- A repeating error: `error when evicting pods/"train-job" ... Cannot evict pod as it would violate the pod's disruption budget.`
- The drain **does not complete** because the PDB says "at least 1 of {app=training} must be available" and the only other node doesn't exist.
- `train-job` stays `Running`. The node is `SchedulingDisabled` (cordoned) but the pods are still there.

This is the canonical "PDB blocks a real incident" failure mode. In production, you have to make a choice:
- wait for the training job to finish naturally (could be hours),
- scale up the cluster so a new home exists, or
- delete the PDB and re-drain (what we do in step 4b).

### 4b. The "incidents still happen" escape hatch

```bash
echo "---delete the PDB and re-drain---"
kubectl --context kind-platform-lab delete pdb training-pdb
kubectl --context kind-platform-lab drain platform-lab-control-plane \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \
  --grace-period=5 2>&1 | tail -10
```

Now the drain succeeds: `train-job` is evicted, the node is fully drained.
The cilium-operator Deployment replica is also evicted (it IS a Deployment,
not a DaemonSet — `--ignore-daemonsets` only protects `cilium-rjpvn`).

In production this is what the on-call engineer has to do during a real
node failure. The "cost" is the training job loses its in-flight state —
which is why production ML platforms checkpoint to S3 every N minutes
and resume from the last checkpoint rather than restart from scratch.

### 5. Watch Hubble: the drain should generate a burst of eBPF-visible events

```bash
# Start a port-forward to hubble-metrics.
# (use background: true)
# Then watch the dropped/forwarded counters during the drain.
curl -s http://127.0.0.1:9965/metrics | grep -E '^hubble_(drop|flows_processed)_total' | head -10
```

In a single-node cluster, draining the only node is catastrophic for
running pods. In a multi-node cluster, you'd see the pods reschedule
elsewhere — and Hubble would show a brief burst of TCP RST/FIN as
connections close.

### 6. Restore: uncordon, retest

```bash
# Make the node schedulable again.
kubectl --context kind-platform-lab uncordon platform-lab-control-plane
# Remove the GPU taint so future labs and coredns/hubble are happy.
kubectl --context kind-platform-lab taint nodes --all gpu=true:NoSchedule-
# Verify the cluster is healthy.
kubectl --context kind-platform-lab get nodes
kubectl --context kind-platform-lab get pods -A | head -10
```

## Restore (final)

```bash
kubectl --context kind-platform-lab delete pod train-job --ignore-not-found
kubectl --context kind-platform-lab delete deploy web -n lab --ignore-not-found
kubectl --context kind-platform-lab delete pdb training-pdb --ignore-not-found
pkill -f "port-forward ds/cilium" 2>/dev/null || true
```

## Postmortem prompts

Answer these in `postmortems/06-drain-chaos.md`:

1. **What did the system actually do?**
   - When you ran `kubectl drain ... --force --grace-period=10`:
     - Did the training pod get the SIGTERM signal and the 10-second
       grace period? Or did the API server delete it immediately?
     - Did the `web` deployment's pods get rescheduled? (On a single-node
       cluster, no — there's nowhere for them to go.)
2. **What would the on-call impact have been?**
   - A real cluster has 50+ nodes. A drain moves pods smoothly because
     the scheduler can find a new home. The PDB's `minAvailable: 1`
     means the drain *blocks* if the pod can't find a new home —
     which is the correct safety property, but it can also deadlock
     a node replacement.
   - In production, what do you do when a drain is blocked by a PDB and
     the node really needs to come out?
3. **What's the production pattern?**
   - **Maintenance windows**: cordon + drain during low traffic.
   - **`kubectl drain --disable-eviction`**: cordon without evicting,
     so the node is unschedulable for new pods but running ones finish.
   - **Node replacement workflow**: provision a new node → migrate
     pods (DaemonSets are sticky) → drain old → terminate.
   - **PDB + maxSurge in Deployments**: ensures you can do
     `kubectl rollout restart` without violating SLOs.
4. **How would an AI agent fail here?**
   - An AI sees a stuck drain and recommends `kubectl drain --force`.
     That's a destructive action that bypasses the PDB safety net.
     What it should do FIRST is check WHY the drain is stuck:
     `kubectl get pods -A -o wide` to see what's blocking, then
     `kubectl get pdb -A` to see which budget is failing.

## Stretch (optional)

If you have `chaos-mesh` or `litmuschaos` installed, schedule a real
pod-kill chaos experiment on the `train-job`:

```bash
kubectl apply -f https://raw.githubusercontent.com/chaos-mesh/chaos-mesh/master/examples/chaos-experiment.yaml
```

Compare the eBPF/Hubble events you see during the chaos experiment
vs. a normal drain. The kernel sees both the same way.
