# Lab 05 — A malicious image vs. your admission webhook

**Chapter tie-in:** `06-security.md` — RBAC, ServiceAccounts, admission control, supply chain.
**Time:** ~40 min.
**Cluster:** `kind-prod-lab`.

---

## Goal

Stand up a real admission webhook, then try to deploy a container that
violates its policy. Learn the difference between *what the AI agent
asks the cluster to do* and *what the apiserver actually accepts*, and
why admission control is the place where the AI's output is bound.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab
kubectl create namespace lab-05
```

## Background — two kinds of admission

There are two ways to enforce admission policy in modern Kubernetes:

1. **Built-in `ValidatingAdmissionPolicy` (VAP, K8s 1.26+, GA 1.30)** —
   CEL expressions inline in a CRD. No extra process. Powerful but
   requires the apiserver to have the plugin enabled, which is *not
   the default on every distribution* (kind does NOT enable it by
   default — see the gotcha below).
2. **Admission webhooks (`ValidatingAdmissionWebhook`)** — a Service
   the apiserver calls over HTTPS for every matching request. Heavier
   to operate but works on every cluster.

We'll use approach 2 because (a) it's portable, (b) you'll see the
whole stack, and (c) it's still what most production clusters use.

## Step 1 — Write the webhook

> **Real bug I hit writing this lab:** a plain `HTTPServer` with an
> `SSLContext` *does not serve HTTPS*. You have to subclass and wrap
> the accepted socket manually, or use `ssl.wrap_socket` on a raw
> socket. The first version of this lab silently served plain HTTP
> on 8443, the apiserver's TLS handshake failed with "wrong version
> number", and every webhook call failed closed. Lesson: always
> test your webhook with `openssl s_client` from inside the pod
> before pointing an admission config at it.

```bash
mkdir -p /tmp/lab-05-webhook
cd /tmp/lab-05-webhook

cat > webhook.py <<'PYEOF'
#!/usr/bin/env python3
"""Minimal ValidatingAdmissionWebhook. Rejects:
  - containers with securityContext.runAsUser == 0
  - containers with securityContext.allowPrivilegeEscalation == true
  - any container without a securityContext at all
  - any hostPath volume
Skips kube-system, local-path-storage, and lab-05-system (so the
webhook itself can run).
"""
import json, ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

class TLSHTTPServer(HTTPServer):
    """HTTPS server that wraps each accepted socket with TLS."""
    def get_request(self):
        sock, addr = self.socket.accept()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain("/tls/tls.crt","/tls/tls.key")
        return ctx.wrap_socket(sock, server_side=True), addr

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length","0"))
        review = json.loads(self.rfile.read(n))
        req = review["request"]
        obj = req["object"]
        ns = obj["metadata"]["namespace"]
        uid = req["uid"]

        if ns in ("kube-system","local-path-storage","lab-05-system"):
            allowed = True
            reason = ""
        else:
            bad = []
            for c in obj["spec"].get("containers", []):
                if "securityContext" not in c:
                    bad.append(f"container '{c['name']}' has no securityContext")
                    continue
                sc = c["securityContext"] or {}
                if sc.get("runAsUser") == 0:
                    bad.append(f"container '{c['name']}' runs as root")
                if sc.get("allowPrivilegeEscalation") is True:
                    bad.append(f"container '{c['name']}' allows privilege escalation")
            for v in obj["spec"].get("volumes", []) or []:
                if "hostPath" in v:
                    bad.append(f"hostPath mount: {v['hostPath']['path']}")
            allowed = (len(bad) == 0)
            reason = "; ".join(bad)

        resp = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": allowed}
        }
        if not allowed:
            resp["response"]["status"] = {"message": reason}
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **kw): pass

TLSHTTPServer(("0.0.0.0", 8443), H).serve_forever()
PYEOF
```

**Sanity check the listener** before you wire it up to the apiserver
(this is the test that catches the plain-HTTP mistake above):

```bash
docker build -t webhook:lab-05 . 2>&1 | tail -2
kind load docker-image webhook:lab-05 --name prod-lab
# Run it locally to verify TLS works:
docker run --rm -p 18443:8443 -v /tmp/lab-05-webhook/tls.crt:/tls/tls.crt:ro \
  -v /tmp/lab-05-webhook/tls.key:/tls/tls.key:ro webhook:lab-05 &
echo | openssl s_client -connect 127.0.0.1:18443 -showcerts 2>&1 | head -10
# Expect: "Verify return code: 0 (ok)" and a negotiated TLS version.
kill %1
```

## Step 2 — Generate a self-signed cert

> **Layout note:** the webhook runs in `lab-05-system` (not `lab-05`),
> because the webhook's *own* pods must be exempt from its policy —
> otherwise it can't recreate itself. The cert SANs must match the
> Service FQDN `<svc>.<ns>.svc`, which is `webhook.lab-05-system.svc`.

```bash
cd /tmp/lab-05-webhook
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout tls.key -out tls.crt \
  -days 1 \
  -subj "/CN=webhook.lab-05-system.svc.cluster.local" \
  -addext "subjectAltName=DNS:webhook.lab-05-system.svc,DNS:webhook.lab-05-system.svc.cluster.local,DNS:webhook.lab-05-system" \
  2>/dev/null

