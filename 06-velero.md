# 6. Velero

**Solves:** Backup and restore of Kubernetes cluster resources and Persistent Volume data. The standard for "I just deleted the namespace, how do I get it back?" and "we need to migrate to a new cluster."

**When you need it:** Day 1. By the time you need it, you needed it yesterday. **Install before your first incident.**

## 6.1 Install (AWS S3 + IRSA — the production path)

```bash
helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts
helm repo update

# S3 bucket for backups
aws s3api create-bucket --bucket ${CLUSTER_NAME}-velero --region us-east-1
# Enable versioning
aws s3api put-bucket-versioning \
  --bucket ${CLUSTER_NAME}-velero \
  --versioning-configuration Status=Enabled
# Block public access
aws s3api put-public-access-block \
  --bucket ${CLUSTER_NAME}-velero \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# IAM policy
cat > velero-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:ListBucket", "s3:GetBucketLocation",
        "s3:ListBucketVersions", "s3:GetObjectVersion", "s3:DeleteObjectVersion"
      ],
      "Resource": [
        "arn:aws:s3:::${CLUSTER_NAME}-velero",
        "arn:aws:s3:::${CLUSTER_NAME}-velero/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ec2:DescribeVolumes", "ec2:DescribeSnapshots", "ec2:CreateSnapshot", "ec2:DeleteSnapshot"],
      "Resource": "*"
    }
  ]
}
EOF
aws iam create-policy --policy-name velero --policy-document file://velero-policy.json

# IRSA
eksctl create iamserviceaccount \
  --cluster=${CLUSTER_NAME} \
  --namespace velero \
  --name velero \
  --role-name velero \
  --attach-policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/velero \
  --override-existing-serviceaccounts \
  --approve

# Install
helm upgrade --install velero vmware-tanzu/velero \
  --namespace velero \
  --create-namespace \
  --values - <<EOF
image:
  repository: velero/velero
  tag: v1.14.0
  pullPolicy: IfNotPresent

configuration:
  provider: aws
  backupStorageLocation:
    - name: default
      provider: aws
      bucket: ${CLUSTER_NAME}-velero
      config:
        region: us-east-1
        # Use IRSA — the pod's SA will be used for S3 + EBS snapshot auth
        # Do NOT put credentials in the config
  volumeSnapshotLocation:
    - name: default
      provider: aws
      config:
        region: us-east-1
  # Default backup TTL
  defaultBackupTTL: 720h       # 30 days

deployRestic: false             # we'll use CSI snapshots for state, not restic
# If you have in-cluster PVs (not cloud disks), enable restic:
# deployRestic: true
# restic:
#   podVolumePath: /var/lib/kubelet/pods
#   defaults:
#     disableCompression: false

credentials:
  existingSecret: velero-credentials  # leave this empty for IRSA

initContainers:
  - name: velero-plugin-for-aws
    image: velero/velero-plugin-for-aws:v1.9.0
    volumeMounts:
      - mountPath: /target
        name: plugins

resources:
  requests: { cpu: 100m, memory: 256Mi }
  limits:   { cpu: 500m, memory: 1Gi }

replicaCount: 2

podDisruptionBudget:
  enabled: true
  minAvailable: 1

serviceAccount:
  server:
    name: velero
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::${AWS_ACCOUNT_ID}:role/velero

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
EOF
```

## 6.2 Schedules — the daily/weekly/monthly cadence

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: daily-full
  namespace: velero
