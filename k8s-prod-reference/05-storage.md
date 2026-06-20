# Part V — Storage

Storage is where Kubernetes becomes specific. The abstractions are simple; the realities of durability, performance, and cost live in the CSI driver.

## 5.1 The storage contract

Kubernetes gives you a layered model:

```
  Pod.spec.volumes
      │
      ▼
  PersistentVolumeClaim (PVC)
      │
      ▼
  PersistentVolume (PV) — provisioned dynamically by a StorageClass
      │
      ▼
  CSI driver on the node — actually attaches and mounts the volume
```

A Pod asks for a volume via a PVC. The PVC binds to a PV. The PV is backed by a CSI driver that talks to the actual storage (EBS, GCE PD, Azure Disk, Ceph, NFS, etc.).

### What each layer does

- **Volume** (Pod-level): how the volume is mounted into containers. Types: `emptyDir`, `hostPath`, `configMap`, `secret`, `persistentVolumeClaim`, `csi`, `projected`, `downwardAPI`, etc.
- **PVC**: a request for storage with size, access mode, and storage class. Namespaced.
- **PV**: a piece of storage in the cluster. Cluster-scoped resource. Can be statically provisioned (admin creates them) or dynamically (StorageClass + CSI).
- **StorageClass**: the template for dynamic provisioning. References a provisioner (CSI driver) and parameters.
- **CSI driver**: the plugin that knows how to create/delete/attach/detach/mount volumes on the actual storage backend.

## 5.2 Volume types — when to use what

| Type | Lifetime | Use |
|------|----------|-----|
| `emptyDir` | Pod lifetime | Scratch space, caches, tmp |
| `hostPath` | Node lifetime | Single-node clusters, dev; **avoid in prod** |
| `configMap` / `secret` | Same as source | Inject config/secrets as files |
| `persistentVolumeClaim` | Until PVC deleted | Real persistent storage |
| `projected` | Pod lifetime | Multiple sources merged into one dir |
| `downwardAPI` | Pod lifetime | Inject Pod metadata as files |
| `csi` | Until PVC deleted | Direct CSI reference |

## 5.3 PersistentVolumes and PVCs

### Access modes

| Mode | Meaning | Storage class support |
|------|---------|------------------------|
| `ReadWriteOnce` (RWO) | One node can mount as RW | All block storage |
| `ReadOnlyMany` (ROX) | Many nodes can mount as RO | NFS, CephFS |
| `ReadWriteMany` (RWX) | Many nodes can mount as RW | NFS, CephFS, Azure Files |
| `ReadWriteOncePod` (RWOP) | Only one Pod can mount as RW | CSI 1.x+ |

**Production reality:** RWO is the common case. RWX is expensive (NFS or object-backed). RWOP is the new ideal for databases (no two Pods ever mount the same volume).

### PVC lifecycle

```
Pending ─► Bound ─► In-use (Pod referencing it) ─► Released ─► (deleted)
```

A PVC in `Released` keeps the underlying PV (unless `persistentVolumeReclaimPolicy: Delete`). Stale `Released` PVCs are a common clutter issue. Periodically clean them.

### Reclaim policy

- `Retain` — admin must clean up manually. **Use for production data.**
- `Delete` — PV and underlying storage deleted when PVC is deleted. **Use for ephemeral workloads.**
- `Recycle` — deprecated, ignore.

### Expanding PVCs

In-place expansion requires:
- StorageClass `allowVolumeExpansion: true`.
- CSI driver that supports it (most modern ones do).

```yaml
spec:
  resources:
    requests:
      storage: 200Gi    # was 100Gi
```

Volume expansion is online for most file systems, offline for some. Online expansion is the production default now.

## 5.4 StorageClass and dynamic provisioning

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
```

### `volumeBindingMode`

- **Immediate** — PV is created when PVC is submitted. Might be in a different AZ than the Pod. **Bad.**
- **WaitForFirstConsumer** — PV is created when a Pod is scheduled, in the same AZ. **Good. Default this.**

### Replica topology

For block storage that needs to be replicated across zones (EBS does this; persistent regional disks do not by default), check your driver's capabilities. AWS EBS volumes are zonal — one AZ, one volume.

## 5.5 CSI — Container Storage Interface

CSI is the standard plugin contract for storage. The kubelet talks to the CSI driver via Unix socket. The driver (often run as a DaemonSet) does the actual orchestration.

### Production CSI drivers

| Provider | Driver | Notes |
|----------|--------|-------|
| AWS | `ebs.csi.aws.com` | Per-Pod EBS; EBS gp3, io2 |
| GCP | `pd.csi.storage.gke.io` | Persistent Disk |
| Azure | `disk.csi.azure.com` | Managed Disks |
| NetApp | `netapp.io/trident` | Enterprise storage |
| Ceph | `rook-ceph.rbd.csi.ceph.com` / `rook-ceph.cephfs.csi.ceph.com` | Self-managed distributed storage |
| NFS | `nfs.csi.k8s.io` | Various |
| Local | `local.csi.k8s.io` | Local volumes, node-bound |

### CSI snapshot and clone

CSI snapshot support (1.20+):
- `VolumeSnapshot` API.
- `VolumeSnapshotClass` — like StorageClass for snapshots.
- Use for backups, cloning, dev databases.

Most managed CSI drivers support snapshots. Use them.

## 5.6 Production patterns

### Ephemeral storage

```yaml
spec:
  containers:
    - name: app
      resources:
        requests:
          ephemeral-storage: "1Gi"
          memory: "512Mi"
        limits:
          ephemeral-storage: "2Gi"
          memory: "1Gi"
