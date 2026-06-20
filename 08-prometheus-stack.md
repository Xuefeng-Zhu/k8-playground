# 8. kube-prometheus-stack

**Solves:** Production-grade metrics, dashboards, and alerts in one Helm chart. Ships Prometheus, Alertmanager, Grafana, kube-state-metrics, node-exporter, and ~150 pre-built recording rules + alerts + Grafana dashboards. The "I just need the metrics" answer.

**When you need it:** Almost every production cluster. The alternative is assembling 6+ components yourself and never quite getting the dashboards right.

## 8.1 Install

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring
kubectl label namespace monitoring pod-security.kubernetes.io/enforce=baseline

# CRDs are huge — install them explicitly so GitOps knows about them
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_alertmanagerconfigs.yaml
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_prometheuses.yaml
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_servicemonitors.yaml
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_podmonitors.yaml
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_probes.yaml
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/example/prometheus-operator-crd/monitoring.coreos.com_rules.yaml

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values - <<'EOF'
# CRDs are installed separately above
crds:
  crds:
    enabled: false

# ─── Prometheus ───
prometheus:
  prometheusSpec:
    replicas: 2                       # HA via Prometheus operator
    retention: 30d                    # 30 days of metrics
    retentionSize: "50GiB"            # hard cap before oldest are dropped
    resources:
      requests: { cpu: 500m, memory: 2Gi }
      limits:   { cpu: 2,    memory: 8Gi }
    # WAL compression — saves disk
    walCompression: true
    # ServiceAccount for IRSA / Workload Identity
    serviceAccountName: prometheus
    # External labels — added to every metric
    externalLabels:
      cluster: prod-us-east-1
      region: us-east-1
    # Storage
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 100Gi
    # PodMonitor / ServiceMonitor / Probe selectors — restrict what we scrape
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorNamespaceSelector: {}     # all namespaces
    serviceMonitorSelector: {}
    podMonitorSelectorNilUsesHelmValues: false
    podMonitorNamespaceSelector: {}
    podMonitorSelector: {}
    probeSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    # Scrape interval — 30s for most, 15s for critical
    scrapeInterval: 30s
    evaluationInterval: 30s

# ─── Alertmanager ───
alertmanager:
  alertmanagerSpec:
    replicas: 3
    resources:
      requests: { cpu: 50m, memory: 128Mi }
      limits:   { cpu: 200m, memory: 256Mi }
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 5Gi

# ─── Grafana ───
grafana:
  replicas: 2
  adminPassword: "${GRAFANA_ADMIN_PASSWORD}"   # from a Secret in production
  persistence:
    enabled: true
    size: 5Gi
  # Pre-install dashboards and datasources via sidecar
  sidecar:
    dashboards:
      enabled: true
      searchNamespace: monitoring
      label: grafana_dashboard
    datasources:
      enabled: true
      searchNamespace: monitoring
  # Ingress for Grafana
  ingress:
    enabled: true
    ingressClassName: nginx
    hosts: [grafana.yourorg.com]
    tls: [{ hosts: [grafana.yourorg.com], secretName: grafana-tls }]
  resources:
    requests: { cpu: 100m, memory: 128Mi }
    limits:   { cpu: 500m, memory: 512Mi }

# ─── kube-state-metrics ───
kubeStateMetrics:
  enabled: true
  resources:
    requests: { cpu: 10m, memory: 32Mi }
    limits:   { cpu: 100m, memory: 128Mi }

# ─── node-exporter ───
nodeExporter:
  enabled: true
  resources:
    requests: { cpu: 50m, memory: 32Mi }
    limits:   { cpu: 200m, memory: 128Mi }

# ─── Pushgateway (for batch jobs) ───
pushgateway:
  enabled: false    # not needed unless you have batch jobs pushing metrics

EOF
```

## 8.2 ServiceMonitor — scrape your apps

The cleanest way to add custom metrics. Drop this in your app's Git repo:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: api-server
  namespace: prod
  labels:
    release: kube-prometheus-stack   # so the operator picks it up
spec:
  selector:
    matchLabels:
      app: api-server               # the Service's labels
  endpoints:
    - port: http                    # the Service port name
      path: /metrics
      interval: 15s                  # scrape every 15s (faster than default 30s)
      scrapeTimeout: 10s
      # Add custom labels
      relabelings:
        - sourceLabels: [__meta_kubernetes_service_label_app]
          targetLabel: app
```

Make sure your app exposes `/metrics` in Prometheus format. Most frameworks have libraries:

```python
# Python with prometheus-client
from prometheus_client import Counter, Histogram, generate_latest
from flask import Flask, Response

app = Flask(__name__)
REQUESTS = Counter('http_requests_total', 'Total HTTP requests', ['method', 'endpoint', 'code'])
LATENCY = Histogram('http_request_duration_seconds', 'HTTP latency', ['endpoint'])

@app.route('/metrics')
def metrics():
    return Response(generate_latest(), mimetype='text/plain')
```

## 8.3 PodMonitor — scrape Pods directly

