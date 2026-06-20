# 1. cert-manager

**Solves:** Automated TLS certificate issuance and renewal inside Kubernetes. Removes the manual `openssl` workflow for ingress TLS, supports public CAs (Let's Encrypt, DigiCert, ZeroSSL) and internal CAs (Vault PKI, CFSSL).

**When you need it:** Any cluster with HTTPS ingress. There is no production alternative worth the operational cost.

## 1.1 Install

Helm is the supported install path. The CRDs ship separately so you can manage them with your GitOps tool.

```bash
# Add the Jetstack repo
helm repo add jetstack https://charts.jetstack.io
helm repo update

# Install CRDs first (they are huge — 100+ resources)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.0/cert-manager.crds.yaml

# Install the chart with production-grade values
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.15.0 \
  --values - <<'EOF'
installCRDs: false    # we installed them manually above
replicaCount: 3       # HA — important for renewal during upgrades

resources:
  requests: { cpu: 50m, memory: 64Mi }
  limits:   { cpu: 200m, memory: 256Mi }

# Pod-level security
securityContext:
  runAsNonRoot: true
  seccompProfile: { type: RuntimeDefault }

# Pod disruption budget — renewal should not stop during node drains
podDisruptionBudget:
  enabled: true
  minAvailable: 1

# Metrics for Prometheus
prometheus:
  enabled: true
  servicemonitor:
    enabled: true

# Webhook is the API for CertificateRequests; if it's down, no certs
webhook:
  replicaCount: 3
  resources:
    requests: { cpu: 50m, memory: 64Mi }
    limits:   { cpu: 200m, memory: 256Mi }
EOF
```

## 1.2 ClusterIssuers — the production-grade config

A `ClusterIssuer` is cluster-scoped; an `Issuer` is per-namespace. Use ClusterIssuer unless you have a reason not to.

### ACME with Let's Encrypt + Route53 (DNS-01, supports wildcards)

DNS-01 is the right choice for production. HTTP-01 requires port 80 inbound; DNS-01 just needs an IAM role that can update Route53 records.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    # Let's Encrypt production. Staging at https://acme-staging-v02.api.letsencrypt.org/directory
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@yourorg.com        # receives expiry warnings — make this a list
    # ACME account private key — managed by cert-manager, persisted as a Secret
    privateKeySecretRef:
      name: letsencrypt-prod-account
    solvers:
      - dns01:
          route53:
            region: us-east-1
            # IAM role created via IRSA — see "IRSA setup" below
            role: arn:aws:iam::${AWS_ACCOUNT_ID}:role/cert-manager-route53
        selector:
          # Only use DNS-01 for wildcard certs — leave room for HTTP-01 if you want
          dnsZones:
            - "yourorg.com"
```

**IRSA setup** (one-time, per cluster):

```bash
# 1. Create the IAM policy with the minimum Route53 permissions
cat > cert-manager-route53-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "route53:GetChange",
      "Resource": "arn:aws:route53:::change/*"
    },
    {
      "Effect": "Allow",
      "Action": ["route53:ChangeResourceRecordSets","route53:ListResourceRecordSets"],
      "Resource": "arn:aws:route53:::hostedzone/*"
    }
  ]
}
EOF
aws iam create-policy --policy-name cert-manager-route53 --policy-document file://cert-manager-route53-policy.json

# 2. Create the IRSA role with OIDC trust
eksctl create iamserviceaccount \
  --cluster=${CLUSTER_NAME} \
  --namespace cert-manager \
  --name cert-manager \
  --role-name cert-manager-route53 \
  --attach-policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/cert-manager-route53 \
  --approve
```

### ACME with Cloudflare (simpler — global API token)

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-cloudflare
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@yourorg.com
    privateKeySecretRef: { name: letsencrypt-cf-account }
    solvers:
      - dns01:
          cloudflare:
            # Reference the API token stored as a Secret — never put the token inline
            apiTokenSecretRef:
              name: cloudflare-api-token
              key: api-token
```

The Secret holding the token (apply via ESO or sealed-secrets, not raw):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-api-token
  namespace: cert-manager
type: Opaque
stringData:
  api-token: "${CLOUDFLARE_API_TOKEN}"
```

### Internal CA (Vault PKI)

For services inside the cluster that don't need public certs:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: vault-internal
spec:
  vault:
    server: https://vault.vault-system.svc:8200
    # The token has limited permissions; rotate it via Vault
    auth:
      kubernetes:
        mountPath: /v1/auth/kubernetes
        role: cert-manager
        # ServiceAccount that's bound to the Vault role
        serviceAccountRef:
          name: cert-manager-vault-auth
    # Path in Vault where the PKI engine lives
    path: pki-internal/sign
```

## 1.3 Certificates — the resource you actually use

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: api-server-tls
  namespace: prod
