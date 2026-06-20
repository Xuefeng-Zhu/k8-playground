# 3. Kyverno

**Solves:** Policy-as-code for Kubernetes. Validates, mutates, and generates resources at admission time. The production alternative to OPA/Gatekeeper for most teams — Kyverno uses K8s-native YAML (no Rego), is CNCF-graduated, and integrates cleanly with GitOps.

**When you need it:** Multi-tenant clusters, compliance frameworks (SOC2, PCI), preventing known-bad patterns (`:latest` tags, privileged containers, unsigned images) before they hit etcd.

## 3.1 Install

```bash
helm repo add kyverno https://kyverno.github.io/kyverno
helm repo update

helm upgrade --install kyverno kyverno/kyverno \
  --namespace kyverno \
  --create-namespace \
  --values - <<'EOF'
installCRDs: true

# HA — background controller scans must keep up with API traffic
backgroundController:
  enabled: true
  replicaCount: 3
  resources:
    requests: { cpu: 50m, memory: 128Mi }
    limits:   { cpu: 500m, memory: 512Mi }

cleanupController:
  enabled: true
  replicaCount: 2

reportsController:
  enabled: true
  replicaCount: 2

# Webhook is the API for admission — if it's down, no Pods create
admissionController:
  replicaCount: 3
  resources:
    requests: { cpu: 100m, memory: 256Mi }
    limits:   { cpu: 1,    memory: 1Gi   }
  # Failure policy — Fail is safer, Ignore keeps cluster alive if Kyverno is down
  # Choose Fail for compliance-critical clusters; Ignore for dev where availability > safety

securityContext:
  runAsNonRoot: true
  seccompProfile: { type: RuntimeDefault }

podDisruptionBudget:
  enabled: true
  minAvailable: 2

serviceMonitor:
  enabled: true
EOF
```

## 3.2 The four policy types

Before the policies: **what can Kyverno do?**

| Type | What | Example |
|------|------|---------|
| `validate` | Allow / deny the resource | "Reject Pods with `:latest`" |
| `mutate` | Modify the resource on creation | "Inject a default `runAsNonRoot` if missing" |
| `generate` | Create sibling resources | "Auto-create a default NetworkPolicy per namespace" |
| `verifyImages` | Verify image signatures/attestations | "Only allow images signed by Sigstore" |

## 3.3 The 8 policies every prod cluster needs

### 1. Block `:latest` tags and unpinned images

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: deny-latest
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: >-
          Image tags must be pinned (not 'latest', not empty).
          Use a digest or explicit version.
        pattern:
          spec:
            containers:
              - image: "!*:latest"
              - image: "!latest"
              - image: "!*:"
    - name: deny-missing-tag
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "Image must have an explicit tag"
        pattern:
          spec:
            containers:
              - image: "*:*"
```

### 2. Block privileged containers

```yaml
apiVersion: kyverno.io/v2
kind: ClusterPolicy
metadata:
  name: restrict-privileged
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: deny-privileged
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: >-
          Privileged containers are forbidden.
          Use specific capabilities (NET_ADMIN, SYS_TIME) if needed.
        pattern:
          spec:
            containers:
              - securityContext:
                  privileged: "false|nil"
```

### 3. Enforce runAsNonRoot + readOnlyRootFilesystem + drop ALL capabilities

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-security-context
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: require-nonroot
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "All containers must run as non-root."
        pattern:
          spec:
            containers:
              - securityContext:
                  runAsNonRoot: true
    - name: drop-all-caps
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "All Linux capabilities must be dropped."
        anyPattern:
          - spec:
              containers:
                - securityContext:
                    capabilities:
                      drop: ["ALL"]
          - spec:
              containers:
                - securityContext: {}
```

### 4. Require resource requests AND limits

```yaml
apiVersion: kyverno.io/v2
kind: ClusterPolicy
metadata:
  name: require-resources
spec:
  validationFailureAction: Audit          # Audit, not Enforce, while you migrate
  rules:
    - name: cpu-and-memory-required
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "Containers must set CPU and memory requests AND limits."
        pattern:
          spec:
            containers:
              - resources:
                  requests:
                    memory: "?*"
                    cpu: "?*"
                  limits:
                    memory: "?*"
                    cpu: "?*"
```