Use when there's no Service (a batch job, sidecar, etc.):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: cert-exporter
  namespace: cert-manager
spec:
  selector:
    matchLabels:
      app: cert-exporter
  podMetricsEndpoints:
    - port: metrics
      path: /metrics
      interval: 30s
```

## 8.4 Recording rules — precompute expensive queries

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: api-server-recording
  namespace: prod
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: api-server.recording
      interval: 30s
      rules:
        - record: api:request_rate:5m
          expr: |
            sum by (endpoint, code) (
              rate(http_requests_total{service="api-server"}[5m])
            )

        - record: api:error_rate:5m
          expr: |
            sum by (endpoint) (
              rate(http_requests_total{service="api-server",code=~"5.."}[5m])
            )
            /
            sum by (endpoint) (
              rate(http_requests_total{service="api-server"}[5m])
            )

        - record: api:latency_p99:5m
          expr: |
            histogram_quantile(0.99,
              sum by (le, endpoint) (
                rate(http_request_duration_seconds_bucket{service="api-server"}[5m])
              )
            )
```

These recording rules are what your alerts and dashboards should query. Querying raw `rate()` every dashboard refresh is expensive.

## 8.5 SLO recording rules — error budget tracking

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: api-server-slo
  namespace: prod
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: api-server.slo
      interval: 30s
      rules:
        # Total requests over the SLO window (28 days)
        - record: slo:api-server:requests_total:28d
          expr: |
            sum(
              increase(http_requests_total{service="api-server"}[28d])
            )

        # Failed (5xx) requests
        - record: slo:api-server:errors_total:28d
          expr: |
            sum(
              increase(http_requests_total{service="api-server",code=~"5.."}[28d])
            )

        # The SLO ratio — keep this < 0.001 for 99.9% availability
        - record: slo:api-server:error_budget_remaining:28d
          expr: |
            1 - (
              slo:api-server:errors_total:28d
              /
              slo:api-server:requests_total:28d
            ) / 0.001   # the SLO target
```

Then alert on burn rate:

```yaml
groups:
  - name: api-server.alerts
    rules:
      # Fast burn: 2% of budget in 1 hour = 14.4x burn rate
      - alert: ApiServerFastBurn
        expr: |
          (
            slo:api-server:errors_total:1h
            /
            slo:api-server:requests_total:1h
          ) > (14.4 * 0.001)
        for: 2m
        labels: { severity: page }
        annotations:
          summary: "API server burning error budget fast"
          runbook: "https://wiki.yourorg.com/runbooks/api-server-fast-burn"

      # Slow burn: 5% of budget in 24 hours
      - alert: ApiServerSlowBurn
        expr: |
          (
            slo:api-server:errors_total:6h
            /
            slo:api-server:requests_total:6h
          ) > (3 * 0.001)
        for: 30m
        labels: { severity: ticket }
        annotations:
          summary: "API server slowly burning error budget"
```

This is the **Google SRE workbook** pattern: multi-window burn rate alerts catch both sudden outages and slow degradation.

## 8.6 Alertmanager config — Slack + PagerDuty + inhibition

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: main
  namespace: monitoring
  labels:
    alertmanagerConfig: main
spec:
  route:
    receiver: 'default'
    groupBy: ['alertname', 'namespace']
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 4h
    routes:
      - matchers:
          - severity = "page"
        receiver: 'pagerduty'
        continue: true
      - matchers:
          - severity = "ticket"
        receiver: 'slack-eng'
        continue: true
      - matchers:
          - severity = "info"
        receiver: 'slack-eng-low'
  receivers:
    - name: 'default'
      slackConfigs:
        - apiURL: "${SLACK_WEBHOOK}"
          channel: '#alerts'
          sendResolved: true
    - name: 'pagerduty'
      pagerdutyConfigs:
        - serviceKey: "${PAGERDUTY_KEY}"
          sendResolved: true
    - name: 'slack-eng'
      slackConfigs:
        - apiURL: "${SLACK_WEBHOOK}"
          channel: '#prod-oncall'
          sendResolved: true
    - name: 'slack-eng-low'
      slackConfigs:
        - apiURL: "${SLACK_WEBHOOK}"
          channel: '#alerts-low'
          sendResolved: true
  inhibitRules:
    # If a node is down, don't alert on Pods that can't schedule
    - sourceMatchers:
        - alertname = "NodeDown"
      targetMatchers:
        - alertname = "PodPending"
      equal: [node]
    # If the cluster's API server is down, don't alert on individual readiness
    - sourceMatchers:
        - alertname = "Watchdog"
      targetMatchers:
        - alertname =~ "KubeJob.*|KubeDeployment.*"
```

## 8.7 Long-term storage with Thanos

The kube-prometheus-stack Prometheus is fine for 30 days. For longer, use **Thanos** (or Cortex/Mimir):

```yaml
# Thanos sidecar (upload to S3)
prometheus:
  prometheusSpec:
    thanos:
      version: v0.34.0
      objectStorageConfig:
        name: thanos-objstore-config
        key: thanos.yaml
      # Block compaction — fewer objects in S3
      thanosObjectStorageConfig: {}
```

