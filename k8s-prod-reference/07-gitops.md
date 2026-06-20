# Part VII — GitOps & Continuous Delivery

GitOps is the operational model that fits Kubernetes best. The cluster reconciles toward Git; humans push commits; the system does the rest.

## 7.1 What GitOps actually is

GitOps is the practice of:

1. **Declarative configuration** (YAML, Helm, Kustomize) describes the desired cluster state.
2. **Versioned in Git** as the single source of truth.
3. **Automatically applied** by an agent that watches Git (or repo) and reconciles the cluster to match.
4. **Audited** via Git history: who changed what, when, and why.

The benefits:

- **Recoverability.** Cluster state is reproducible from Git + secrets.
- **Audit trail.** Every change is a commit with a reviewer.
- **Separation of concerns.** Devs write code; platform team owns the deployment patterns.
- **Reduced kubectl.** Less imperative access to the cluster, fewer footguns.

## 7.2 GitOps vs traditional CI/CD

| | Traditional | GitOps |
|---|------------|--------|
| Source of truth | CI pipeline + cluster state | Git repo |
| Cluster drift | Hidden | Detected and reverted by reconciler |
| Promotion | Pipeline stages, env variables | Branch/tag, environment directory |
| Rollback | Re-run old pipeline | Revert commit |
| Audit | CI logs | Git log + commit metadata |
| Multi-cluster | Replicate pipeline | Multiple reconcilers, same repo |

**The key conceptual shift:** the cluster is no longer the source of truth. Git is. The cluster is a *projection* of Git.

## 7.3 The two camps: Argo CD vs Flux

Both are CNCF-graduated. They implement GitOps differently.

### Argo CD

- **Architecture:** central server + repo server + Redis. HA via leader election.
- **UI:** built-in, polished. Shows app health, sync status, drift.
- **Application model:** `Application` CRD points to a repo + path. `ApplicationSet` for templating many apps.
- **Sync modes:** automatic with self-heal; manual; sync windows.
- **Notifications:** via Argo Notifications + EventBus, can alert Slack/Teams/webhook on sync failures.
- **Strengths:** rich UI, easy to demo, good RBAC model, ApplicationSets.
- **Weaknesses:** central server is a HA concern; UI invites users to "click and forget" the Git discipline.

### Flux

- **Architecture:** tool-by-toolkit. `source-controller` watches Git/Helm/OCI; `kustomize-controller`/`helm-controller` apply; `notification-controller` sends alerts. All operators, no central server.
- **UI:** none (third-party UIs like Weave GitOps).
- **Application model:** `GitRepository` source, `Kustomization`/`HelmRelease` for the result. Compose with `dependsOn`.
- **Strengths:** toolkit composition; no central state; modular.
- **Weaknesses:** steeper learning curve; less polished visibility.

### When to pick which

| Scenario | Pick |
|----------|------|
| Want UI for visibility and demo | Argo CD |
| Many small repos and want composition | Flux |
| Need HA without single point of failure | Flux |
| Have App-of-Apps or templating use cases | Both work; Argo has ApplicationSets; Flux has HelmRelease templating |
| Existing Helm workflow + GitOps | Flux v2 (better Helm story) |
| Existing Helm workflow but already on Argo | Stick with Argo + Helm charts |

## 7.4 The Application / Kustomization model

Both tools model "an application" as a resource that points to a source and a target.

### Argo CD Application

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: api-server
  namespace: argocd
spec:
  project: prod
  source:
    repoURL: https://github.com/myorg/infra
    targetRevision: main
    path: apps/api-server
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: prod
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
    retry:
      limit: 5
      backoff:
        duration: 30s
        factor: 2
        maxDuration: 5m
```

### Flux Kustomization

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: api-server
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: GitRepository
    name: infra
  path: ./apps/api-server
  prune: true
  wait: true
  timeout: 5m
```

## 7.5 Repo structure patterns

### Monorepo

```
infra/
├── clusters/
│   ├── prod-us-east-1/
│   ├── prod-eu-west-1/
│   └── dev/
├── apps/
│   ├── api-server/
│   │   ├── base/
│   │   └── overlays/
│   └── worker/
├── platform/
│   ├── ingress/
│   ├── secrets/
│   └── monitoring/
└── README.md
```

**Per-cluster directory** holds the entrypoint; each references apps/platform via relative paths. Tools like Argo CD's `ApplicationSet` (cluster generator) or Flux's Kustomization auto-discovery work well here.

### Multi-repo

```
app-repo (deploy/ subdir)         infra-repo (clusters/)
                                   ├── prod-cluster-app.yaml
                                   └── dev-cluster-app.yaml
```

Application team owns `app-repo`. Platform team owns `infra-repo`. Argo/Flux watches both. Cleaner separation, more moving parts.

**Decision:** start monorepo if you're a small platform. Split to multi-repo when app teams start owning their own pipelines.

## 7.6 Templating — Kustomize vs Helm

### Kustomize

- Native to `kubectl` (`kubectl apply -k`).
- Pure patching: base + overlay.
- No templating language.
- Less expressive for complex logic.

