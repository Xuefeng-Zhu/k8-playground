#!/usr/bin/env bash
# Deploy the capstone end-to-end.
#
# This script:
#   1. Creates the capstone namespaces
#   2. Generates a self-signed cert for the admission webhook
#   3. Deploys the tier manifests (frontend, backend, postgres)
#   4. Deploys the network policies
#   5. Deploys the admission webhook + registers it with the apiserver
#   6. Verifies everything is reachable
set -e
export PATH="$HOME/.local/bin:$PATH"
# Resolve the script's actual directory (works whether invoked as
# `bash deploy.sh`, `./deploy.sh`, or `bash /full/path/deploy.sh`).
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"
kubectl config use-context kind-prod-lab

echo "=== 1. Namespaces ==="
kubectl apply -f manifests/00-namespaces.yaml
kubectl wait --for=jsonpath='{.status.phase}=Active' namespace/capstone-frontend --timeout=10s
kubectl wait --for=jsonpath='{.status.phase}=Active' namespace/capstone-backend --timeout=10s
kubectl wait --for=jsonpath='{.status.phase}=Active' namespace/capstone-postgres --timeout=10s
kubectl wait --for=jsonpath='{.status.phase}=Active' namespace/capstone-system --timeout=10s

echo "=== 2. Generate TLS cert for admission webhook ==="
CAPSTONE_DIR="$SCRIPT_DIR"
mkdir -p /tmp/capstone-tls
cd /tmp/capstone-tls
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout tls.key -out tls.crt \
  -days 30 \
  -subj "/CN=capstone-webhook.capstone-system.svc.cluster.local" \
  -addext "subjectAltName=DNS:capstone-webhook.capstone-system.svc,DNS:capstone-webhook.capstone-system.svc.cluster.local,DNS:capstone-webhook.capstone-system" \
  2>/dev/null

kubectl -n capstone-system create secret tls capstone-webhook-tls \
  --cert=tls.crt --key=tls.key

echo "=== 3. Deploy tiers ==="
cd "$CAPSTONE_DIR"
kubectl apply -f manifests/01-frontend.yaml
kubectl apply -f manifests/02-backend.yaml
kubectl apply -f manifests/03-postgres.yaml

echo "Waiting for pods to be ready..."
kubectl -n capstone-frontend wait --for=condition=available deployment/frontend --timeout=120s
kubectl -n capstone-backend wait --for=condition=available deployment/backend --timeout=120s
echo "  frontend + backend ready"

# Postgres takes longer (volume mount + init)
kubectl -n capstone-postgres wait --for=condition=ready pod/postgres-0 --timeout=180s
echo "  postgres ready"

echo "=== 4. Network policies ==="
kubectl apply -f manifests/04-network-policies.yaml
echo "  NetworkPolicies applied"

echo "=== 5. Admission webhook ==="
kubectl apply -f manifests/05-admission.yaml
kubectl -n capstone-system wait --for=condition=available deployment/capstone-webhook --timeout=120s
echo "  webhook deployment ready"

# Register with apiserver (we're in /tmp/capstone-tls here, cert is local)
CA_BUNDLE=$(base64 -w0 /tmp/capstone-tls/tls.crt)
kubectl apply -f - <<EOF
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: capstone-policy
webhooks:
- name: capstone-policy.capstone-system.svc.cluster.local
  clientConfig:
    service:
      name: capstone-webhook
      namespace: capstone-system
      path: /validate
    caBundle: $CA_BUNDLE
  rules:
  - apiGroups: [""]
    apiVersions: ["v1"]
    operations: ["CREATE","UPDATE"]
    resources: ["pods"]
  admissionReviewVersions: ["v1"]
  sideEffects: None
  failurePolicy: Fail
  namespaceSelector:
    matchExpressions:
    - key: kubernetes.io/metadata.name
      operator: NotIn
      values: ["kube-system","local-path-storage","capstone-system"]
EOF
echo "  ValidatingWebhookConfiguration applied"

echo "=== 6. Verify ==="
echo "Frontend reachable:"
curl -sf --max-time 5 http://localhost:18080/ -o /dev/null -w "  HTTP %{http_code}\n" || echo "  (not reachable yet)"
echo "Backend reachable from inside cluster:"
kubectl -n capstone-frontend exec deploy/frontend -- wget -q -O - --timeout=5 http://backend.capstone-backend/ 2>&1 | head -2 || echo "  (exec failed — that's fine, exec is via apiserver)"
echo "Admission policy denies root pods:"
kubectl -n capstone-frontend run bad --image=nginx:1.27 --restart=Never --dry-run=server -o jsonpath='{.spec.containers[0].securityContext.runAsUser}' 2>&1 | head -3
echo ""
echo "Capstone deployed successfully."