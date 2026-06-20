# Part VIII — Observability

You can't operate what you can't see. Kubernetes observability has four pillars and many pitfalls.

## 8.1 The four signals

| Signal | What | How in Kubernetes |
|--------|------|-------------------|
| **Metrics** | Numeric time series | Prometheus, Datadog, Cloud Monitoring |
| **Logs** | Discrete events | Loki, ELK, Splunk, Cloud Logging |
| **Traces** | Request paths across services | Jaeger, Tempo, Datadog APM |
| **Events** | Cluster-level changes | EventRouter → log store; kube-state-metrics |

**Plus a fifth, often-forgotten signal:**
- **Profiles** — continuous profiling (Parca, Pyroscope, Datadog). Useful for cost optimisation, not for incident response.

### The golden signals

For each service, capture:
- **Rate** (requests/sec)
- **Errors** (5xx, 4xx)
- **Duration** (latency p50, p95, p99)
- **Saturation** (CPU, memory, queue depth)

## 8.2 Metrics — Prometheus and the ecosystem

### Prometheus architecture

```
┌─────────────┐    scrape    ┌──────────────┐
│ /metrics    │◄─────────────│  Prometheus  │
│ endpoints   │              │              │
└─────────────┘              └──────┬───────┘
   (Pod, Node, Service)            │
                                   │ PromQL
                                   ▼
                            ┌──────────────┐
                            │ Alertmanager │──► Slack/PagerDuty
                            └──────────────┘
                                   │
                                   ▼
                            ┌──────────────┐
                            │   Grafana    │──► dashboards
                            └──────────────┘
```

### What to scrape

| Source | What it gives you |
|--------|-------------------|
| **cAdvisor (kubelet)** | Per-container CPU, memory, network, fs |
| **kube-state-metrics** | Cluster-level counts (Pods, Deployments, Node status) |
| **Node exporter** | Host-level metrics |
| **App `/metrics`** | App-specific (RED metrics, business KPIs) |
| **Ingress / API gateway** | L7 traffic, status codes |
| **Cloud integration** | LB metrics, managed DB metrics |

### Prometheus Operator

The de facto way to run Prometheus. CRDs:
- `Prometheus` — instance
- `Alertmanager` — instance
- `ServiceMonitor` — how to scrape a Service
- `PodMonitor` — how to scrape Pods
- `PrometheusRule` — alerting rules
- `Probe` — blackbox probing

This is a heavy install. Alternatives:
- **Grafana Cloud / Datadog / New Relic / Chronosphere** — managed Prometheus.
- **Victoria Metrics** — drop-in Prometheus with better compression and cost.
- **Thanos / Cortex / Mimir** — long-term storage + global view across many Prometheuses.

### Production Prometheus rules

- **Retention:** 30 days local, longer in object storage. Cardinality is the cost driver.
- **Cardinality:** `http_requests_total` with `path` and `user_id` labels is a footgun. Cap or drop high-cardinality labels.
- **Sharding:** at scale, split Prometheus into multiple shards by label.
- **Recording rules:** precompute expensive queries (e.g. `apiserver_request_duration_seconds:rate5m`) so dashboards don't hit raw data.
- **SLO recording rules:** `slo:service_errors_total:ratio_rate5m` style.

### Alerting

Good alerts are:
- **Page on user-impacting symptoms** (latency, errors, availability). Not on causes.
- **Tied to SLOs.** Each page is a budget burn.
- **Actionable.** Every alert has a runbook URL.
- **Tuned.** Alert fatigue kills trust.

Bad alerts:
- "Disk usage > 80%" (no impact yet; who acts?)
- "Pod restart" (informational; rare in steady state)
- "Prometheus self-monitoring failed" (yes, but rare)

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: api-server
spec:
  groups:
    - name: api-server.rules
      rules:
        - alert: ApiServerHighErrorRate
          expr: |
            sum(rate(http_requests_total{service="api-server",code=~"5.."}[5m]))
            / sum(rate(http_requests_total{service="api-server"}[5m])) > 0.05
          for: 10m
          labels:
            severity: page
          annotations:
            summary: "API 5xx > 5% for 10m"
            runbook: https://wiki/runbooks/api-errors
```

## 8.3 Logs

### Log patterns

- **Node-level:** each kubelet writes container logs to `/var/log/containers/` or `/var/log/pods/`. Read via `kubectl logs`.
- **Sidecar pattern:** a sidecar (Fluent Bit, Vector) in each Pod tails stdout → forwards to log store. Heavy at scale.
- **Node-level agent:** a DaemonSet (Fluent Bit, Vector, Filebeat) on each node tails all container logs. **The production default.**
- **Cloud integration:** GKE logs go to Cloud Logging; EKS to CloudWatch; AKS to Log Analytics.

### Structured logging

Production logs must be JSON. Stdout from `app.log("user signed in")` is unactionable. Stdout from `app.log({event: "user_signed_in", user_id: 123})` is searchable, aggregatable, alertable.

```python
import logging
import json
class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": record.created,
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            **({"trace_id": ...} if hasattr(record, "trace_id") else {}),
        })