```yaml
# Secret for Thanos
apiVersion: v1
kind: Secret
metadata:
  name: thanos-objstore-config
  namespace: monitoring
stringData:
  thanos.yaml: |
    type: S3
    config:
      bucket: thanos-prod-metrics
      endpoint: s3.us-east-1.amazonaws.com
      region: us-east-1
      access_key: ${AWS_ACCESS_KEY_ID}    # from IRSA in production
      secret_key: ${AWS_SECRET_ACCESS_KEY}
```

Then run **Thanos Querier** + **Thanos Store** + **Thanos Compactor** in the cluster (separate chart). Querier gives you a single PromQL endpoint across all Prometheus replicas + long-term S3 data.

## 8.8 Verify

```bash
# What's running?
kubectl get pods -n monitoring
# Prometheus, alertmanager, grafana, kube-state-metrics, node-exporter...

# Can Prometheus scrape itself?
kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090 -n monitoring &
# Open http://localhost:9090
# Status > Targets — should see all the kube-system targets up

# Are alerts firing?
# Open http://localhost:9090/alerts

# Test an alert
# Edit a rule to `for: 0s` and a threshold that's already true
# Or trigger an alert via kubectl:
kubectl scale deployment alert-test --replicas=0
# (assuming a HorizontalPodAutoscaler is configured)

# Grafana
kubectl port-forward svc/kube-prometheus-stack-grafana 3000:80 -n monitoring &
# Login: admin / $GRAFANA_ADMIN_PASSWORD
# Check the "Kubernetes / Compute Resources" dashboard
```

## 8.9 Production gotchas

### Cardinality is the silent killer

Every unique combination of labels = one time series. `http_requests_total` with `user_id` = millions of series = OOM. Watch:

```promql
# Top 10 series by cardinality
topk(10, count by (__name__) ({__name__=~".+"}))
```

**Mitigations:**
- Drop high-cardinality labels in `metricRelabelings`.
- Use recording rules to aggregate.
- Set a hard cap: `--storage.tsdb.max-block-chunk-segment-size` (operator: `storage.tsdbOutOfOrderTimeWindow: 0`).

### Prometheus storage fills fast

30 days of cluster + app metrics on a 50-node cluster = 200-500 GB easily. Plan storage accordingly and **alert on disk usage**:

```yaml
- alert: PrometheusDiskWillFillIn24h
  expr: |
    predict_linear(
      prometheus_tsdb_head_series[6h],
      24 * 3600
    ) > 10000000
  labels: { severity: warning }
```

### WAL on ephemeral storage

If Prometheus restarts (e.g. evicted Pod), the WAL on `emptyDir` is lost. **Use a PVC for the WAL** (the chart does this by default with `storageSpec`). Don't override to ephemeral storage.

### Grafana admin password

The default admin password in Helm values is in plain text in Git. Use `--set grafana.adminPassword=$(cat secret.txt)` or pull from a Secret:

```bash
kubectl create secret generic grafana-admin --from-literal=admin-password=$(openssl rand -hex 16) -n monitoring
helm upgrade --install kube-prometheus-stack ... \
  --set grafana.adminPasswordExistingSecret=grafana-admin
```

### Scrape interval vs cardinality

Lower scrape intervals = more data = more cost. **Default 30s is fine for most metrics.** 15s for SLO-critical services. Below 10s = expensive; consider Victoria Metrics or a streaming solution.

### Recording rules aren't free

Recording rules run continuously. A 1000-rule recording group every 30s = significant CPU. Test the load before adding.

### kube-state-metrics resource limit

By default, kube-state-metrics watches *all* CRDs. For multi-tenant clusters with many CRDs, this can OOM. Configure `--resources=deployments,pods,services,configmaps,...` to limit.

### Prometheus operator RBAC

The Prometheus operator creates / updates Prometheus CRs and config. If the operator is down, you can't change your Prometheus config. Lock it down with RBAC.

### AlertmanagerConfig hot reload

AlertmanagerConfig CRs are reconciled by the operator. Changes take ~1 minute to apply. Don't expect instant propagation.

### Multi-cluster Prometheus

For multi-cluster, use **Thanos Querier** or **Cortex** to federate. Don't try to scrape cross-cluster from one Prometheus — network and auth are painful.

### Monitoring namespace = trusted

Anyone with `get pods` in `monitoring` can read Prometheus config (which contains scrape targets, basic auth passwords, etc.). Lock down:

```yaml
# NetworkPolicy
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: monitoring-egress
  namespace: monitoring
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to: [{}]    # Prometheus needs to scrape everything
      ports:
        - protocol: TCP
          port: 9090
```

### Long retention ≠ free

Thanos / Cortex / Mimir charges for storage. $0.023/GB/month on S3. 1TB for 1 year = $276/year. Worth it for prod; budget accordingly.

### Custom dashboards in Git

The chart's `sidecar.dashboards.enabled: true` loads dashboards from ConfigMaps labelled `grafana_dashboard`. Put your dashboards in Git as ConfigMaps — they survive cluster rebuilds.