# CA bundle the apiserver will use to verify our webhook's cert
CA_BUNDLE=$(base64 -w0 tls.crt)
echo "CA bundle: $CA_BUNDLE" | head -c 100
echo "..."
```

> **Gotcha:** if you forget `cluster.local` in the SAN, the apiserver's
> TLS handshake fails with "first record does not look like a TLS
> handshake" — same error as the plain-HTTP bug above, completely
> different cause. Always match `<svc>.<ns>.svc.cluster.local` exactly.

## Step 3 — Build the image and load it into kind

```bash
cat > Dockerfile <<'DEOF'
FROM python:3.12-alpine
RUN apk add --no-cache openssl
WORKDIR /app
COPY webhook.py .
EXPOSE 8443
CMD ["python","/app/webhook.py"]
DEOF

docker build -t webhook:lab-05 . 2>&1 | tail -3
kind load docker-image webhook:lab-05 --name prod-lab
```

## Step 4 — Deploy the webhook (in `lab-05-system`, NOT `lab-05`)

```bash
# Two-namespace layout:
#   lab-05-system — the webhook lives here, excluded from the policy
#   lab-05        — your test workloads, fully covered by the policy

kubectl create namespace lab-05-system
kubectl create namespace lab-05

cat <<EOF | kubectl -n lab-05-system apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: webhook-tls
  namespace: lab-05-system
type: kubernetes.io/tls
stringData:
  tls.crt: $(base64 -w0 /tmp/lab-05-webhook/tls.crt)
  tls.key: $(base64 -w0 /tmp/lab-05-webhook/tls.key)
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: webhook
  namespace: lab-05-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: webhook
  template:
    metadata:
      labels:
        app: webhook
    spec:
      containers:
      - name: webhook
        image: webhook:lab-05
        ports:
        - containerPort: 8443
        volumeMounts:
        - name: tls
          mountPath: /tls
          readOnly: true
        securityContext:
          runAsNonRoot: true
          runAsUser: 1000
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
          readOnlyRootFilesystem: true
      volumes:
      - name: tls
        secret:
          secretName: webhook-tls
---
apiVersion: v1
kind: Service
metadata:
  name: webhook
  namespace: lab-05-system
spec:
  selector:
    app: webhook
  ports:
  - port: 443
    targetPort: 8443
EOF

kubectl -n lab-05-system wait --for=condition=available deployment/webhook --timeout=90s
kubectl -n lab-05-system get pods -l app=webhook
```

> **Verify the webhook is actually serving TLS** before pointing the
> apiserver at it. This catches the plain-HTTP mistake:
> ```bash
> kubectl -n lab-05-system exec deploy/webhook -- python3 -c "
> import ssl, socket
> ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
> s = ctx.wrap_socket(socket.socket(), server_hostname='webhook')
> s.connect(('127.0.0.1', 8443))
> print('TLS version:', s.version())"
> # Expect: TLS version: TLSv1.2 or TLSv1.3
> ```

## Step 5 — Register the webhook with the apiserver

> **Important:** the `namespaceSelector` must exclude `lab-05-system`
> or the webhook can't recreate itself (chicken-and-egg). It also
> excludes the standard system namespaces.

```bash
cat <<EOF | kubectl apply -f -
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: pod-security-baseline
webhooks:
- name: pod-security.lab-05.svc.cluster.local
  clientConfig:
    service:
      name: webhook
      namespace: lab-05-system
      path: /validate
    caBundle: $(base64 -w0 /tmp/lab-05-webhook/tls.crt)
  rules:
  - apiGroups:   [""]
    apiVersions: ["v1"]
    operations:  ["CREATE","UPDATE"]
    resources:   ["pods"]
  admissionReviewVersions: ["v1"]
  sideEffects: None
  failurePolicy: Fail
  namespaceSelector:
    matchExpressions:
    - key: kubernetes.io/metadata.name
      operator: NotIn
      values: ["kube-system","local-path-storage","lab-05-system"]
EOF
```

Note `failurePolicy: Fail` — if the webhook is unreachable, pod
creation *fails*. That's the right default for security; in real prod
you might use `Ignore` for non-critical policies but you'd need to
alert on the failure separately.

The webhook name **must be a valid domain** (≥3 dot-separated
segments). `"pod-security.lab-05"` is rejected; `"pod-security.lab-05.svc.cluster.local"` works.

## Step 6 — Try to violate the policy

```bash
cat <<EOF | kubectl -n lab-05 apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: bad-pod
spec:
  containers:
  - name: app
    image: nginx:1.27
    securityContext:
      runAsUser: 0
      allowPrivilegeEscalation: true