```

### Log levels and noise

- **DEBUG:** noisy; never in production unless debugging.
- **INFO:** normal events (request, startup, shutdown).
- **WARN:** recoverable issue (retry, fallback).
- **ERROR:** failed operation.
- **FATAL:** process should die.

Sampling and rate-limiting matter. Don't log every successful request. Aggregate: "1k requests logged at sample 0.01" is fine.

### Production logging tools

| Tool | Strength | Trade-off |
|------|----------|-----------|
| **Loki + Grafana** | Cheap, integrates with Grafana | Limited query language |
| **Elasticsearch + Kibana** | Powerful search | Heavy ops, expensive |
| **Cloud-native (CloudWatch, Cloud Logging)** | Default, integrated | Vendor lock |
| **Datadog Logs / Splunk** | SaaS, easy | Cost at scale |
| **Vector** | Fast, transform in pipeline | Operational tool |

## 8.4 Traces

Distributed tracing correlates a request across services.

### OpenTelemetry (OTel)

OpenTelemetry is the standard. It replaces OpenTracing + OpenCensus.

**Production pattern:** an OTel SDK in each service emits spans to a collector. The collector fans out to a backend (Tempo, Jaeger, Datadog, etc.).

```
service A ──► OTel SDK ──┐
service B ──► OTel SDK ──┼──► OTel Collector ──► Tempo / Jaeger
service C ──► OTel SDK ──┘
```

### Sampling

Tracing every request is expensive. **Head-based sampling** decides at request start. **Tail-based sampling** decides after the request, based on outcome — preferred for catching errors but harder to scale.

For Kubernetes:
- 1-10% sampling is the starting point.
- 100% for error/timeout/5xx paths (if you can tail-sample).
- Always sample health checks at 0%.

### Context propagation

W3C Trace Context (`traceparent` header) is the standard. Inject it at the ingress; propagate through services. Service mesh does this for you (Istio, Linkerd, Cilium). With bare services, your libraries must.

## 8.5 Events and Kubernetes-aware signals

Kubernetes emits Events for almost everything (Pod scheduled, image pulled, OOM killed). They're not retained long; capture them.

- **EventRouter** — ships Events to a log store.
- **kube-state-metrics** — converts K8s state to Prometheus metrics (Pod phase, Deployment replicas, Node condition).
- **Cluster API observability** — what nodes are unhealthy, what's draining, what's pending.

Useful alerts:
- Pod `CrashLoopBackOff` for > 5 minutes
- `FailedScheduling` events
- Node `NotReady` for > 5 minutes
- Image pull errors

## 8.6 The four signals together — what "good" looks like

A production service has:

- **Metrics:** RED metrics per service, plus saturation (CPU, memory, queue depth). Prometheus + Grafana. Alerts on SLO burn.
- **Logs:** structured JSON, all app logs in one place (Loki, ELK). Search by trace_id.
- **Traces:** OpenTelemetry, sampled, propagated. Spans from ingress to DB.
- **Events:** captured; alerts on CrashLoopBackOff, FailedScheduling, OOMKilled.

The signals must be correlated. Click a metric spike → see the traces → see the logs → see the events. This is the difference between 10-minute MTTR and 4-hour MTTR.

## 8.7 Tooling stack reference

### Open-source reference stack

| Layer | Tool |
|-------|------|
| Metrics | Prometheus + Thanos / Mimir for long-term |
| Logs | Loki + Promtail / Vector |
| Traces | Tempo + OpenTelemetry Collector |
| Dashboards | Grafana |
| Alerts | Alertmanager |
| Uptime | Blackbox exporter |
| Profiles | Parca / Pyroscope |

### Managed alternatives

- **Datadog** — full-stack SaaS.
- **Grafana Cloud** — managed Grafana + Prometheus + Loki + Tempo.
- **Chronosphere** — observability at scale (cost-conscious metric platform).
- **Honeycomb** — traces-first observability.

The choice is usually driven by:
- Existing investment (Datadog customers stick with Datadog).
- Cost at scale (Prometheus + Loki is cheap; Datadog is feature-rich and expensive).
- Engineering culture (more or less self-managed).

## 8.8 Production failure modes (Part VIII scope)

- **Cardinality explosion.** `path` as a label = millions of unique series = Prometheus OOM.
- **Logging without trace context.** Logs are useless during an incident.
- **Alerts on every metric.** Alert fatigue → real alerts ignored.
- **Logs in the container filesystem.** Lost on Pod restart; node disk fills.
- **No SLO definition.** Everything is "high priority."
- **Health checks failing logged at ERROR.** Flood of alerts during routine rollout.
- **No event capture.** You miss OOMKilled because it was a one-time event, scrolled away.
- **Sampling 0% in production.** Zero traces on incidents.
- **Sampling 100% in production.** Bill of $200k/month.

## 8.9 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| Self-managed Prom + Loki + Tempo | Cost-sensitive, ops capacity, open-source commitment | Small team, no Grafana expertise |
| Datadog | Need everything working tomorrow, can afford it | Cost-sensitive at scale |
| Grafana Cloud | Self-managed-ish but not hosted-on-call | Pure OSS requirement |
| Tempo (traces) | Already on Loki/Prometheus | Need strong query power (Jaeger) |
| OpenTelemetry | Greenfield, new services | Deep OpenTracing investment |
| Head-based sampling | Easy, predictable | Need to catch rare errors |
| Tail-based sampling | Catch all errors | Cost-conscious |

## 8.10 Further reading

- [Prometheus Documentation](https://prometheus.io/docs/)
- [OpenTelemetry](https://opentelemetry.io/docs/)
- [Loki](https://grafana.com/oss/loki/)
- [Grafana Tempo](https://grafana.com/oss/tempo/)
- [Kubernetes Monitoring Guide](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-usage-monitoring/)
- [Google SRE Book — Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)