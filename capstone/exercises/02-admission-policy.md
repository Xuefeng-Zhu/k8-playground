# Exercise 02 — Bypassing the admission policy

**Linked lab:** `labs/05-admission-vs-agent.md`
**Linked chapter:** `06-security.md` (admission control, supply chain)

**Time:** ~10 min
**What you'll break:** the assumption that "no manifest can deploy a
non-compliant pod in capstone tier namespaces."

---

## Before

The capstone has a ValidatingWebhookConfiguration that denies pods in
the capstone-frontend / backend / postgres namespaces unless they have
explicit non-root securityContexts.

```bash
kubectl get validatingwebhookconfigurations
# NAME              ADMISSION   RULES                       VALIDATIONS
# capstone-policy   -           CREATE,UPDATE pods in ...   deny

# Try the obvious bad pod:
kubectl -n capstone-frontend run bad --image=nginx:1.27 --restart=Never
# Expected: Error from server: admission webhook denied the request:
#   container 'bad' has no securityContext
```

## Break — try various ways to bypass

These should ALL fail. Try each one and observe what catches it:

```bash
# 1. No securityContext at all (the obvious case)
kubectl -n capstone-frontend apply -f broken-manifests/root-pod.yaml
# Caught by: admission webhook

# 2. Explicit runAsUser: 0 (root)
kubectl -n capstone-frontend run root-explicit --image=nginx:1.27 \
  --restart=Never --overrides='{"spec":{"containers":[{"name":"root-explicit",
  "image":"nginx:1.27","securityContext":{"runAsUser":0}}]}}'
# Caught by: admission webhook

# 3. Allow privilege escalation
kubectl -n capstone-frontend run escalates --image=nginx:1.27 \
  --restart=Never --overrides='{"spec":{"containers":[{"name":"escalates",
  "image":"nginx:1.27","securityContext":{"runAsNonRoot":true,"runAsUser":10001,
  "allowPrivilegeEscalation":true}}]}}'
# Caught by: admission webhook

# 4. Try to deploy in a non-capstone namespace (should succeed)
kubectl run anywhere --image=nginx:1.27 --restart=Never --namespace=default
# Expected: succeeds. The webhook only covers the capstone tier namespaces.

# 5. Try to deploy in capstone-system (should succeed)
kubectl run webhook-sibling --image=nginx:1.27 --restart=Never \
  --namespace=capstone-system --overrides='{"spec":{"containers":[{
  "name":"webhook-sibling","image":"nginx:1.27","securityContext":{
  "runAsNonRoot":true,"runAsUser":1000}}]}}'
# Expected: succeeds. capstone-system is exempt (it's the webhook's home).

# Cleanup
kubectl delete pod anywhere webhook-sibling --namespace=default --ignore-not-found
kubectl delete pod webhook-sibling --namespace=capstone-system --ignore-not-found
```

## Observe — which layer catches which violation

Each violation was caught by a **different layer**:

| Violation | Caught by | Why |
|---|---|---|
| No securityContext | Admission webhook (webhook) | CEL condition fails |
| runAsUser: 0 | Admission webhook | CEL condition fails |
| allowPrivilegeEscalation | Admission webhook | CEL condition fails |
| hostPath volume | Admission webhook | CEL condition fails |
| Outside capstone namespaces | Nothing — allowed | Webhook's namespaceSelector excludes non-capstone namespaces |

This is **defense in depth**. The admission policy is the wall; if
someone slips a pod past it (a bug in the policy, a new namespace, a
disable of the webhook), the kubelet still enforces some things (no
running as root if `runAsNonRoot: true` is set on the pod), and the
OS still enforces others (Linux capabilities, seccomp profiles).

## What happens if you DISABLE the policy?

```bash
# Bypass the admission webhook entirely
kubectl delete validatingwebhookconfiguration capstone-policy
# Now try the bad pod:
kubectl -n capstone-frontend apply -f broken-manifests/root-pod.yaml
# It will succeed. The policy was the ONLY thing stopping it.

# Restore the policy
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
    caBundle: $(kubectl -n capstone-system get secret capstone-webhook-tls -o jsonpath='{.data.ca\.crt}')
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
# Now the bad pod is rejected again:
kubectl -n capstone-frontend apply -f broken-manifests/root-pod.yaml
# Error from server: admission webhook denied the request
```

**This is the most important demonstration in the capstone.** Without
the policy, nothing else enforces the baseline. With it back, nothing
slips through.

## Postmortem prompt (`postmortems/02-capstone-admission.md`)

1. **What happens when the policy is disabled AND someone deploys a
   root pod?** Walk through the full chain: pod created → kubelet tries
   to start it → with `runAsNonRoot: true` enforced at pod level, the
   kubelet rejects it. So defense in depth: which violations are caught
   by the admission policy, and which would be caught by the kubelet's
   `runAsNonRoot` enforcement if the admission policy were absent?
2. **What happens if the webhook is unreachable?** `failurePolicy: Fail`
   means the apiserver rejects the pod if the webhook times out. What
   does this mean for availability during a webhook outage? (Hint: see
   lab 02.)
3. **What's the right way to relax a policy?** Don't edit it in place
   (auditing breaks). Don't disable it (you lose the protection). Add
   an exception that requires review. How would you structure that?
4. **How would an AI agent fail here?** It can't disable the policy
   because that's a privileged operation. But it CAN suggest "let's
   relax the policy because it's blocking my work" — how would you
   detect that drift in a code review?