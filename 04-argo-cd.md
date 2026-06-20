# 4. Argo CD

**Solves:** GitOps. The cluster reconciles toward the Git repo. Humans push commits; Argo CD applies. Removes the imperative `kubectl apply` workflow and gives you cluster state == Git state, always.

**When you need it:** Any cluster with more than a few apps. It's the production default unless you have a strong reason to use Flux.

## 4.1 Install

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# Create the namespace first with PSS labels
kubectl create namespace argocd
kubectl label namespace argocd pod-security.kubernetes.io/enforce=baseline

# Production-grade install
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --values - <<'EOF'
# HA: 3 of each, leader election where supported
redis:
  enabled: true
  architecture: replication        # 3-node redis HA
  master:
    persistence: { enabled: true, size: 8Gi }
  replica:
    persistence: { enabled: true, size: 8Gi }
  sentinel:
    enabled: true

server:
  replicas: 2
  autoscaling: { enabled: false }  # HPA handled externally
  resources:
    requests: { cpu: 100m, memory: 256Mi }
    limits:   { cpu: 500m, memory: 512Mi }

controller:
  replicas: 2
  resources:
    requests: { cpu: 100m, memory: 512Mi }
    limits:   { cpu: 1,    memory: 1Gi   }

repoServer:
  replicas: 2
  resources:
    requests: { cpu: 50m,  memory: 128Mi }
    limits:   { cpu: 250m, memory: 512Mi }

applicationSet:
  enabled: true                   # templating many apps from one definition

dex:
  enabled: false                  # use OIDC directly via SSO config below

# Notifications for sync failures → Slack/PagerDuty
notifications:
  enabled: true
  trigger: [on-deployed, on-health-degraded, on-sync-failed]

# Metrics
metrics:
  enabled: true
  serviceMonitor:
    enabled: true
EOF
```

## 4.2 The Application — the unit of work

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: api-server
  namespace: argocd
  # Finalizer — when you delete the Application, Argo CD cleans up the managed resources
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: prod                   # AppProject for RBAC

  source:
    repoURL: https://github.com/myorg/infra
    targetRevision: main          # or: HEAD, refs/heads/main, v1.4.2
    path: apps/api-server/overlays/prod
    # Helm: chart + values
    # helm:
    #   valueFiles:
    #     - values-prod.yaml
    # Kustomize: path is the base
    # directory:
    #   recurse: true              # for monorepo structures
    #   jsonnet: {}
  destination:
    server: https://kubernetes.default.svc
    namespace: prod

  syncPolicy:
    automated:
      prune: true                 # delete resources removed from Git
      selfHeal: true              # revert manual kubectl edits
      allowEmpty: false
    syncOptions:
      - CreateNamespace=true      # create the destination namespace if missing
      - PrunePropagationPolicy=foreground
      - ServerSideApply=true      # SSA — fewer conflicts on large objects
      - ApplyOutOfSyncOnly=true   # only apply what's drifted (faster syncs)
    retry:
      limit: 5
      backoff:
        duration: 30s
        factor: 2
        maxDuration: 5m

  # Don't sync during incidents
  syncWindows:
    - kind: deny
      schedule: "0 0 * * 0"       # deny Sundays (planned maintenance)
      duration: 4h
      applications: ["*"]
      manualSync: true            # manual sync still allowed
```

## 4.3 ApplicationSet — the templating

When you have many apps from one repo (the standard pattern), use ApplicationSet.

### Cluster generator (one app per cluster)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: cluster-apps
  namespace: argocd
spec:
  goTemplate: true
  generators:
    - clusters:
        selector:
          matchLabels:
            env: prod              # only clusters with this label
        values:
          url: https://kubernetes.default.svc
  template:
    metadata:
      name: 'api-server-{{.nameNormalized}}'
    spec:
      project: prod
      source:
        repoURL: https://github.com/myorg/infra
        targetRevision: main
        path: 'apps/api-server/overlays/{{.nameNormalized}}'
      destination:
        server: '{{.url}}'
        namespace: prod
      syncPolicy:
        automated: { prune: true, selfHeal: true }