spec:
  schedule: "0 2 * * *"             # 2 AM UTC daily
  useOwnerReferencesInBackup: false # simpler to manage
  template:
    ttl: 168h                        # 7 days retention for daily
    includeClusterResources: true
    defaultVolumesToRestic: false   # we use CSI snapshots, not restic
    snapshotMoveData: false
    volumeSnapshotLocations:
      - default
    includedNamespaces:
      - '*'
    excludedNamespaces:
      - kube-system
      - velero
      - argocd
      - monitoring
      - cert-manager
    labelSelector:
      matchLabels:
        # Skip workloads that explicitly opt out
        # velero.io/skip: "true"
    storageLocation: default
    orderedResources:
      # Restore in dependency order
      - kind: Namespace
      - kind: ServiceAccount
      - kind: Secret
      - kind: ConfigMap
      - kind: PersistentVolumeClaim
      - kind: PodDisruptionBudget
      - kind: Service
      - kind: Deployment
      - kind: StatefulSet
      - kind: DaemonSet
---
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: weekly-full
  namespace: velero
spec:
  schedule: "0 3 * * 0"             # 3 AM UTC Sundays
  template:
    ttl: 2160h                       # 90 days
    includedNamespaces: ['*']
    excludedNamespaces: [kube-system, velero]
---
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: monthly-full
  namespace: velero
spec:
  schedule: "0 4 1 * *"             # 4 AM UTC, 1st of month
  template:
    ttl: 8760h                       # 1 year
    includedNamespaces: ['*']
```

## 6.3 Ad-hoc backup — before risky operations

```bash
# Full backup of one namespace
velero backup create pre-upgrade-$(date +%F) \
  --include-namespaces prod \
  --wait

# Verify
velero backup describe pre-upgrade-2024-01-15 --details
# Status:       Completed
# Errors:       0
# Warnings:     0

# What did it capture?
velero backup get pre-upgrade-2024-01-15 -o yaml | grep -A 5 'Included'

# List items in the backup
velero backup describe pre-upgrade-2024-01-15 --details

# Wait, then proceed with the upgrade
```

## 6.4 Restore

```bash
# What backups exist?
velero backup get
# NAME                          STATUS      CREATED                         EXPIRES   STORAGE LOCATION   SELECTOR
# daily-full-20240115020000     Completed   2024-01-15 02:00:00 +0000 UTC   6d        default            <none>
# daily-full-20240114020000     Completed   2024-01-14 02:00:00 +0000 UTC   5d        default            <none>

# Restore the entire cluster from yesterday
velero restore create --from-backup daily-full-20240114020000 --wait

# Restore only one namespace
velero restore create --from-backup daily-full-20240114020000 \
  --include-namespaces prod \
  --wait

# Restore into a different namespace (use case: clone dev from prod)
velero restore create --from-backup daily-full-20240114020000 \
  --include-namespaces prod \
  --namespace-mappings prod:prod-clone \
  --wait

# What got restored?
velero restore describe --details
```

## 6.5 Cross-cluster migration

```bash
# On the OLD cluster: backup
velero backup create migration-snapshot --include-namespaces prod
# Wait for completion
velero backup describe migration-snapshot

# On the NEW cluster: install Velero pointing at the SAME S3 bucket
# (use the same IRSA / credentials pattern)

# Restore on the new cluster
velero restore create --from-backup migration-snapshot \
  --include-namespaces prod \
  --wait
```

Velero handles the namespace recreation automatically. **PV data restores only if the cluster can attach the original CSI volumes** — typically this means the same cloud provider, possibly the same region.

## 6.6 Restic for filesystem PVs

If you have NFS, hostPath, or emptyDir-style workloads that aren't cloud disks, you need Restic:

```bash
# Restic is bundled when you set deployRestic: true in the Helm chart
# Annotate Pods to back up their volumes:
kubectl annotate pod my-app-pod-xyz \
  -n prod \
  velero.io/backup-volumes=my-volume

# Or use pod-level annotation via the Pod spec:
metadata:
  annotations:
    velero.io/backup-volumes: data,cache
```

Restic is slow and CPU-heavy. For production, prefer **CSI snapshots** for new workloads and use Restic only for legacy / in-cluster storage.

## 6.7 Verify

```bash
# What's installed?
velero backup-location get
# NAME      PROVIDER   BUCKET/PREFIX                 STATUS   LAST VALIDATED
# default   aws        mycluster-velero              Valid    2024-01-15 12:00

