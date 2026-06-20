# 2. External Secrets Operator (ESO)

**Solves:** Synchronises secrets from external stores (AWS Secrets Manager, AWS Parameter Store, GCP Secret Manager, Azure Key Vault, HashiCorp Vault, 1Password) into Kubernetes Secrets, with automatic refresh on rotation.

**When you need it:** Anywhere you'd otherwise `kubectl create secret` with a manually-rotated value. After 3+ namespaces with secrets, you need ESO.

## 2.1 Install

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --values - <<'EOF'
installCRDs: true
replicaCount: 2

resources:
  requests: { cpu: 50m, memory: 64Mi }
  limits:   { cpu: 200m, memory: 256Mi }

securityContext:
  runAsNonRoot: true
  seccompProfile: { type: RuntimeDefault }

podDisruptionBudget:
  enabled: true
  minAvailable: 1

webhook:
  replicaCount: 2
  failurePolicy: Fail           # hard fail if webhook is down — better than silent skip

certController:
  replicaCount: 2

# Production observability
serviceMonitor:
  enabled: true
prometheus:
  enabled: true
EOF
```

## 2.2 ClusterSecretStore — backends

A `ClusterSecretStore` (cluster-scoped) or `SecretStore` (namespace-scoped) defines the connection to the backend.

### AWS Secrets Manager (IRSA — the production path)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-aws   # SA with IRSA annotation
            namespace: external-secrets
```

**IAM setup:**

```bash
# Policy — read-only access to specific secret paths
cat > eso-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret"
    ],
    "Resource": "arn:aws:secretsmanager:us-east-1:${AWS_ACCOUNT_ID}:secret:prod/*"
  }]
}
EOF
aws iam create-policy --policy-name eso-prod-read --policy-document file://eso-policy.json

eksctl create iamserviceaccount \
  --cluster=${CLUSTER_NAME} \
  --namespace external-secrets \
  --name external-secrets-aws \
  --role-name eso-prod-read \
  --attach-policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/eso-prod-read \
  --override-existing-serviceaccounts \
  --approve
```

### AWS Parameter Store (cheaper than Secrets Manager for static config)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-parameter-store
spec:
  provider:
    aws:
      service: ParameterStore
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-aws
            namespace: external-secrets
```

### GCP Secret Manager (Workload Identity)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: gcp-secret-manager
spec:
  provider:
    gcpsm:
      projectID: your-gcp-project
      auth:
        workloadIdentity:
          serviceAccountRef:
            name: external-secrets-gcp
            namespace: external-secrets
          # GSA has roles/secretmanager.secretAccessor on the project
```

### HashiCorp Vault (Kubernetes auth)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: vault
spec:
  provider:
    vault:
      server: https://vault.vault-system.svc:8200
      path: kv/data     # KV v2
      version: v2
      auth:
        kubernetes:
          mountPath: kubernetes
          role: external-secrets
          serviceAccountRef:
            name: external-secrets-vault
            namespace: external-secrets
```

**Vault policy + role (run on Vault):**

```bash
# Policy — read specific paths
vault policy write eso - <<'EOF'
path "kv/data/prod/*" { capabilities = ["read"] }
path "kv/data/shared/*" { capabilities = ["read"] }
EOF

# Kubernetes auth role
vault write auth/kubernetes/role/external-secrets \
  bound_service_account_names=external-secrets-vault \
  bound_service_account_namespaces=external-secrets \
  policies=eso \
  ttl=1h
```

### Azure Key Vault (Workload Identity)

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: azure-keyvault
spec:
  provider:
    azurekv:
      vaultUrl: https://your-vault.vault.azure.net
      authType: WorkloadIdentity
      serviceAccountRef:
        name: external-secrets-azure
        namespace: external-secrets
```

## 2.3 ExternalSecret — the resource apps use

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-creds
  namespace: prod
spec:
  refreshInterval: 5m              # how often to re-sync
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: db-creds                 # the K8s Secret to create
    creationPolicy: Owner          # ESO owns the Secret; don't edit it manually
    deletionPolicy: Retain         # if ExternalSecret is deleted, keep the Secret
    template:
      type: Opaque
      metadata:
        annotations:
          # Optional: signals to kyverno / policy engines
          reloaded-by: external-secrets
      data:
        # Map keys from the source to the destination
        DB_PASSWORD: "{{ .secretData.password }}"
  data:
    - secretKey: DB_PASSWORD
      remoteRef:
        key: prod/db/main
        property: password
    # For JSON secrets, property extracts a sub-field
    - secretKey: DB_HOST
      remoteRef:
        key: prod/db/main
        property: host