```

### Git directory generator (one app per directory)

```yaml
spec:
  generators:
    - git:
        repoURL: https://github.com/myorg/infra
        revision: main
        directories:
          - path: apps/*
          # Exclude patterns
          - path: apps/*
            exclude: true
            glob: apps/_template
  template:
    metadata:
      name: '{{.path.basename}}'
    spec:
      source:
        repoURL: https://github.com/myorg/infra
        targetRevision: main
        path: '{{.path.path}}'
      destination:
        server: https://kubernetes.default.svc
        namespace: '{{.path.basename}}'
```

### Matrix generator (combine multiple)

```yaml
spec:
  generators:
    - matrix:
        generators:
          - clusters: { selector: { matchLabels: { env: prod } } }
          - git:
              repoURL: https://github.com/myorg/infra
              revision: main
              directories:
                - path: apps/*
  template: { ... }
```

## 4.4 AppProject — RBAC + boundaries

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: prod
  namespace: argocd
spec:
  description: Production apps
  sourceRepos:
    - https://github.com/myorg/infra
    - https://charts.bitnami.com/bitnami   # for Helm charts you depend on
  destinations:
    - namespace: 'prod-*'                  # only prod-* namespaces
      server: https://kubernetes.default.svc
  clusterResourceWhitelist:
    - group: ""
      kind: Namespace
  namespaceResourceWhitelist:
    - group: ""           # core
    - group: "apps"
    - group: "networking.k8s.io"
    - group: "policy"
    - group: "autoscaling"
  orphanedResources:
    warn: true
    ignore:
      - kind: Secret
        name: kube-root-ca.crt
  roles:
    - name: developer
      policies:
        - p, proj:prod:developer, applications, get, prod/*, allow
        - p, proj:prod:developer, applications, sync, prod/*, deny
      groups:
        - myorg:developers
```

## 4.5 SSO / OIDC (the production auth path)

```bash
# Patch argocd-cm to add OIDC
kubectl patch cm argocd-cm -n argocd --type merge -p '
data:
  url: https://argocd.yourorg.com
  oidc.config: |
    name: Okta
    issuer: https://yourorg.okta.com
    clientId: argo-cd
    clientSecret: $oidc.clientSecret   # from a K8s secret
    requestedScopes: ["openid", "profile", "email", "groups"]
    requestedClaims: {groups: {essential: true}}
'
```

```bash
# Restart server to pick up OIDC config
kubectl rollout restart deployment argocd-server -n argocd
```

For RBAC mapping (Okta groups → Argo CD permissions):

```yaml
# argocd-rbac-cm
data:
  policy.csv: |
    p, role:developer, applications, get, */*, allow
    p, role:developer, applications, sync, prod/*, allow
    g, myorg:developers, role:developer
```

## 4.6 Notifications — wire sync failures to Slack

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-notifications-cm
  namespace: argocd
data:
  service.slack: |
    token: $slack-token
  template.sync-failed: |
    message: |
      :fire: Application {{.app.metadata.name}} sync failed
      Sync Status: {{.app.status.sync.status}}
      Revision: {{.app.status.sync.revision}}
      Error: {{.app.status.operationState.message}}
  template.health-degraded: |
    message: |
      :warning: Application {{.app.metadata.name}} is degraded
      Health: {{.app.status.health.status}}
  trigger.on-deployed: |
    - when: app.status.operationState.phase in ['Succeeded']
      send: [sync-succeeded]
  trigger.on-sync-failed: |
    - when: app.status.sync.status == 'OutOfSync' and app.status.operationState.phase == 'Failed'
      send: [sync-failed]
```

Subscribe services:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Subscription
metadata:
  name: prod-oncall
  namespace: argocd
spec:
  destinations:
    - service: slack
      channel: '#prod-oncall'
  filters:
    - afterSyncSucceeded: {}     # only fire for prod
  triggers:
    - on-sync-failed
    - on-health-degraded
```

## 4.7 Verify

```bash
# Login via CLI
argocd login argocd.yourorg.com --sso

# What apps exist?
argocd app list

# Sync status
argocd app get api-server
# Sync Status:        Synced
# Health Status:      Healthy
# Last Sync:          2024-01-15 12:34:56

# Force a refresh (re-evaluate from Git, don't apply)
argocd app get api-server --refresh

# Diff: what's different?
argocd app diff api-server

# Manual sync
argocd app sync api-server

# Hard refresh — re-pull from Git
argocd app get api-server --hard-refresh

# App history
argocd app history api-server
```

## 4.8 Production gotchas

### Self-heal masks problems

`selfHeal: true` will revert manual `kubectl` edits within ~3 minutes. This is what you want for stateless workloads. For stateful workloads (Databases with manual failover), turn it off:

```yaml
syncPolicy:
  automated:
    selfHeal: false           # let operators edit safely
    prune: false              # don't delete things on sync
```

### Sync window vs incident response

If you have `syncWindows: [deny]` and there's an outage, manual sync still works but is logged. Don't deny manual sync during incident windows — you'll add operational friction when you least want it.

### Repo URL must be exactly the same

`https://github.com/myorg/infra` and `https://github.com/myorg/infra.git` are different repos to Argo CD. Pick one and stick with it. Use SSH (`git@github.com:myorg/infra`) in clusters; HTTPS+token is fragile to token rotation.

### CRDs and Application boundaries

Argo CD's sync can include CRDs (cert-manager CRDs, Kyverno CRDs). **Don't sync CRDs through normal Application paths** — if you do, you get diffs every time the operator changes a CRD status field. Use a separate Application for CRDs that auto-syncs once, then self-heal off.

```yaml
# Pattern: separate Application for CRDs, one-time sync
syncPolicy:
  syncOptions:
    - Replace=true
    - PrunePropagationPolicy=foreground
  automated: { selfHeal: false }   # don't fight the operator
```

### The Argo CD admin password

If you don't set up SSO, the initial admin password is the `argocd-secret` `admin.password` field. **Set up SSO before exposing Argo CD to anyone.**

### Repo server OOMs

The repo server caches manifests in memory. Large monorepos with hundreds of apps will OOM. Solutions:
- Bump repo server memory limits (1Gi → 2Gi).
- Split monorepo into per-team repos.
- Use ApplicationSet generators to avoid listing all apps in one Application.

### Finalizer on Application

`finalizers: [resources-finalizer.argocd.argoproj.io]` means deleting the Application deletes the managed resources. **This is dangerous** — a bad merge can wipe state. For stateful workloads, remove the finalizer:

```yaml
metadata:
  finalizers: []   # resources stay when Application is deleted
```

### Many clusters = repo-server bottleneck

With 10+ clusters polling the same repo, the repo-server can become a bottleneck. Strategies:
- One repo-server per cluster (Argo CD instance per cluster).
- Webhook-driven sync instead of polling (`argocd-appset-controller` polls every 3 min by default; configure your Git provider to send webhook events).

### ApplicationSet `goTemplate: true`

Enable `goTemplate: true` so you can use `{{.nameNormalized}}` and similar — without it, the templating syntax is limited and you'll hit walls immediately.

### Sync waves

For ordered rollouts (DB migration → app), use sync waves:

```yaml
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "-1"   # lower = earlier
spec:
  # ...
```

Wave 0 = first, wave 5 = later. Default is 0.

### Sync options you'll want

```yaml
syncOptions:
  - ServerSideApply=true     # SSA — fewer merge conflicts
  - ApplyOutOfSyncOnly=true  # faster syncs (only what changed)
  - CreateNamespace=true     # auto-create namespaces
  - PrunePropagationPolicy=foreground  # wait for deletion before pruning dependents
  - RespectIgnoreDifferences=true     # respect .argocd-ignore
```

### Debugging

```bash
# Sync error — what's the diff?
argocd app diff api-server

# Force re-evaluation
argocd app get api-server --refresh

# Controller logs (sync issues)
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-application-controller --tail=200

# Repo server logs (manifest generation issues)
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-repo-server --tail=200

# Server logs (UI / API)
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-server --tail=200
```

### Backups

Argo CD stores state in its own DB (or Redis if you enabled HA). Back up via:

```bash
# Dump applications + projects
argocd app list -o yaml > apps-backup.yaml
argocd proj list -o yaml > projects-backup.yaml

# Or use the argocd-ext-conf plugin / Velero
```

Or use `argocd-autopilot` to manage the bootstrap itself from Git.