### 5. Enforce image registry allowlist

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: restrict-image-registries
spec:
  validationFailureAction: Enforce
  rules:
    - name: allowed-registries
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: >-
          Images must come from an approved registry:
          ghcr.io/myorg/*, 123456789012.dkr.ecr.us-east-1.amazonaws.com/*
        pattern:
          spec:
            containers:
              - image: "ghcr.io/myorg/* | 123456789012.dkr.ecr.us-east-1.amazonaws.com/*"
```

### 6. Verify image signatures (Cosign / Sigstore)

**First, set up the public key:**

```bash
# Extract the public key from your signing key
cosign public-key --key cosign.key > cosign.pub

# Create a ConfigMap with the key
kubectl create configmap cosign-pub \
  --from-file=cosign.pub=cosign.pub \
  -n kyverno
```

**Then the policy:**

```yaml
apiVersion: kyverno.io/v2
kind: ClusterPolicy
metadata:
  name: verify-image-signatures
spec:
  validationFailureAction: Enforce
  rules:
    - name: verify-cosign
      match:
        any:
          - resources:
              kinds: ["Pod"]
      verifyImages:
        - imageReferences:
            - "ghcr.io/myorg/*"
            - "123456789012.dkr.ecr.us-east-1.amazonaws.com/*"
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
                      -----END PUBLIC KEY-----
```

For keyless verification (Sigstore Fulcio + Rekor):

```yaml
verifyImages:
  - imageReferences: ["ghcr.io/myorg/*"]
    attestors:
      - entries:
          - keyless:
              issuer: "https://token.actions.githubusercontent.com"
              subject: "https://github.com/myorg/myrepo/.github/workflows/release.yml@refs/tags/v*"
              rekor:
                url: "https://rekor.sigstore.dev"
```

### 7. Default-deny + required labels (mutate + validate combo)

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-labels
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-owner
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "All Pods must have an 'owner' label."
        pattern:
          metadata:
            labels:
              owner: "?*"
    - name: mutate-add-owner
      match:
        any:
          - resources:
              kinds: ["Pod"]
              # Only mutate in dev namespaces where owner is missing
      mutate:
        patchStrategicMerge:
          metadata:
            labels:
              +(owner): "unknown"
```

### 8. Auto-generate default NetworkPolicy per namespace

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: generate-default-deny
spec:
  rules:
    - name: deny-all-ingress
      match:
        any:
          - resources:
              kinds: ["Namespace"]
              # Only generate for new namespaces
      generate:
        kind: NetworkPolicy
        name: default-deny-ingress
        namespace: "{{request.object.metadata.name}}"
        synchronize: true          # Kyverno keeps this in sync if someone edits it
        data:
          spec:
            podSelector: {}
            policyTypes: ["Ingress"]
```

## 3.4 Namespace exemptions (the carve-out mechanism)

Not every workload can comply with all rules. Use `exclusions` in the policy:

```yaml
apiVersion: kyverno.io/v2
kind: ClusterPolicy
metadata:
  name: restrict-privileged
spec:
  rules:
    - name: deny-privileged
      exclude:
        any:
          - namespaces: ["kube-system", "monitoring", "falco", "gatekeeper-system"]
          - resources:
              namespaces: ["cert-manager"]
              # cert-manager needs some privileges for the webhook
```

## 3.5 Verify

```bash
# What policies are active?
kubectl get cpol
# NAME                          ADMISSION   BACKGROUND   VALIDATE ACTION   READY
# disallow-latest-tag           true        true         Enforce           True
# restrict-privileged           true        true         Enforce           True

# What violations exist?
kubectl get policyreport -A
# NAMESPACE   NAME                    PASS   FAIL   WARN   ERROR
# prod        polr-prod-api-server-xyz 12    1      0      0

# Detailed report for one
kubectl describe policyreport polr-prod-api-server-xyz -n prod

# Live test — would this be blocked?
kubectl run test --image=foo:latest --dry-run=server
# Error from server: admission webhook "validate.kyverno.svc-fail" denied the request:
# resource Pod/default/test was blocked due to the following policies
# disallow-latest-tag: deny-latest: Image tags must be pinned...

# Controller logs
kubectl logs -n kyverno -l app.kubernetes.io/component=background-controller --tail=100
```

## 3.6 Production gotchas

### ValidationFailureAction: Enforce vs Audit

- `Enforce`: blocks the request. Use for hard rules (privileged, signed images, registry allowlist).
- `Audit`: records violation but allows. Use while you migrate existing workloads.
- Setting everything to Enforce without an audit phase = instant outage on rollout. **Always Audit first.**

### Background vs Admission

Background scanning catches violations in resources created *before* the policy was applied. Background=true for every "deny" policy. Without it, you only catch new violations.

### PolicyReport is gold

`kubectl get policyreport -A` is your single best view of cluster compliance. Wire it into Grafana — count violations per policy per namespace, alert on sudden spikes.

### The webhook timeout trap

Kyverno's webhook must respond in <10s (the API server default). If Kyverno is overloaded, every Pod creation in the cluster hangs. **Set `--failurePolicy=Fail` (default), but monitor:**

```promql
# Kyverno admission latency p99 — alert > 5s
histogram_quantile(0.99,
  rate(kyverno_admission_review_seconds_bucket[5m])
)
```

### Kyverno is itself a privileged workload

Kyverno needs to be able to mutate and validate *anything*. Compromise of Kyverno = compromise of the cluster. Lock it down:
- NetworkPolicies to allow only kube-apiserver traffic.
- Don't give the kyverno SA cluster-admin unless a policy requires it (most don't).
- Run on dedicated nodes with taints.

### OPA/Gatekeeper vs Kyverno

OPA is more powerful (Rego can express anything), Kyverno is more ergonomic (YAML matches what you already know). For ~90% of policies, Kyverno wins on DX. For complex logic (cryptographic checks, multi-resource constraints), OPA wins. You can run both.

### Update carefully

Kyverno policies can break existing workloads on rollout. The safe pattern:

1. Apply policy with `validationFailureAction: Audit`.
2. Watch `kubectl get policyreport -A` for violations.
3. Fix the workloads.
4. Switch to `Enforce`.

### Testing policies

Use `kyverno test` to unit-test policies against fixtures:

```bash
# test.yaml
---
policies:
  - disallow-latest-tag
resources:
  - Pod:
      - name: good-pod
        spec:
          containers:
            - image: "myorg/app:1.4.2"
      - name: bad-pod
        spec:
          containers:
            - image: "myorg/app:latest"
results:
  - policy: disallow-latest-tag
    rule: deny-latest
    resource: bad-pod
    kind: Pod
    status: fail
```

```bash
kyverno test test.yaml
```

Add this to your policy repo's CI.

### Don't put secrets in policies

Kyverno configs end up in Git. If you embed secrets in a policy (you shouldn't), they're in your audit trail. Use `exclude` blocks or `match` block exemptions rather than embedding values.
