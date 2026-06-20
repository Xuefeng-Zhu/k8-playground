# 5. Argo Rollouts

**Solves:** Progressive delivery. Canary, blue/green, traffic shaping, **metrics-driven automatic promotion/abort** based on Prometheus queries. Drop-in replacement for a Deployment when you need safer rollouts.

**When you need it:** User-facing services with SLOs. The transition from `kubectl rollout status` to "promote if p99 stays under 200ms and error rate stays under 1%" is what production-grade delivery looks like.

## 5.1 Install

```bash
# Argo Rollouts is a separate chart from Argo CD
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

kubectl create namespace argo-rollouts
kubectl label namespace argo-rollouts pod-security.kubernetes.io/enforce=baseline

helm upgrade --install argo-rollouts argo/argo-rollouts \
  --namespace argo-rollouts \
  --values - <<'EOF'
dashboard:
  enabled: true                    # the kubectl plugin + web UI

controller:
  replicas: 2
  resources:
    requests: { cpu: 50m,  memory: 128Mi }
    limits:   { cpu: 500m, memory: 512Mi }

serviceMonitor:
  enabled: true

# Metrics server is optional — only if you use AnalysisRun with K8s metrics
metricsService:
  enabled: false                   # not needed for Prometheus analysis
EOF

# Install the kubectl plugin
curl -LO https://github.com/argoproj/argo-rollouts/releases/latest/download/kubectl-argo-rollouts-darwin-amd64
chmod +x kubectl-argo-rollouts-darwin-amd64
sudo mv kubectl-argo-rollouts-darwin-amd64 /usr/local/bin/kubectl-argo-rollouts
```

## 5.2 Canary with NGINX traffic routing

### The Rollout

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api-server
  namespace: prod
spec:
  replicas: 5
  revisionHistoryLimit: 5
  selector:
    matchLabels: { app: api-server }

  strategy:
    canary:
      canaryService: api-server-canary
      stableService: api-server-stable
      # NGINX ingress traffic routing
      trafficRouting:
        nginx:
          stableIngress: api-server          # name of the Ingress for the stable version
          additionalIngressAnnotations:
            canary-by-header: X-Canary        # allow manual override
            canary-by-header-value: enroll    # header value triggers canary
      steps:
        - setWeight: 5
        - pause: { duration: 5m }            # wait 5 minutes
        - setWeight: 25
        - pause: { duration: 5m }
        - setWeight: 50
        - pause: { duration: 10m }
        - setWeight: 100                    # promote to 100%

      analysis:
        # Background analysis throughout the rollout
        templates:
          - templateName: success-rate
        # Starting analysis at step 2, running every 5 minutes
        startingStep: 2
        args:
          - name: service-name
            value: api-server

  template:
    metadata:
      labels: { app: api-server }
    spec:
      containers:
        - name: api
          image: ghcr.io/myorg/api:1.4.2
          ports: [{ name: http, containerPort: 8080 }]
          # Standard probes — required for Argo Rollouts health checks
          readinessProbe:
            httpGet: { path: /ready, port: http }
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /healthz, port: http }
            periodSeconds: 10
          resources:
            requests: { cpu: 100m, memory: 256Mi }
            limits:   { cpu: 500m, memory: 512Mi }
```

### The two Services (stable + canary)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: api-server-stable
  namespace: prod
spec:
  selector: { app: api-server }    # both canary and stable share this selector
  ports: [{ port: 80, targetPort: http }]
---
apiVersion: v1
kind: Service
metadata:
  name: api-server-canary
  namespace: prod
spec:
  selector: { app: api-server }
  ports: [{ port: 80, targetPort: http }]
```

### The Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-server
  namespace: prod
  annotations:
    # Required for NGINX canary traffic splitting
    kubernetes.io/ingress.class: nginx
spec:
  tls: [{ hosts: [api.yourorg.com], secretName: api-server-tls }]
  rules:
    - host: api.yourorg.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: api-server-stable, port: { number: 80 } } }
```

## 5.3 AnalysisTemplate — Prometheus-driven success criteria

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
  namespace: prod
spec:
  args:
    - name: service-name
  metrics:
    - name: error-rate
      # Roll out aborted if error rate exceeds 1% for 2 of 3 consecutive checks
      interval: 60s
      count: 3
      successCondition: result <= 0.01
      failureCondition: result > 0.01
      provider:
        prometheus:
          address: http://kube-prometheus-stack-prometheus.monitoring.svc:9090
          query: |
            sum(rate(
              http_requests_total{service="{{args.service-name}}",code=~"5.."}[5m]
            ))
            /
            sum(rate(
              http_requests_total{service="{{args.service-name}}"}[5m]
            ))
    - name: latency-p99
      interval: 60s
      count: 3
      successCondition: result <= 0.2        # 200ms
      failureCondition: result > 0.5         # 500ms — abort
      provider:
        prometheus:
          address: http://kube-prometheus-stack-prometheus.monitoring.svc:9090
          query: |
            histogram_quantile(0.99,
              sum by (le) (
                rate(http_request_duration_seconds_bucket{service="{{args.service-name}}"}[5m])
              )
            )
```

## 5.4 Blue/Green

For a service where you want atomic switchover (not gradual):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: billing
  namespace: prod
spec:
  replicas: 4
  selector: { matchLabels: { app: billing } }
  strategy:
    blueGreen:
      activeService: billing-active
      previewService: billing-preview
      autoPromotionEnabled: false           # require manual promote
      scaleDownDelaySeconds: 300             # keep old version for 5 min post-switch
      previewReplicaCount: 4                 # full replica preview
      abortScaleDownDelaySeconds: 30
      previewMetadata:
        labels: { role: preview }            # for tests/queries
      analysis:
        templates: [{ templateName: success-rate }]
        startingStep: 1
  template:
    metadata: { labels: { app: billing } }
    spec:
      containers:
        - name: billing
          image: myorg/billing:2.1.0