EOF
```

**Observe:** the apiserver calls the webhook, the webhook returns
`allowed: false` with the reason, kubectl prints the message, and **no
pod is created.** No etcd write, no scheduler work. Verify:

```bash
kubectl -n lab-05 get pods   # no bad-pod
```

## Step 7 — A fix that *looks* compliant but isn't

This is the AI-failure scenario: an agent picks a manifest from its
training data, removes the explicit `runAsUser: 0`, but the upstream
image defaults to UID 0.

```bash
cat <<EOF | kubectl -n lab-05 apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: sneaky-pod
spec:
  containers:
  - name: app
    image: nginx:1.27
    # No securityContext at all — the image's USER directive is root
EOF
```

**Observe:** this *passes* our webhook (we only check explicit
`runAsUser`, not the image default). The pod starts as root. This is
the **coverage gap** every admission policy has — you can only enforce
what your CEL expression / Python code looks for. Real production
policies check more: Pod Security Standards `restricted` profile,
Kyverno `runAsNonRoot` rule (which checks the effective UID, not just
the declared one), image signature verification, allowed registries,
the list goes on.

Update the webhook to require a securityContext at minimum:

```bash
# Edit /tmp/lab-05-webhook/webhook.py and add to the 'bad' list:
#   if "securityContext" not in c: bad.append(f"'{c['name']}' has no securityContext")
docker build -t webhook:lab-05 /tmp/lab-05-webhook 2>&1 | tail -2
kind load docker-image webhook:lab-05 --name prod-lab
kubectl -n lab-05 rollout restart deployment/webhook
kubectl -n lab-05 rollout status deployment/webhook
```

Now try the sneaky pod again — rejected because no securityContext.

## Step 8 — The wider lesson: policies bind the agent

This is the **most important concept in lab 05**: admission control
decides what the cluster will accept, regardless of what any tool
asks for. An AI agent with `cluster-admin` can't deploy a root pod
because the apiserver won't write it. An AI agent editing a manifest
later — every CREATE/UPDATE goes through admission. The agent is
**bounded by what the cluster accepts**, not by what it knows to do.

This is exactly why production clusters pin `pod-security.kubernetes.io/enforce:
restricted` and use OPA/Kyverno. It's not about trust; it's about
*shrinking the surface area any actor (human, agent, script) can act
on*.

## Gotcha — `ValidatingAdmissionPolicy` (the modern alternative)

Kubernetes 1.26+ has a CRD-based `ValidatingAdmissionPolicy` that lets
you write CEL expressions inline — no webhook process needed. It's
cleaner but it requires the apiserver to have the plugin enabled.

```bash
# Check whether your cluster has it:
kubectl api-resources | grep validatingadmissionpolicy
# ^ if you see it, the CRD is registered. The PLUGIN may still be disabled.
docker exec prod-lab-control-plane \
  cat /etc/kubernetes/manifests/kube-apiserver.yaml | grep enable-admission
# On kind, you typically see: --enable-admission-plugins=NodeRestriction
# ValidatingAdmissionPolicy is missing → the CRD exists but won't enforce.
```

To enable it you'd need to patch the static pod manifest and restart
the apiserver — destructive on a running cluster. In production this
is a thing to verify at cluster bring-up: `kubectl get validatingadmissionpolicy`
followed by `kubectl dry-run` to see if policies are actually being
checked, not just stored.

## Restore

```bash
kubectl delete validatingadmissionwebhookconfiguration pod-security-baseline
kubectl delete namespace lab-05
rm -rf /tmp/lab-05-webhook
docker rmi webhook:lab-05 2>/dev/null
```

---

## Write up (`labs/postmortems/05-admission-vs-agent.md`)

1. **What's the difference between admission policy and RBAC?** RBAC
   controls *who* can act; admission controls *what* is allowed to exist.
   Why do you need both?
2. **Why is `failurePolicy: Fail` the right default for security
   policies?** When would `Ignore` be appropriate? What's the trade-off?
3. **What did step 7 reveal about policy coverage?** Policies have to
   anticipate every "looks fine but isn't" case. How do you discover
   the gaps in real life? (Hint: postmortems from real breaches,
   periodic table-top exercises, Kyverno policy libraries.)
4. **What's the difference between `ValidatingAdmissionWebhook` and
   `ValidatingAdmissionPolicy`?** When would you choose one over the
   other in production?
5. **How would an AI agent fail here?** The interesting failure isn't
   "agent bypasses policy" (it can't — that's the whole point). It's
   "agent suggests loosening the policy because it can't deploy the
   workload the user asked for." How do you detect that drift?

## Bonus: prod reading

- [Admission controllers reference](https://kubernetes.io/docs/reference/access-authn-authz/admission-controllers/)
- [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-admission/)
- [Kyverno policy examples](https://kyverno.io/policies/)
- [ValidatingAdmissionPolicy (KEP-3882)](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/3882-admission-policy)
- Your reference: `06-security.md` (sections on admission control and
  supply chain).