spec:
  secretName: api-server-tls         # the TLS secret name (consumed by Ingress)
  duration: 2160h                    # 90 days — the default, but be explicit
  renewBefore: 720h                  # renew 30 days before expiry
  issuerRef:
    name: letsencrypt-prod           # ClusterIssuer name
    kind: ClusterIssuer
    group: cert-manager.io
  dnsNames:
    - api.yourorg.com
    - "*.api.yourorg.com"            # wildcard — needs DNS-01
  # Optional: store the cert in a different namespace from where it's used
  secretTemplate:
    annotations:
      # Helps external-secrets / reflection tools
      reflectors.app/v1.kube-version: "v1"
```

**Wildcard cert gotcha:** ACME wildcard requires DNS-01. You cannot get a wildcard with HTTP-01. Make sure your `ClusterIssuer` has a DNS-01 solver before requesting a wildcard.

## 1.4 Ingress integration — the missing piece

cert-manager can annotate Ingress objects so certs are created automatically when the Ingress appears:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-server
  namespace: prod
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    # Use this if you have both prod and staging ClusterIssuers
    # cert-manager.io/issuer: letsencrypt-prod
    acme.cert-manager.io/http01-edit-in-place: "true"   # only for HTTP-01
spec:
  tls:
    - hosts:
        - api.yourorg.com
      secretName: api-server-tls
  rules:
    - host: api.yourorg.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: api-server, port: { number: 80 } } }
```

## 1.5 Verify

```bash
# Is the issuer ready?
kubectl get clusterissuer
# NAME                  READY   AGE
# letsencrypt-prod      True    5d

# What certs exist and when do they renew?
kubectl get certificate -A
# NAME                READY   SECRET              AGE
# api-server-tls      True    api-server-tls      12d

# Detailed status of one cert (look for "Renewal Time")
kubectl describe certificate api-server-tls -n prod

# Has the renewal Challenge succeeded?
kubectl get challenges -A

# Check the actual Secret has the right data
kubectl get secret api-server-tls -n prod -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -subject -dates

# View controller logs for renewal failures
kubectl logs -n cert-manager -l app=cert-manager --tail=200 | grep -i error
```

## 1.6 Production gotchas

### Rate limits

Let's Encrypt has rate limits: **50 certs per registered domain per week**, **5 duplicate certs per week**, **5 accounts per IP per 3 hours**. Hit these in a CI loop that creates Ingresses per build and you'll lock yourself out for a week. Mitigations:
- Always use the staging issuer in CI / preview environments.
- Reuse existing certs across Ingresses with the same hostname (use `secretName` not new auto-names).
- Track issuance with Prometheus: `certmanager_certificate_issuance_total{status="success"}`.

### Renewal failures

Renewal fails when:
- The DNS provider credentials rotate out from under cert-manager.
- A network rule blocks egress to Let's Encrypt.
- The CAA record forbids issuance.

cert-manager retries with backoff but eventually the cert expires and your Ingress serves an expired cert — silently to most clients, noisily to security scanners. **Alert on `certmanager_certificate_expiration_timestamp_seconds - time() < 21d` (21 days = 30-day renewBefore should still have 9 days left).**

### The ACME account private key

cert-manager creates the account on first use. If you delete the Secret, the next issuance creates a new account — and may hit the 5-accounts-per-IP rate limit. **Don't delete the account Secret.**

### Cross-namespace cert sharing

A Certificate in namespace `A` writes to a Secret in namespace `A`. If your Ingress is in namespace `B`, you need to either:
- Copy the Secret (don't; secrets are 2nd-class citizens in K8s)
- Use `cert-manager-csi-driver-spiffe` to project the cert into Pods without a Secret
- Reference the Secret via `secretName` even cross-namespace (works for Ingress in some controllers — verify your ingress-nginx version)

### HTTP-01 + NGINX Ingress gotcha

For HTTP-01, cert-manager needs port 80 reachable from Let's Encrypt. If you have an external L4 LB that doesn't pass port 80 through, DNS-01 is your only option. Document this in your incident response — "TLS renewals failing" + "firewall change last week" is a common pairing.

### Multiple ingress controllers

If you have multiple ingress controllers, the `cert-manager.io/cluster-issuer` annotation only fires for Ingresses that match the right ingress-class. Make sure your Ingress has `spec.ingressClassName: nginx` (or whatever).

### cert-manager version skew

cert-manager has had breaking changes. Don't skip more than one minor version on upgrade. Read the upgrade notes: https://cert-manager.io/docs/releases/

### Debugging checklist

When a cert won't issue:
1. `kubectl describe challenge` — most failures are here.
2. `kubectl logs -n cert-manager deploy/cert-manager --tail=100` — controller logs.
3. `kubectl get events --sort-by='.lastTimestamp' -n <namespace>` — issuance events.
4. Test DNS propagation: `dig TXT _acme-challenge.example.com @8.8.8.8`
5. Test IAM: `aws sts assume-role-with-web-identity --role-arn <arn> --web-identity-token <token>` (IRSA path)