velero snapshot-location get

# Schedule status
velero schedule get

# Last backup
velero backup get --output json | jq '.items | sort_by(.metadata.creationTimestamp) | .[-1] | {name: .metadata.name, status: .status.phase, errors: .status.errors}'

# Restore history
velero restore get

# Debug a failed backup
velero backup logs daily-full-20240115020000 | tail -50
velero backup describe daily-full-20240115020000 --details

# Controller logs
kubectl logs -n velero -l app.kubernetes.io/name=velero --tail=200
```

## 6.8 Production gotchas

### **Test your restores. Quarterly.**

The most common failure mode: Velero runs every day, the operator has never tested a restore, the day you need it, the S3 bucket was deleted, or the AWS account was rotated, or the IRSA role is gone.

```bash
# Quarterly drill — spin up a sandbox cluster
eksctl create cluster --name velero-drill --region us-east-1
# Install Velero pointing at the SAME bucket
helm install velero ... --set configuration.backupStorageLocation[0].bucket=prod-velero
# Try restoring a recent backup
velero restore create --from-backup daily-full-...
# If it works, document the time it took. If not, fix.
```

### **Backup before risky operations**

The "schedule" cadence isn't enough. Make ad-hoc backups a pre-deploy step in your runbook:

```bash
# In your upgrade runbook:
velero backup create pre-k8s-upgrade-$(date +%Y%m%d) --wait
# Then proceed
```

### **Resource ordering matters**

The default restore order can fight you (e.g. a Deployment restored before its ConfigMap). The `orderedResources` field in the Schedule template fixes this. Get it right for your workload types.

### **Restic CPU cost**

Restic does whole-file scans. A 1TB PV takes significant time and CPU. For EBS / cloud disks, **prefer CSI snapshots** — they're incremental and don't load the cluster.

### **Backup storage costs**

Backups grow. With daily + weekly + monthly, a 100GB PV can become 500GB+ in S3 over a year. Set `ttl` aggressively. Monitor with:

```bash
aws s3 ls s3://${CLUSTER_NAME}-velero --recursive --summarize | tail
```

### **Cross-region for DR**

A backup in the same region as the cluster won't help if the region goes down. Copy backups to another region:

```bash
# Via S3 cross-region replication
aws s3api put-bucket-replication \
  --bucket ${CLUSTER_NAME}-velero \
  --replication-configuration file://replication.json
```

### **Backup size limits**

CSI snapshots have size limits. EBS snapshots are unlimited but slow for first snapshot. Restic compresses per-file.

### **Cluster resources vs namespace resources**

`includeClusterResources: true` backs up CRDs, ClusterRoles, etc. — useful for full restore. For namespace-only restores, set it to `false` to avoid conflicting with existing cluster-wide resources.

### **Sensitive data in Secrets**

By default, Velero backs up Secrets. If your compliance posture forbids this:

```yaml
spec:
  # In the Backup spec:
  excludedResources:
    - secrets
```

Or use the `--default-volumes-to-restic=false` and selectively opt in via annotations.

### **The 1000-item CRD trap**

Some CRDs (cert-manager, Argo CD) have many custom resources. A full-cluster backup can be 50k+ items and take 30+ minutes to restore. Test the restore time and plan accordingly.

### **Kopia (the future)**

Kopia is replacing Restic for filesystem backups — faster, deduplicated, supports cloud-native storage. Available as `velero install --features=EnableKopia` or via the Helm chart. Migrate when you have time.

### **Helm-managed chart gotcha**

If your apps are managed by Helm, Velero's restore creates resources without Helm labels. The next `helm upgrade` will think they're out of date. Fix with `velero restore create --include-cluster-resources=true` and `adopt-by-helm`, or restore via Helm release labels.

### **Velero version**

Stay within 1 minor version of your Kubernetes. Velero 1.14 supports K8s 1.27-1.30.
