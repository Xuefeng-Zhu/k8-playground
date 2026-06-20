# Tooling Deep-Dives for Kubernetes in Production

> Working configs for the eight tools that make up the platform layer in a production Kubernetes cluster. Each deep-dive is self-contained: copy the YAML, adapt the values, apply.

This companion to the [Kubernetes in Production reference](../k8s-prod-reference/) covers the *how* — the reference covers the *why*. When the reference says "use cert-manager," this is where you find the manifest, the helm values, the verify command, and the gotchas.

## Scope

These are not tutorials. They are **operational recipes**: install → configure → verify → debug. Each page assumes you already understand *what* the tool does and need to know *how* to make it work in production.

## The 8 deep-dives

| # | Tool | What it solves | When you need it |
|---|------|----------------|------------------|
| 1 | [cert-manager](./01-cert-manager.md) | TLS cert issuance + auto-renewal | Any cluster with an HTTPS ingress |
| 2 | [External Secrets Operator](./02-external-secrets.md) | Sync secrets from cloud KMS / Vault → K8s | Anything beyond `kubectl create secret` |
| 3 | [Kyverno](./03-kyverno.md) | Policy-as-code: image signing, PSS, no `:latest` | Multi-tenant clusters, compliance |
| 4 | [Argo CD](./04-argo-cd.md) | GitOps: cluster reconciles from Git | Any cluster, unless you prefer Flux |
| 5 | [Argo Rollouts](./05-argo-rollouts.md) | Progressive delivery: canary, blue/green, metrics-driven | User-facing services, SLOs |
| 6 | [Velero](./06-velero.md) | Cluster + PV backup/restore, namespace migration | Day-1; you need it before you need it |
| 7 | [Falco](./07-falco.md) | Runtime threat detection | Compliance, multi-tenant, defense-in-depth |
| 8 | [kube-prometheus-stack](./08-prometheus-stack.md) | Metrics + dashboards + alerts, batteries included | Almost every prod cluster |

## How each page is organised

Every deep-dive has the same structure so you can find what you need fast:

1. **What it is / when to use it** — 30-second context (skip if you know the tool).
2. **Install** — Helm values or kubectl apply, with the production-grade knobs called out.
3. **Core configs** — the YAML/Helm values you'll actually copy.
4. **Verify** — commands that prove it's working.
5. **Production gotchas** — the things that bite you in week 2.

## Conventions

- All commands assume `kubectl` and `helm` are installed and configured.
- "Apply with kustomize" examples use `kubectl apply -k` for inline use; in production, these belong in your GitOps repo.
- Variables like `${CLUSTER_NAME}` or `${AWS_ACCOUNT_ID}` are placeholders — substitute.
- Cloud-agnostic where possible; cloud-specific steps are flagged.

## What's not covered

- **Service mesh (Istio / Linkerd).** Too large; the configs are 200+ lines each. See Part VI of the reference for the decision framework; reach out if you want a dedicated deep-dive.
- **CNI installation** (Calico, Cilium). The reference covers architecture; install is provider-specific.
- **Cluster bootstrap** (kubeadm, kOps, Cluster API). Belongs in cluster-lifecycle docs, not platform tooling.
- **Per-cloud managed equivalents** (ACM PCA, Cloud DNS service accounts, GKE Workload Identity). Mentioned in context, not deep-dived.

## How to use

**In a hurry:** open the deep-dive, copy the **Core configs** section, replace placeholders, apply, verify.

**Setting up a new cluster:** work through 1 → 8 in order. They don't strictly depend on each other, but the natural build order is: cert-manager first (Ingress needs it), then secrets, then GitOps, then the rest.

**Debugging:** each page has a **Debugging** section under Production gotchas.
