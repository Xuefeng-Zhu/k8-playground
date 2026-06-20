# Lab 05 — GPU scheduling: taints, tolerations, and gang scheduling

> **Topic:** AI/ML Infrastructure (GPU scheduling, training workloads)
> **Goal:** See the Kubernetes scheduler in action as it routes GPU-claiming
> pods to the only node that can host them, and watch what happens when a
> GPU "dies" mid-training.

---

## Why this matters

GPU nodes are expensive (here: $3.10/hr, vs $0.18/hr for general). The last
thing you want is a 4-CPU web pod landing on a GPU node because it ran out
of general capacity. Kubernetes has three orthogonal mechanisms to keep
workloads off GPU nodes unless they really need them:

1. **Taints + tolerations** — repel pods that don't tolerate
2. **nodeSelector / nodeAffinity** — pull pods only to nodes that match
3. **Resource requests** (`nvidia.com/gpu: 1`) — the kubelet only allocates
   the pod if the node has the GPU plugin reporting the resource

Production ML platforms combine all three. This lab walks through each
mechanism on a single node, then exercises the failure mode that every
ML platform team hits at 3am: a GPU dies mid-training.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl --context kind-platform-lab get nodes -o jsonpath='{.items[*].metadata.labels}' | tr ' ' '\n' | grep -E '(gpu|zone|workload)'
# We re-applied the gpu=true:NoSchedule taint earlier in up.sh.
# Verify the cluster state: the taint should be GONE (up.sh removed it so
# coredns/hubble can land), and we'll add a simulated gpu taint back for this lab.
kubectl --context kind-platform-lab describe nodes | grep -i taint
```

If you see "Taints: gpu=true:NoSchedule" already, you're set. If not, the lab
will add it for you in step 1.

## Steps

### 1. Taint the node so only GPU-tolerating pods land here

```bash
# On a multi-node cluster this taint would only be on the GPU node. Here
# we have one node, so we'll taint it, then watch normal pods fail to
# schedule (Pending), and GPU-tolerating pods succeed.
kubectl --context kind-platform-lab taint nodes --all gpu=true:NoSchedule
kubectl --context kind-platform-lab describe nodes | grep -i taints
```

### 2. A non-GPU pod: should stay Pending

```bash
kubectl --context kind-platform-lab run web --image=nginx:1.27 --restart=Never
sleep 10
kubectl --context kind-platform-lab get pod web
kubectl --context kind-platform-lab describe pod web | tail -5
```

You'll see:
- `STATUS: Pending`
- `Events: 0/1 nodes are available: 1 node(s) had untolerated taint {gpu: true}.`

The scheduler is correctly saying "this pod has no toleration, the only node
is tainted, nowhere to go". On a real cluster this is the *desired* behavior —
the pod gets Pending and the cluster-autoscaler can spin up a non-GPU node
for it (or the platform team investigates).

```bash
kubectl --context kind-platform-lab delete pod web --ignore-not-found
```

### 3. A GPU pod with a toleration AND a resource request: should land

```bash
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: train-step
  namespace: default
  labels: { workload: "training" }
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
      image: nginx:1.27  # any image; the resource request is what matters here
      resources:
        requests:
          nvidia.com/gpu: 1
        limits:
          nvidia.com/gpu: 1
EOF
sleep 5
kubectl --context kind-platform-lab get pod train-step -o wide
echo "---events---"
kubectl --context kind-platform-lab describe pod train-step | tail -10
```

You'll see either:
- **`STATUS: Running`** — the scheduler accepted the toleration + nodeSelector
  + resource request, and the kubelet admitted it. (Note: the actual nvidia
  device plugin isn't installed in this lab cluster, so `nvidia.com/gpu: 1`
  is just a *resource*; on a real GPU cluster the device plugin would
  advertise it as allocatable.)
- **`STATUS: Pending` with "Insufficient nvidia.com/gpu"** — this is the
  expected outcome on a CPU-only lab cluster; the resource request is honored
  by the scheduler but no node has the GPU resource advertised.

Either way, you've proven the scheduling path: taint → toleration check →
nodeSelector match → resource check.

### 4. A *gang-scheduled* training job (all-or-nothing across 4 "GPUs")

In a real ML platform, a 4-GPU training job either gets all 4 GPUs at once
or it doesn't run. Partial scheduling wastes money and breaks collectives
(AllReduce across 2 GPUs ≠ across 4). The pattern is a `PodGroup` (Volcano)
or a `Workload` (Kueue).

This cluster has no Volcano/Kueue installed, but the *manifest shape* is
worth seeing. Apply a 4-replica Job and watch what happens:

```bash
cat <<'EOF' | kubectl --context kind-platform-lab apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: train-4gpu
  namespace: default
