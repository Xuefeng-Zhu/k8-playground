# Lab 04 — StatefulSet through a node loss with `volumeBindingMode: Immediate`

**Chapter tie-in:** `05-storage.md` — PVCs, StorageClasses, CSI, the difference between bound and dynamically provisioned volumes, and how topology constraints affect rescheduling.
**Time:** ~30 min.
**Cluster:** `kind-prod-lab`.

---

## Goal

Deploy a StatefulSet that pins its volume to a specific zone (worker),
delete that worker, watch the StatefulSet get stuck. Learn the difference
between `volumeBindingMode: Immediate` and `WaitForFirstConsumer`, and why
the wrong choice takes your database offline.

## Background

`local-path` (kind's default StorageClass) uses `volumeBindingMode: WaitForFirstConsumer`
by default — that's the right choice, and it's why StatefulSets in kind
usually survive node loss. But many cloud StorageClasses default to
`Immediate`, which means: bind the volume to a zone *when the PVC is created*,
not when the pod schedules. The result: StatefulSet claims a volume in
zone A, schedules to zone B, refuses to start.

We'll reproduce the failure mode by patching the StorageClass.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab
kubectl create namespace lab-04

# What we have
kubectl get sc
# NAME                   PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION
# standard (default)     rancher.io/local-path   Delete          WaitForFirstConsumer   false
```

## Step 1 — Deploy a StatefulSet with the safe default

```bash
cat <<EOF | kubectl -n lab-04 apply -f -
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None
  selector:
    app: db
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db
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
      - name: db
        image: nginx:1.27   # nginx in place of postgres; we just need state
        volumeMounts:
        - name: data
          mountPath: /data
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: standard
      resources:
        requests:
          storage: 100Mi
EOF

kubectl -n lab-04 get pods -l app=db -o wide -w
# Wait for all 3 to be Running
```

Each pod is bound to a worker (zone-a or zone-b via the topology label
you set in `cluster.yaml`). The PVCs were created *after* scheduling, so
they're in the right zone. **This is the production-correct path.**

## Step 2 — Tag a worker with a "zone" label

```bash
kubectl label node prod-lab-worker topology.kubernetes.io/zone=a --overwrite
kubectl label node prod-lab-worker2 topology.kubernetes.io/zone=b --overwrite
```

## Step 3 — Cordon a worker, drain it, "lose" it

Simulating a node loss:

```bash
kubectl cordon prod-lab-worker
kubectl drain prod-lab-worker --ignore-daemonsets --delete-emptydir-data --force

# Simulate the actual loss: stop the container
docker stop prod-lab-worker

# Wait. The StatefulSet will try to reschedule `db-0` (which was on prod-lab-worker)
# onto prod-lab-worker2. The PVC was created with WaitForFirstConsumer, so it
# was bound to whichever zone `db-0` landed in. New pod should bind correctly.
kubectl -n lab-04 get pods -l app=db -o wide -w
```

**Observe:** within ~30s, `db-0` should reschedule to `prod-lab-worker2`.
If the PVC was correctly bound (it was, because of `WaitForFirstConsumer`),
the volume follows the pod. The StatefulSet recovers.

## Step 4 — Now reproduce the BAD path

## Step 5 — Patch the StorageClass to `Immediate`

```bash
kubectl -n lab-04 delete statefulset db
kubectl -n lab-04 delete pvc --all
```

> **Real bug in the original lab:** `volumeBindingMode` is an
> **immutable** field on a StorageClass — K8s rejects patches with
> `field is immutable`. So you can't change an existing SC. The
> prevention pattern is to **create a new SC** (with a new name) and
> migrate workloads. In cloud providers this means creating a new
> StorageClass with the same provisioner but `Immediate`, and the
> old SC stays around until you migrate off it.

```bash
# Create a NEW SC with Immediate binding (this is what an SRE
# would actually do in production — they wouldn't patch the default)
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: broken-immediate
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: Immediate
EOF

# Label nodes with fake zones so we can demo topology conflicts
kubectl label node prod-lab-worker topology.kubernetes.io/zone=zone-a --overwrite
kubectl label node prod-lab-worker2 topology.kubernetes.io/zone=zone-b --overwrite
```

## Step 6 — Reproduce the failure

```bash
cat <<EOF | kubectl -n lab-04 apply -f -
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None
  selector:
    app: db
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db
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
      - name: db
        image: nginx:1.27
        volumeMounts:
        - name: data
          mountPath: /data
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: broken-immediate   # the unsafe one
      resources:
        requests:
          storage: 100Mi
EOF

sleep 15
kubectl -n lab-04 get pods -l app=db -o wide
kubectl -n lab-04 get pvc
```

**Observe:** with `Immediate` binding, the PVC binds to whichever node
the provisioner picks first. The StatefulSet pod is then **stuck in
Pending** because:

1. local-path doesn't actually support `Immediate` (it requires a pod
   to determine the local path). The PVC never binds → the pod never
   gets scheduled.
2. In a cloud cluster (EBS, Persistent Disk, etc.) the PVC *would*
   bind to a zone, and a pod with a conflicting zone selector would
   stay Pending forever.

**The real-world lesson:** any StatefulSet workload (Postgres, Mongo,
Kafka, ZooKeeper, Elasticsearch, etcd-as-a-service) running on a cloud
provider with `volumeBindingMode: Immediate` is **one zone outage away
from a permanent data unavailability event**. You can detect this with:

```bash
kubectl get sc -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.volumeBindingMode}{"\n"}{end}'
```

If any SC is `Immediate` and you have StatefulSets using it, that's a
production risk.

## Restore

```bash
kubectl -n lab-04 delete statefulset db --ignore-not-found
kubectl -n lab-04 delete pvc --all --ignore-not-found
kubectl delete storageclass broken-immediate --ignore-not-found
kubectl label node prod-lab-worker topology.kubernetes.io/zone-
kubectl label node prod-lab-worker2 topology.kubernetes.io/zone-
# If you stopped a docker container in earlier steps, bring it back:
docker start prod-lab-worker
kubectl uncordon prod-lab-worker
kubectl delete namespace lab-04
```

---

## Write up (`labs/postmortems/04-statefulset-zone-loss.md`)

1. **What's the chain of failures?** PVC binds before pod schedules →
   PV is in wrong zone → pod can't be scheduled → StatefulSet is wedged.
   Where in this chain does the human need to be?
2. **What's the right `volumeBindingMode` for a database StatefulSet?**
   What about for a stateless workload with a `PersistentVolumeClaim`
   cache?
3. **What's the recovery path?** Walk through: lose a zone, StatefulSet
   pods stuck, what does an SRE actually do? (Hint: it involves manual
   PVC surgery and an apology to the data team.)
4. **Why does chapter 5 say this is the most common stateful-workload
   outage?** Connect to the cloud defaults (EBS, Persistent Disk, etc.)
   that ship with `Immediate`.
5. **How would an AI agent fail here?** If an agent is told "the database
   is down, restart the pods," it might just keep cycling them. Why is
   that actively dangerous here? What should it check first?

## Bonus: prod reading

- [Storage Classes — `volumeBindingMode`](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
- [Kubernetes docs on StatefulSet stable storage](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#stable-storage)
- [GKE Persistent Disk CSI docs](https://cloud.google.com/kubernetes-engine/docs/concepts/storage-overview)
- Your reference: `05-storage.md` (the section on topology-aware volume
  binding is the production gotcha).