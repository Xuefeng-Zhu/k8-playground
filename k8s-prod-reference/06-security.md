# Part VI — Security

Production security is layered: identity, isolation, supply chain, runtime. Kubernetes provides the primitives; you compose them.

## 6.1 The threat model

What you're defending against:

1. **Compromised Pod.** An attacker has RCE in your container. Goal: prevent lateral movement, data theft, persistence.
2. **Compromised credentials.** A leaked ServiceAccount token, a stolen kubeconfig. Goal: minimise blast radius.
3. **Compromised supply chain.** Malicious image, malicious dependency. Goal: verify, sign, scan.
4. **Compromised node.** Attacker has root on a node. Goal: workload isolation makes this harder.
5. **Compromised operator.** A malicious or careless human with cluster admin. Goal: RBAC, audit, break-glass procedures.
6. **Compromised cloud account.** IAM credentials for the cluster. Goal: protect the cloud boundary, not the cluster.

Kubernetes security = defence in depth. No single layer is enough.

## 6.2 Pod Security Standards (PSS)

PSS replaced PodSecurityPolicy (removed in 1.25). Three levels:

| Level | What it allows | Use |
|-------|----------------|-----|
| **Privileged** | Anything | System/infra Pods only |
| **Baseline** | Blocks most escalations | Default for non-prod |
| **Restricted** | Hardened best practice | **Default for production** |

Apply at namespace level via labels:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: prod
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

`enforce` rejects; `audit` records violations in audit log; `warn` shows `kubectl` warning.

### What `restricted` enforces

- `runAsNonRoot: true`
- `seccompProfile.type: RuntimeDefault` (or `Localhost` with config)
- `allowPrivilegeEscalation: false`
- `capabilities.drop: [ALL]`
- `readOnlyRootFilesystem: true` (best practice, not strictly enforced)

Run with `runAsUser` set explicitly; rely on fsGroup for shared writable volumes.

## 6.3 RBAC — Role-Based Access Control

### The model

- Subjects (users, groups, ServiceAccounts) → Roles → Verbs → Resources → Namespaces (or cluster-wide).
- `Role` + `RoleBinding` for namespaced; `ClusterRole` + `ClusterRoleBinding` for cluster.

### Production RBAC patterns

**Service accounts per workload.** Not one SA per namespace; one SA per app, scoped tight.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: api-server
  namespace: prod
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: api-server
  namespace: prod
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
    resourceNames: ["api-server-config"]
```

**Don't use the default SA.** Mount a specific one (`spec.serviceAccountName`).

**Humans get cluster-scoped roles sparingly.** Most engineers need namespace-level access. Cluster-admin is rare and audited.

**Aggregation and discovery.** ClusterRoles can be aggregated with `aggregationRule.clusterRoleSelectors`. Use this to build composable permission bundles.

### Tools for RBAC hygiene

- **rbac-tool**, **rakkess** — show what a user can do.
- **kubectl-who-can** — "who can delete pods in X?"
- **kubescape** — RBAC review.

## 6.4 ServiceAccounts and tokens

### The token lifecycle change

Pre-1.24: every SA got a long-lived bearer token, auto-mounted into Pods.
1.24+: tokens are **bound to the lifetime of the Pod** via projected service account tokens (TokenRequest API). Old behavior is opt-in via `kubernetes.io/service-account-token` Secret type.

**Production rule:** explicitly set `automountServiceAccountToken: false` on Pods that don't need API access. Default mount is a leak.

### Audience-scoped tokens

Tokens can be scoped to specific audiences (`--audience` in TokenRequest). Used for:
- IRSA on AWS (Pod's token → AWS IAM role).
- GKE Workload Identity.
- Azure Workload Identity.

The cloud providers verify the token signature and the audience matches. Pod assumes IAM role with no static AWS keys.

### Token security

- Tokens are JWTs signed by the kube-apiserver. Anyone with the token can call the API.
- `kubectl exec` over a websocket uses the token.
- Tokens are short-lived (default 1h) — good. But if an attacker steals one, they have an hour.

## 6.5 Secrets management

### The problem

Kubernetes Secrets are:
- Stored in etcd, base64-encoded (not encrypted by default).
- Available to anyone with `get secrets` RBAC.
- Exposed to Pods via env vars or files.

For real secret management, use one of:

1. **Encrypt Secrets at rest in etcd** (`EncryptionConfiguration`). All major providers do this by default; verify.
2. **External secret stores.** Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault.
3. **Sealed Secrets** (Bitnami), **External Secrets Operator** (syncing from cloud secret stores).
4. **Direct CSI driver** for Vault (`secrets-store.csi.k8s.io`).

**Pattern: External Secrets Operator.** Declare the desired Kubernetes Secret; ESO fetches from cloud KMS / Vault / Parameter Store and syncs. Secret rotation is automatic.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-creds
spec:
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: db-creds
  data:
    - secretKey: password
      remoteRef:
        key: prod/db
        property: password
```

### Don't put secrets in env vars

Env vars leak to:
- `kubectl describe pod` output (visible to anyone with read).
- Crash dumps.
- Child process arguments.
- Logs that include `os.Environ()`.

Mount secrets as files (volume mount). Env vars are acceptable only for tightly-controlled CI build-time injection.

## 6.6 Network isolation

(Detailed in Part III, summarised here for the security context.)