spec:
  completions: 4
  parallelism: 4
  template:
    metadata:
      labels: { workload: "training" }
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
            limits:
              nvidia.com/gpu: 1
EOF
sleep 8
kubectl --context kind-platform-lab get pods -l job-name=train-4gpu -o wide
```

What to look for:
- **In a 1-node cluster**: pods go Pending. There's no "all-or-nothing" gate
  by default — K8s will let partial scheduling happen if you don't use a
  scheduler plugin (Volcano, Kueue) to enforce it.
- **In a real GPU cluster**: you'd see 4 pods go Running, then the Job
  controller marks `COMPLETIONS: 4/4` only when all 4 reach Succeeded.

The takeaway: **gang scheduling requires a scheduler extension**. Default
kube-scheduler is best-effort, not all-or-nothing.

```bash
kubectl --context kind-platform-lab delete job train-4gpu --ignore-not-found
```

### 5. The failure mode: a "GPU" dies mid-training

```bash
# Recreate the single train-step pod, then "evict" it (simulating a
# kubelet-detected GPU failure → API-server delete).
kubectl --context kind-platform-lab delete pod train-step --ignore-not-found
kubectl --context kind-platform-lab apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: train-step
  namespace: default
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
kubectl --context kind-platform-lab get pod train-step -o wide

# Now: simulate a hardware failure. The kubelet on the node detects a
# GPU error and evicts the pod. We mimic that with a direct API delete.
echo "---simulating GPU failure (kubelet eviction)---"
kubectl --context kind-platform-lab delete pod train-step
sleep 3
kubectl --context kind-platform-lab get pod train-step 2>&1 | tail -1
```

What to look for:
- The pod was Running; the delete was instant. In production, kubelet's GPU
  health check (via the device plugin's health-check sidecar) would do this.
- The pod does NOT reschedule on its own — without a controller (Deployment,
  Job, Volcano taskGroup), "all or nothing" means "all dead" if a GPU fails.
- This is why production training jobs are managed by Volcano or Kueue, not
  raw Pods/Deployments.

## Restore

```bash
kubectl --context kind-platform-lab delete pod train-step --ignore-not-found
kubectl --context kind-platform-lab delete job train-4gpu --ignore-not-found
# Remove the taint so other labs (and coredns, hubble, etc.) are happy.
kubectl --context kind-platform-lab taint nodes --all gpu=true:NoSchedule-
```

## Postmortem prompts

Answer these in `postmortems/05-gpu-scheduling.md`:

1. **What did the system actually do?**
   - For step 3, did the GPU pod actually go Running, or did it stay Pending
     because the device plugin wasn't installed? What does that tell you
     about the difference between "scheduler knows about GPUs" and
     "node can actually run a CUDA kernel"?
2. **What would the on-call impact have been?**
   - In step 5, after the simulated GPU death, why didn't the pod come back?
     What's the production fix (Deployment, StatefulSet, Volcano PodGroup)?
3. **What's the production pattern?**
   - Real GPU clusters use:
     - **Node Feature Discovery (NFD)** to label nodes by GPU model/driver
     - **NVIDIA device plugin** to advertise `nvidia.com/gpu` to the kubelet
     - **Volcano / Kueue** for gang scheduling
     - **MIG slicing** to share a single A100 across multiple pods
     - **Topology-aware scheduling** to keep GPUs on the same NVLink island
     Which of these would you wire in first if you were building this today?
4. **How would an AI agent fail here?**
   - An AI sees Pending pods and recommends `kubectl taint nodes ... :-` to
     "fix it". What it should do is look at the events: "untolerated taint
     {gpu: true}" means the *workload* is wrong, not the node.

## Stretch (optional)

Install Volcano (`kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/master/installer/volcano-development.yaml`)
and re-run step 4 with a `PodGroup` spec:

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: train-4gpu
spec:
  minMember: 4
  minResources:
    nvidia.com/gpu: 4
```

Now the scheduler will hold all 4 pods until 4 GPUs are simultaneously
available — true gang scheduling.