```

```yaml
# Two services — both select app=billing, but only "active" gets the production traffic
apiVersion: v1
kind: Service
metadata: { name: billing-active }
spec:
  selector: { app: billing }            # all Pods match; traffic routing chooses active vs preview
  ports: [{ port: 80, targetPort: 8080 }]
---
apiVersion: v1
kind: Service
metadata: { name: billing-preview }
spec:
  selector: { app: billing, role: preview }   # only preview ReplicaSet
  ports: [{ port: 80, targetPort: 8080 }]
```

## 5.5 Traffic routers — what's supported

| Router | Use it when |
|--------|-------------|
| `nginx` | ingress-nginx (most common) |
| `alb` | AWS ALB ingress controller |
| `trafficSplit` | Service mesh (Istio, Linkerd, SMI) |
| `ambassador` | Ambassador / Emissary-ingress |
| `gateway` | Gateway API (alpha in newer versions) |

For Istio:

```yaml
trafficRouting:
  istio:
    virtualService: { name: api-server-vs }
    destinationRule: { name: api-server-dr }
    # Optional: mirror canary traffic to stable for shadow testing
    mirrorTraffic: { percentage: 10 }
```

## 5.6 Day-2 — promoting, aborting, rolling back

```bash
# Trigger a rollout (update image)
kubectl argo rollouts set image api-server api=ghcr.io/myorg/api:1.5.0 -n prod

# Watch the rollout
kubectl argo rollouts get rollout api-server -n prod --watch

# Status
kubectl argo rollouts status api-server -n prod

# Promote manually (skip remaining steps)
kubectl argo rollouts promote api-server -n prod

# Abort (roll back to stable)
kubectl argo rollouts abort api-server -n prod

# Retry after abort
kubectl argo rollouts retry rollout api-server -n prod

# List all rollouts
kubectl argo rollouts list rollouts -n prod

# Dashboard
kubectl argo rollouts dashboard
```

## 5.7 Production gotchas

### PodDisruptionBudget is mandatory

Without a PDB, Argo Rollouts can scale up a new ReplicaSet, fail to scale down the old one, and exhaust your node capacity. **Always set a PDB** when you adopt Rollouts.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-server
  namespace: prod
spec:
  minAvailable: 2
  selector: { matchLabels: { app: api-server } }
```

### The "two Services with the same selector" anti-pattern

This is required for canary, but it means **any plain `Service` that selects `app=api-server` will load-balance across BOTH versions**. If you have other services (e.g. for an internal-only consumer), they need a different selector strategy or you'll get unpredictable traffic.

Mitigation: include the rollout label in selectors, e.g. `app: api-server, role: stable`, and update the Ingress accordingly. Rollouts can do this automatically with `trafficRouting`.

### AnalysisRun query mistakes

The most common bug: forgetting that Prometheus rates are per-second. `rate(metric[5m])` gives you per-second over the last 5m. To get "requests in last 5 minutes" you multiply by 300.

Test your analysis queries in Grafana's Explore before plugging them in.

### Pause without abort vs Abort

- `pause: { duration: 5m }` is a *scheduled* pause; the rollout will auto-resume.
- Manual `promote` or `abort` can happen during the pause.

If your `pause: duration` is too short and the analysis hasn't collected enough data points, the rollout promotes without a real signal. **Match pause durations to analysis `count * interval` with margin.**

### Analysis failures should be alerts

AnalysisTemplate with `failureCondition` *aborts* the rollout, but doesn't necessarily alert. Wire `kubectl get analysisrun -A` into a Prometheus rule:

```promql
# Any failed analysis in the last hour
increase(argocd_rollouts_analysis_run_phase_total{phase="Failed"}[1h]) > 0
```

### nginx ingress annotations

For canary traffic splitting via ingress-nginx, you need:

```yaml
# For nginx.ingress.kubernetes.io/canary-* to work, the rule must include:
# nginx.ingress.kubernetes.io/canary-weight: <number>
```

Argo Rollouts sets these dynamically per step. If you have other annotations on the ingress that conflict (e.g. `nginx.ingress.kubernetes.io/rewrite-target`), test the canary with a real ingress, not just `kubectl apply`.

### Rollouts resource consumption

Two ReplicaSets simultaneously = 2x the resource request during a rollout. If you're memory-tight, schedule rollouts at low-traffic hours or use `maxSurge: 1, maxUnavailable: 0`.

### Controller is itself a SPOF

If Argo Rollouts controller is down, **rollouts don't progress**, but **existing rollouts are not aborted**. Your existing ReplicaSets are fine. Don't restart the controller unless you understand the in-flight state.

### Testing rollouts

Use `dryRun`:

```bash
kubectl argo rollouts set image api-server api=ghcr.io/myorg/api:1.5.0 -n prod --dry-run
```

Or test the analysis query alone:

```bash
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AnalysisRun
metadata:
  generateName: test-success-rate-
  namespace: prod
spec:
  args: [{ name: service-name, value: api-server }]
  metrics:
    - name: error-rate-test
      count: 1
      interval: 30s
      provider:
        prometheus:
          address: http://kube-prometheus-stack-prometheus.monitoring.svc:9090
          query: 'sum(rate(http_requests_total{service="{{args.service-name}}",code=~"5.."}[5m]))'
EOF

kubectl get analysisrun -n prod -w
```