```yaml
# base/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
spec:
  template:
    spec:
      containers:
        - name: app
          image: api-server:1.0.0
---
# overlays/prod/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
namePrefix: prod-
images:
  - name: api-server
    newTag: 1.4.2
replicas:
  - name: api-server
    count: 5
```

### Helm

- Templating language (Go templates + Sprig functions).
- Chart repositories, versioning, dependency management.
- Can be used outside Kubernetes too.
- More expressive but more footguns (curly-bracket conflicts with CRDs).

```yaml
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "api.fullname" . }}
spec:
  replicas: {{ .Values.replicaCount }}
```

**Decision:** Kustomize for app configs you control. Helm for shared infrastructure (cert-manager, ingress-nginx) and for packaging. Don't mix in one app.

## 7.7 Progressive delivery

GitOps gives you declarative state. Progressive delivery gives you safety nets:

### Blue/green

Two Deployments (blue, green). A Service or Ingress routes 100% to one. Switch atomically. Old version stays alive for quick rollback.

Tools: Argo Rollouts, Flagger, manual `kubectl patch`.

### Canary

Gradual traffic shift: 5% → 25% → 50% → 100%. Metrics-driven (Prometheus) or time-based.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api-server
spec:
  strategy:
    canary:
      steps:
        - setWeight: 5
        - pause: { duration: 5m }
        - setWeight: 25
        - pause: { duration: 5m }
        - setWeight: 100
      canaryService: api-server-canary
      stableService: api-server-stable
      analysis:
        templates:
          - templateName: success-rate
```

### Feature flags

Different axis: deploy the code; toggle features at runtime. **LaunchDarkly, Flagsmith, Unleash, OpenFeature.**

GitOps and feature flags solve different problems. GitOps = which version is deployed. Feature flags = which features are exposed. Use both.

## 7.8 Drift detection

The reconciler (Argo CD or Flux) compares cluster state to Git. If they differ (someone ran `kubectl edit`, an autoscaler changed something), it's drift.

- **Self-heal** in Argo / `prune: true` in Flux: the agent reverts the cluster to Git state.
- **Disable self-heal** for stateful workloads where the agent shouldn't revert legitimate changes.

**Production rule:** self-heal for stateless apps. For stateful workloads, leave it off or scope it carefully.

## 7.9 Secrets in GitOps

GitOps assumes everything in Git. Secrets aren't Git-friendly.

Options:
- **External Secrets Operator** syncs from cloud KMS/Vault → Kubernetes Secret.
- **Sealed Secrets** (Bitnami) — encrypted Secrets stored in Git, decrypted in-cluster.
- **SOPS** (Mozilla) — encrypt files in-place, store in Git.
- **Vault Agent Injector** — pulls secrets at runtime, no K8s Secret at all.

Pick based on your secret rotation cadence, compliance, and tooling tolerance.

## 7.10 Multi-cluster GitOps

The same Git repo can drive multiple clusters via:

- **App-of-Apps pattern** (Argo): top-level Application that points to a directory of Applications.
- **ApplicationSet generators** (Argo): cluster selector, Git generator, matrix generator.
- **Flux per-cluster bootstrap:** each cluster gets a Flux install that watches the same repo, filtered by cluster label.

For true multi-region, see Part X.

## 7.11 Production failure modes (Part VII scope)

- **Git is not the source of truth.** Someone runs `kubectl apply` and changes "shouldn't matter" things. Self-heal hides it; revert shows up later.
- **Argo CD without HA.** Central server is a SPOF.
- **Helm chart overrides everywhere.** Config becomes unmaintainable; chart upgrades break things.
- **Sync windows not configured.** Auto-sync during an incident can clobber mitigations.
- **Manifest sprawl.** Hundreds of YAML files with no policy; security gaps, RBAC chaos.
- **No drift detection policy.** Drift accumulates silently until the reconciler fights production.
- **Long-lived branches.** Code merges happen weekly; GitOps is for fast PRs.

## 7.12 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| Argo CD | Want UI, large teams, demo-ability | Want minimal ops footprint |
| Flux | Modularity, multi-tenant, no central server | Need polished UI out of the box |
| Kustomize | Internal apps, simple config | Heavy templating needs |
| Helm | Shared infra, complex apps | Pure patching suffices |
| Monorepo | Small platform, single team | Many independent app teams |
| Multi-repo | App teams own deployment | Heavy coordination overhead |
| Argo Rollouts | Need canary + metrics analysis | Single cluster, single env |
| Progressive delivery | User-facing services | Internal batch jobs |

## 7.13 Further reading

- [Argo CD](https://argo-cd.readthedocs.io/)
- [Flux CD](https://fluxcd.io/)
- [OpenGitOps principles](https://opengitops.dev/)
- [Argo Rollouts](https://argoproj.github.io/argo-rollouts/)
- [Flagger](https://flagger.app/)
- [Helm](https://helm.sh/docs/)
- [Kustomize](https://kustomize.io/)
- [External Secrets Operator](https://external-secrets.io/)