```

Ephemeral storage includes `/tmp`, container writable layer, logs. Without limits, a logging loop fills the node disk and kubelet evicts you.

### Sub-path and mount propagation

Avoid `subPath` when possible — it breaks atomicity of updates (you get stale files). Use mount paths directly or use projected volumes for config injection.

`mountPropagation` controls whether mounts made inside the container are visible on the host. Set to `HostToContainer` for most; `Bidirectional` only when you really need to mount back to the host (CSI drivers, mostly).

### Performance considerations

- **Network-attached storage has latency.** EBS gp3 has ~1ms IOPS; NFS has more. Test your app's I/O pattern.
- **IOPS throttling.** EBS volumes have IOPS caps. gp3 baseline is 3000 IOPS free; pay for more.
- **Throughput vs IOPS.** gp3 separates the two. Tune for your workload (sequential reads = throughput; random = IOPS).

## 5.7 StatefulSet storage pattern

For databases and queues, the pattern is:

```
StatefulSet
   ├── pod-0 → PVC data-db-0 → PV → EBS volume
   ├── pod-1 → PVC data-db-1 → PV → EBS volume
   └── pod-2 → PVC data-db-2 → PV → EBS volume
```

**Production rules:**
- The StorageClass must be `WaitForFirstConsumer` so volumes land in the right AZ.
- The operator (CloudNativePG, MongoDB Operator, etc.) handles replication, not just PVC.
- Backups must be snapshot-based for RPO ≤ minutes, or pg_dump/wal-g for older patterns.

## 5.8 Storage gotchas and pitfalls

### The `local` storage trap

Local volumes (hostPath, local PV) are tied to a node. If the node dies, the data is gone (unless replicated). Useful for caches, **never for state you can't afford to lose**.

### The "PV stuck in Terminating" problem

When you delete a PVC, the PV is supposed to delete (or stay Released). If the CSI driver is misbehaving or the underlying volume is gone, the PV can hang in `Terminating`. You'll see errors like:
```
Failed to delete PV: failed to delete volume: rpc error: ...
```

Mitigation: force-remove the finalizer on the PV (`kubectl patch pv <name> -p '{"metadata":{"finalizers":null}}'`).

### The "I deleted the StatefulSet but kept the PVCs, now I'm restoring from old data" surprise

When you delete a StatefulSet, PVCs are *not* deleted (they're owned by the StatefulSet via `volumeClaimTemplates` but policy is `Retain` by default). Restarting the StatefulSet reattaches the old PVCs with whatever data they had. **This is good for recovery and bad for "I wanted a fresh DB."**

### Snapshot/backup retention

Snapshots grow over time. Most cloud providers charge per GB. Implement retention policies; test restores; encrypt snapshots at rest.

## 5.9 Production failure modes (Part V scope)

- **`volumeBindingMode: Immediate`** in StorageClass. PVC binds before Pod is scheduled → cross-AZ volume that can't mount → Pod pending forever.
- **EBS volume in wrong AZ** (legacy managed clusters with classic networking, or careless cluster expansion).
- **Snapshot without retention policy.** Storage costs grow without bound.
- **`emptyDir` for data you need.** Lost on Pod restart.
- **`hostPath` in production.** Data is node-local, lost on node failure.
- **No expansion policy.** Out of disk during peak load.
- **PVCs left in `Released`.** API clutter; eventually bind quota.
- **Local PVs without replication.** Silent data loss during node failure.
- **CSI driver crash-looping.** Pods stuck in `ContainerCreating` with mount errors.

## 5.10 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| gp3 (AWS) | Default for most | Need extreme IOPS or NVMe-tier |
| io2 (AWS) | Databases with high IOPS | Cost-sensitive |
| NFS (RWX) | Shared files, build outputs | High-IOPS database |
| Ceph (RWX + RWO) | Self-managed, on-prem | Don't have ops capacity |
| Local PV | Caches, scratch | State you can't afford to lose |
| CSI snapshots | Backups, dev clones | RPO > minutes is fine, use pg_dump |
| StatefulSet + Operator | Real DB | Toy workloads |

## 5.11 Further reading

- [Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [Storage Classes](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [CSI Specification](https://github.com/container-storage-interface/spec)
- [Volume Snapshots](https://kubernetes.io/docs/concepts/storage/volume-snapshots/)
- [Rook (Ceph operator)](https://rook.io/)
- [Trident (NetApp)](https://netapp.io/p/trident-container-storage-interface/)