- **Default-deny NetworkPolicy in every namespace.**
- **Egress policies** to block data exfil.
- **Separate namespaces by trust boundary.** Untrusted workloads in their own namespace with their own policies.

## 6.7 Image supply chain

### Image registries

Use private registries (ECR, GCR, ACR, Harbor, Quay). Public Docker Hub is a soft target; don't rely on it for production images.

### Image scanning

- **Build-time:** Trivy, Grype, Snyk in CI.
- **Registry-time:** ECR scan, Harbor Clair.
- **Admission-time:** **Kyverno**, **OPA/Gatekeeper**, **Connaisseur** — reject Pods using unscanned or vulnerable images.

### Image signing and verification

- **Sigstore/Cosign** — sign images, verify at admission. `cosign verify` against a public key or Fulcio-issued cert.
- **Notary v2** — older, less momentum now.
- **In-toto attestations** — provenance (SLSA).

```yaml
# Kyverno policy: only signed images
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-image-signature
spec:
  validationFailureAction: Enforce
  rules:
    - name: verify-signature
      match:
        resources:
          kinds: ["Pod"]
      verifyImages:
        - imageReferences: ["ghcr.io/myorg/*"]
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      ...
                      -----END PUBLIC KEY-----
```

### SLSA levels

- **Level 0:** No guarantees.
- **Level 1:** Provenance exists.
- **Level 2:** Signed provenance, host-source verified.
- **Level 3:** Hardened build platform.

Aim for at least Level 2 for production images. Use Tekton Chains, SLSA GitHub Generator, or Sigstore-based provenance.

## 6.8 Runtime security

Once a Pod is running, what protects the node and the cluster?

- **AppArmor / SELinux / seccomp** — kernel-level confinement. AppArmor profiles via annotations; seccomp via `securityContext.seccompProfile`.
- **gVisor, Kata Containers** — user-space kernel; isolate the container's syscalls. Trade-off: performance overhead.
- **Falco** — runtime threat detection. Watches syscalls, k8s audit, network. Flags anomalies (unexpected exec, file writes, network connections).
- **Tracee** — eBPF-based alternative to Falco.

### Production runtime rules

- Drop all capabilities, add only what's needed (`capabilities.drop: [ALL]`).
- Set `readOnlyRootFilesystem: true` wherever possible; use emptyDir for writable scratch.
- `runAsNonRoot: true` with explicit `runAsUser`.
- Use `seccompProfile.type: RuntimeDefault` (not `Unconfined`).
- For sensitive workloads, run a sandboxed runtime (gVisor).

## 6.9 Audit logging

The API server logs every request. Send this to your SIEM.

Key audit policy decisions:
- Log request and response bodies? Heavy, but valuable for forensics.
- What to exclude (kubelet heartbeats? anonymous health checks?).
- Retention (PCI/SOC2 may require 1+ years).

In managed clusters, audit logs go to cloud logging (CloudWatch, Cloud Logging, Log Analytics). In self-managed, you run audit policy + ship to your log store.

## 6.10 Compliance frameworks (high level)

| Framework | What it requires from K8s |
|-----------|---------------------------|
| **SOC 2** | RBAC, audit logging, secret encryption |
| **PCI-DSS** | Network segmentation, secrets at rest, vulnerability mgmt |
| **HIPAA** | Encryption, access controls, audit |
| **ISO 27001** | Risk assessment, policies, controls |
| **FedRAMP** | FIPS-validated crypto, STIG hardening, multi-factor auth |

Map controls to Kubernetes primitives in a written matrix. Auditors will ask.

## 6.11 Production failure modes (Part VI scope)

- **Default SA in use.** Every Pod has the same token; breach blast radius is cluster-wide.
- **ServiceAccount token auto-mounted on Pods that don't need it.** Token leak via env, crash dump, etc.
- **NetworkPolicy not enforced (Flannel).** All Pods reachable from all Pods.
- **Secrets as env vars.** Visible in describe, in crash dumps.
- **Privileged containers in production.** The original sin.
- **No image scanning.** Known CVEs running in your cluster.
- **No admission control.** Anyone can deploy anything.
- **Cluster-admin to many humans.** Audit noise; compromise of any one breaks everything.
- **Audit logs disabled or shipped nowhere.** You won't know what happened until it's too late.

## 6.12 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| PSS `restricted` everywhere | Production | Legacy Pods that need privileges (use a dedicated namespace) |
| Sealed Secrets | GitOps, no cloud KMS | Need short-lived dynamic secrets |
| External Secrets Operator | Cloud-native secret sources | Pure on-prem without cloud |
| Vault + CSI driver | Many secrets, dynamic, audit-heavy | Small deployments |
| Cosign image signing | Mature supply chain requirements | Early-stage projects |
| Kyverno vs OPA | Kyverno: simpler. OPA: more powerful | Pick one; don't mix policies |
| Falco | Runtime threat detection | You can't operate it (alerting, tuning) |
| gVisor / Kata | High-sensitivity workloads | Performance-sensitive workloads |

## 6.13 Further reading

- [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Encrypting Confidential Data at Rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/)
- [External Secrets Operator](https://external-secrets.io/)
- [Kyverno](https://kyverno.io/)
- [Falco](https://falco.org/)
- [Sigstore / Cosign](https://docs.sigstore.dev/)
- [OWASP Kubernetes Top 10](https://owasp.org/www-project-kubernetes-top-ten/)