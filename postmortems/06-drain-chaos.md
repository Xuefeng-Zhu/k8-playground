# Postmortem: drain a "GPU node" mid-training (Lab 06)

**Date:** 2026-06-19
**Author:** k8s-platform-lab
**Severity:** informational (lab exercise; this is the canonical on-call pattern)

## Summary

On a single-node kind cluster with a tainted `gpu=true:NoSchedule` node, we
launched a training pod with a toleration + nodeSelector, applied a PDB
(`minAvailable: 1`), and then ran `kubectl drain`. The drain **failed
repeatedly** with `Cannot evict pod as it would violate the pod's disruption
budget` — exactly the failure mode a real on-call engineer hits when a
GPU node must come down during a training run.

## Timeline

- **00:00** Lab 06 started. Single node tainted with `gpu=true:NoSchedule`.
- **00:01** Created `train-job` pod with toleration + nodeSelector (no `nvidia.com/gpu`
  resource, so the kubelet admits it on the CPU-only node). Pod is `Running`.
- **00:02** Created `web` Deployment in `lab` namespace (no toleration). Pods are
  `Pending` because the only node is tainted — the *correct* behavior.
- **00:03** Applied PDB: `minAvailable: 1` for `app=training`.
- **00:04** Cordon: node is `SchedulingDisabled`. New `test` pod stays `Pending`.
- **00:05** Drain attempt: `kubectl drain ... --force --grace-period=10`
  starts. The drain attempts to evict `train-job`, fails on the PDB, retries
  every 5 seconds. The same error repeats:
  ```
  error when evicting pods/"train-job" -n "default" (will retry after 5s):
  Cannot evict pod as it would violate the pod's disruption budget.
  ```
- **00:06** After 60s of retry, the drain is still blocked. The pod is still
  `Running`. The node is `SchedulingDisabled` but the pod is alive.
- **00:07** Operator decision: delete the PDB and re-drain. The drain succeeds
  immediately: `train-job` is evicted, node is `drained`.
- **00:08** Uncordon + untaint. coredns and hubble-relay re-place on the
  now-untouched node within 30s. Cluster is healthy.

## Four questions

### 1. What did the system actually do?

The Kubernetes eviction subsystem honored the PDB:
- Drain asked kube-apiserver to evict `train-job` (delete the pod).
- kube-apiserver checked the PDB: `minAvailable=1` for `app=training`, and
  there is currently 1 `train-job` Running. If we evict it, available drops
  to 0, which is below `minAvailable=1`. Eviction denied.
- Drain retried every 5 seconds, exactly as the eviction API specifies.
- The pod was never given a SIGTERM. The kubelet's cgroup was never torn
  down. The OCI container (runc, from lab 01) kept running.

When we deleted the PDB, the next eviction attempt was accepted because the
PD check no longer applied. The pod was deleted, the kubelet noticed, and
runc was asked to stop the container.

### 2. What would the on-call impact have been?

In production, this is the canonical "PDB blocks a real incident" failure:

- The training job is mid-flight, holding 8 A100 GPUs.
- The GPU node needs to come out of service (hardware failure, kernel
  upgrade, node replacement).
- The PDB says "at least 1 training pod must always be available."
- With no other GPU node in the cluster, the PDB is *correct* — the pod
  really has nowhere to go. But the platform team's runbook says the node
  MUST come out now.

User-facing impact:
- If the operator follows the runbook and forces the drain (`--force` on
  the drain, not on the eviction), Kubernetes still enforces the PDB at
  the API level. The drain will retry forever, blocking automation.
- The only safe escapes are: (1) delete the PDB, (2) add a new GPU node
  so the pod has a home, or (3) accept that the node cannot be drained
  and let it run until the job finishes.

### 3. What's the production pattern?

Production ML platforms avoid this entirely by:
- **Always running N+1 GPU nodes** so a single node failure can be
  absorbed by the spare. The PDB then enforces that the spare genuinely
  is the new home.
- **Checkpointing to S3 every N minutes.** The training job can be killed
  and resumed from the last checkpoint, so losing a node doesn't lose
  progress.
- **Volcano PodGroup + Queue**. Volcano enforces gang scheduling, but
  also has explicit "preempt" semantics: low-priority jobs can be
  preempted to make room for higher-priority ones, with a defined
  preemption policy that doesn't depend on PDBs.
- **A separate "drain PDB"** that has a higher `minAvailable` than the
  normal PDB — applied only during maintenance windows. The platform
  team's runbook then says "swap PD Bs before drain, swap back after."

### 4. How would an AI agent fail here?

An AI sees "drain blocked" and might recommend:
- `kubectl drain --force` (it's already in the runbook) — the runbook
  uses `--force` for DaemonSets, not for the PDB. The eviction still
  blocks.
- `kubectl delete pod train-job` — directly bypassing the PDB. The
  training job's state is lost. In production, this is a multi-hour
  restart, not a clean shutdown.
- `kubectl delete pdb training-pdb` followed by drain — actually correct,
  but the AI should *explain* the trade-off (loss of PDB protection)
  before doing it.

What the AI should do FIRST is `kubectl get pdb -A -o yaml` and explain
*which* PDB is blocking, and *why* (minAvailable vs current healthy).
The constraint the AI doesn't know: in production, deleting a PDB
without checking whether the pod has a new home is a self-inflicted
outage. A good answer would propose "add a new GPU node first, then
drain" — but that requires understanding that "no GPU node available"
is the root cause, not "drain is broken."

## Action items

- [x] Verified PDB blocks drain when there's no other home for the pod.
- [x] Verified drain succeeds after PDB is removed.
- [x] Verified node recovery after uncordon + untaint.
- [ ] (Future) Try with `kubectl drain --disable-eviction` to see cordon
      without eviction.
- [ ] (Future) Try with a multi-node cluster to see the pod actually
      reschedule.

## Lessons

1. PDBs are *correctly* blocking; they're not a bug. The right escape
   is to remove the constraint (add capacity, delete the PDB) — not to
   bypass it with `--force`.
2. `kubectl drain` is two operations in one: cordon (mark unschedulable)
   and evict (delete running pods). They're separable: `kubectl cordon`
   alone is useful for maintenance windows where you want the node
   unschedulable but the pods to finish naturally.
3. On single-node clusters, *every* drain blocks if any pod has a PDB
   that includes itself. The lab is single-node, but the failure mode
   is real: a 2-node cluster where the second node is full also produces
   the same error.