```

### JSON-from-secret pattern (entire JSON object → multiple keys)

```yaml
spec:
  dataFrom:
    - extract:
        key: prod/api-service    # the entire JSON
  target:
    data:
      # each key in the JSON becomes a key in the K8s Secret
      # if you don't list keys, all are extracted
      - secretKey: API_KEY
      - secretKey: API_URL
```

### Auto-reload on rotation

Pods need to know to reload when the Secret changes. Three patterns:

**1. Reloader (separate controller, simple):**
```bash
helm upgrade --install reloader stakater/reloader --namespace reloader --create-namespace
```
Then add the annotation to your Deployment:
```yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
    # or specific: reloader.stakater.com/match: "true"
    # or: reloader.stakater.com/secret: "db-creds"
```

**2. Mounted secret + app that watches mtime (most apps don't, sadly):**
No annotation; just rely on the kubelet refresh (default 60-90s for mounted Secrets).

**3. Push-based via vault-agent / secrets-store CSI driver:**
Best for Vault. Not ESO's strength.

## 2.4 Verify

```bash
# Is the store ready?
kubectl get clustersecretstore
# NAME                  AGE   STATUS   CAPABILITIES   READY
# aws-secrets-manager   5d    Valid    ReadOnly       True

# What secrets have been synced?
kubectl get externalsecret -A

# Status of one
kubectl describe externalsecret db-creds -n prod
# Look for: Status: SecretSynced

# When did it last sync?
kubectl get externalsecret db-creds -n prod -o jsonpath='{.status.conditions[?(@.type=="Ready")].lastTransitionTime}'

# The resulting Secret
kubectl get secret db-creds -n prod
kubectl get secret db-creds -n prod -o jsonpath='{.data.DB_PASSWORD}' | base64 -d

# Operator logs
kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=100
```

## 2.5 Production gotchas

### The "stuck syncing" failure mode

A SecretStore that worked yesterday can fail today when:
- IRSA role is replaced (Terraform drift).
- The AWS secret is deleted but the ExternalSecret still references it.
- Vault token expires (Kubernetes auth role has `ttl=1h`).

**Always set alerts:**
```promql
# ESO sync failures (any namespace)
sum by (namespace, name) (
  increase(external_secrets_sync_calls_total{success="false"}[15m])
) > 0

# Stale secrets (no successful sync in 1h — should rotate every 5m)
time() - external_secrets_last_sync_time > 3600
```

### Secret rotation cadence mismatch

If your source rotates every 24h but `refreshInterval: 5m`, you're fine.
If your source rotates every 1h but `refreshInterval: 24h`, you're leaking old credentials.

Match `refreshInterval` to the source's rotation policy, with a margin of error.

### IAM policy creep

The example policy `arn:aws:secretsmanager:*:*:secret:prod/*` is too broad. Use specific ARNs and apply the principle of least privilege per ExternalSecret:

```json
{
  "Effect": "Allow",
  "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
  "Resource": [
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-creds-AbCdEf",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/api-keys-XyZ123"
  ]
}
```

### The "two writes" race

When you change both the `ExternalSecret` spec AND the source secret, ESO can write an intermediate state. Pin your `refreshInterval` to a sane value (1m or 5m) and use `creationPolicy: Owner` to make ESO the only writer.

### Owner vs Orphan vs IfNotExists

- `Owner` (default): ESO creates, updates, deletes the Secret. **Use this.**
- `Orphan`: ESO creates; does not delete. Use when another controller needs to coexist.
- `IfNotExists`: ESO creates only if the Secret doesn't exist; never updates. Use for one-shot bootstrapping.

### ClusterSecretStore vs SecretStore

Use `ClusterSecretStore` for cluster-wide backends (AWS, Vault). Use `SecretStore` only when a namespace needs a different backend (e.g. dev pointing to a different Vault path).

### What if ESO itself goes down?

If ESO is down for 1 hour, secrets still in K8s Secrets continue to work — but no rotation. The blast radius is "stale credentials until ESO recovers." For high-rotation workloads (Vault dynamic secrets), this matters.

Set `replicaCount: 2+`, add PDB, monitor with the prometheus rules above.

### Avoid embedding secrets in Helm values

If you `helm install` with `--set dbPassword=...`, that secret is now in your CI logs, your Argo CD history, and your git history. **Always let ESO fetch the secret from a real backend.** Don't put secret values in Git, ever.

### Migrating from raw Secrets

1. Create the ClusterSecretStore.
2. Create the ExternalSecret — verify the synced Secret matches.
3. Update workloads to mount the new Secret.
4. Delete the old raw Secret.
5. Block raw `Secret` creation with Kyverno / OPA.

Don't skip step 5; otherwise someone will re-create the old Secret in a panic.
