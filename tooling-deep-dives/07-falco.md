# 7. Falco

**Solves:** Runtime threat detection. Watches syscalls, Kubernetes audit logs, and (with plugins) network traffic, flags anomalous activity. The production-grade "did someone just exec into a Pod they shouldn't?" detector.

**When you need it:** Compliance, multi-tenant clusters, defense-in-depth. It's not a replacement for preventive controls (PSP/PSS, NetworkPolicy), it's a tripwire that fires when prevention fails.

## 7.1 Install (modern: Falco + Falco Helm chart)

The classic install uses the `falcosecurity/falco` chart. Newer installs use the **Falco Talon** response framework and **Falco Sidekick** for output fan-out.

```bash
helm repo add falcosecurity https://falcosecurity.github.io/charts
helm repo update

kubectl create namespace falco
kubectl label namespace falco pod-security.kubernetes.io/enforce=privileged  # Falco needs privileged

# Production install — modern eBPF probe (no kernel modules needed)
helm upgrade --install falco falcosecurity/falco \
  --namespace falco \
  --values - <<'EOF'
# Driver: modern eBPF is the right choice for kernel >= 5.8
driver:
  kind: modern_ebpf
  modern_ebpf:
    # The eBPF probe is loaded into the kernel — works without kernel headers
    # and across most distros (Ubuntu, Amazon Linux 2, Bottlerocket)
    # If your distro isn't supported, fall back to kernel module:
    #   driver.kind: kmod

# Falco itself
falco:
  image:
    repository: falcosecurity/falco
    tag: 0.38.0
  resources:
    requests: { cpu: 100m, memory: 512Mi }
    limits:   { cpu: 1,    memory: 1Gi   }
  # DaemonSet — one Falco per node
  daemonset:
    enabled: true
    # Falco tolerations — must run on every node including control plane
    tolerations:
      - operator: Exists

  # Output to stdout + Sidekick
  http_output:
    enabled: true
    url: "http://falco-falcosidekick.falco.svc:2801"

  # Don't audit activity in noisy containers — see "exceptions"
  # (configured via ConfigMap below)

# Audit logs from kube-apiserver
# Falco can read these via the audit webhook — alternative to syscall watching
# In this install, we stick to eBPF syscall watching.

# Sidekick for output fan-out (Slack, Loki, Elasticsearch, PagerDuty)
falcosidekick:
  enabled: true
  replicas: 2
  resources:
    requests: { cpu: 50m, memory: 64Mi }
    limits:   { cpu: 200m, memory: 256Mi }
  config:
    # Where to send the events
    slack:
      webhookurl: "https://hooks.slack.com/services/${SLACK_TOKEN}"
      channel: "#sec-alerts"
      minimumpriority: "warning"
    loki:
      hostport: "http://loki.monitoring.svc:3100/loki/api/v1/push"
    # Webhook for custom pipelines
    webhook:
      address: "http://eventrouter.monitoring.svc:9200"
      minimumpriority: "warning"

# Falco UI for browsing events
falcosidekick-ui:
  enabled: true

# Response framework
falco-talon:
  enabled: true                  # automated responses (kill pod, quarantine, etc.)

serviceMonitor:
  enabled: true                  # metrics for Prometheus

# Custom rules (loaded from a ConfigMap)
customRules:
  custom-rules.yaml: |           # see Section 7.2
    - rule: Crypto Miner Detection
      desc: >
        Detects known crypto miner binaries or processes running in containers.
      condition: >
        spawned_process and container and
        proc.name in (xmrig, minerd, c2miner, stratum, cryptonight, ethminer, etherminer)
      output: >
        Crypto miner detected in container
        (user=%user.name command=%proc.cmdline container=%container.name image=%container.image.repository:%container.image.tag namespace=%k8s.ns.name pod=%k8s.pod.name)
      priority: CRITICAL
      tags: [cryptomining, attack]
EOF
```

## 7.2 Custom rules — the high-signal ones

Falco ships with ~200 rules. The ones below are what most teams add. Drop these in a ConfigMap and reference them.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: falco-custom-rules
  namespace: falco
data:
  custom-rules.yaml: |
    # ─── Cryptocurrency mining ───
    - rule: Crypto Miner Binary
      desc: Detects known crypto miner binaries
      condition: >
        spawned_process and container and
        proc.name in (xmrig, minerd, stratum, cryptonight, ethminer)
      output: >
        Crypto miner binary executed
        (user=%user.name command=%proc.cmdline container=%container.name namespace=%k8s.ns.name pod=%k8s.pod.name)
      priority: CRITICAL
      tags: [cryptomining, attack]

    # ─── Reverse shells ───
    - rule: Outbound Reverse Shell
      desc: Outbound connection from a shell process (possible reverse shell)
      condition: >
        spawned_process and container and
        proc.name in (bash, sh, zsh, dash, ash) and
        (connect to non-local address) or
        (proc.cmdline contains "bash -i" or proc.cmdline contains "/dev/tcp/")
      output: >
        Possible reverse shell from container
        (user=%user.name command=%proc.cmdline container=%container.name namespace=%k8s.ns.name)
      priority: CRITICAL
      tags: [reverse_shell, attack]

    # ─── Sensitive file reads ───
    - rule: Read of Sensitive File in Container
      desc: Read of /etc/shadow or similar sensitive file
      condition: >
        open_read and container and
        fd.name in (/etc/shadow, /etc/sudoers, /root/.ssh/id_rsa, /root/.ssh/authorized_keys)
      output: >
        Sensitive file read in container
        (user=%user.name file=%fd.name container=%container.name namespace=%k8s.ns.name)
      priority: WARNING
      tags: [sensitive_file, attack]

    # ─── Setuid / capability abuse ───
    - rule: SUID Binary Executed
      desc: Setuid binary execution in container (privilege escalation)
      condition: >
        spawned_process and container and
        proc.name in (su, sudo, passwd, mount, umount, newgrp, chsh, gpasswd)
      output: >
        SUID binary executed in container
        (user=%user.name command=%proc.cmdline container=%container.name namespace=%k8s.ns.name)
      priority: WARNING
      tags: [privilege_escalation]

    # ─── Package manager in production container ───
    - rule: Package Manager in Production Container
      desc: apt/yum/apk in a running container (post-install activity)
      condition: >
        spawned_process and container and
        proc.name in (apt, apt-get, yum, dnf, apk, pip, npm, gem, cargo)
      output: >
        Package manager invoked in container
        (user=%user.name command=%proc.cmdline container=%container.name namespace=%k8s.ns.name)
      priority: WARNING
      tags: [post_exploitation]
```

## 7.3 Exceptions — silencing known noise

Falco fires on *behaviour*. Some behaviours are legit (kyverno doing mutation, fluentbit reading files). Use **exceptions** to whitelist known-safe patterns.

```yaml
# Add to the ConfigMap above
data:
  exceptions.yaml: |
    # kyverno needs to read configmaps / mutate webhooks
    - rule: Read sensitive file untrusted
      exceptions:
        - name: kyverno-bg
          fields: [container.name, k8s.ns.name]
          comps: [=, =]
          values:
            - kyverno
            - kyverno

    # fluentbit reads /var/log — exclude
    - rule: Read sensitive file untrusted
      exceptions:
        - name: fluentbit
          fields: [container.image.repository]
          comps: [=]
          values:
            - fluent/fluent-bit

    # healthchecks can connect to many things
    - rule: Unexpected outbound connection
      exceptions:
        - name: liveness-probes
          fields: [k8s.ns.name, proc.name]
          comps: [=, =]
          values:
            - monitoring
            - curl
```

## 7.4 Verify

```bash
# Is Falco running?
kubectl get ds -n falco
# NAME    DESIRED   CURRENT   READY   UP-TO-DATE   AVAILABLE   NODE SELECTOR   AGE
# falco   5         5         5       5            5           <none>          10d

# Are the rules loaded?
kubectl logs -n falco -l app.kubernetes.io/name=falco --tail=200 | grep -i "rule"

# Trigger an alert (in another terminal):
kubectl run attacker --image=alpine --restart=Never -n default -- sh -c "wget -q -O- http://1.1.1.1"
# Falco will detect the outbound connection from `wget` in a container.

# Sidekick UI
kubectl port-forward svc/falco-falcosidekick-ui -n falco 2802:2802
# Open http://localhost:2802

# Metrics endpoint
kubectl logs -n falco -l app.kubernetes.io/name=falco --tail=100 | grep -i metrics

# FalcoTalon response actions (if enabled)
kubectl get falcotalon -A
```

## 7.5 FalcoTalon — automated responses

FalcoTalon kills / quarantines / notifies on Falco events. **Use carefully** — auto-killing pods is fine for dev, dangerous in prod without limits.

```yaml
apiVersion: talos.security/falco/v1alpha1
kind: TalonResponse
metadata:
  name: kill-miners
  namespace: falco
spec:
  rule: "Crypto Miner Binary"
  action: kubernetes.cluster.job.delete.pod
  # Only in dev — not prod!
  enabled: false
  parameters:
    pod: "{{.PodName}}"
    namespace: "{{.Namespace}}"
```

## 7.6 Production gotchas

### Falco is privileged

Falco runs as a DaemonSet with `privileged: true` to load eBPF programs. **Compromise of Falco = cluster root.** Lock down:
- NetworkPolicy: only allow egress to Sidekick / Loki / Slack.
- RBAC: no ServiceAccount token mounted (`automountServiceAccountToken: false`).
- Run on dedicated node pools with taints.

### Falco driver choice

- **modern_ebpf**: kernel ≥ 5.8, no kernel headers needed, fast.
- **kmod**: classic kernel module, requires kernel headers at build time.
- **bpf**: legacy BPF probe, deprecated.

Use modern_ebpf unless your distro kernel is < 5.8 (Amazon Linux 2 has 5.10, Bottlerocket has 5.10, Ubuntu 22.04 has 5.15, all fine).

### Kernel upgrade breaks eBPF

When the host kernel upgrades, the eBPF probe may fail to load until Falco restarts. Configure a `postStart` hook or rely on the DaemonSet rolling restart:

```yaml
falco:
  daemonset:
    updateStrategy:
      type: RollingUpdate
      rollingUpdate:
        maxUnavailable: 1
```

### CPU cost at high traffic

Falco hooks every syscall. On a busy node, it can hit 5-15% CPU. Mitigations:
- Filter rules with conditions that don't fire frequently.
- Drop verbose rules (the default ruleset has ~200 rules; many are noisy).
- Use Falco *only* on nodes that need it (taint + toleration).

### Alert volume

Out of the box, Falco fires hundreds of events per day. **You must tune or your team ignores it.** The pattern:
1. Run Falco in `priority: DEBUG` mode for a week.
2. Categorize every event: real alert, noise, false positive.
3. Add exceptions for noise.
4. Raise priority on real-but-noisy alerts.
5. Wire to a ticketing system, not just Slack (so triage is tracked).

### eBPF program size limit

The kernel has a hard limit on eBPF program complexity (verified instruction count). If you add dozens of complex rules, Falco will fail to load with cryptic errors. Test custom rules in isolation.

### Cloud provider logging

Falco doesn't replace CloudTrail / Cloud Audit Logs. Use both:
- Cloud audit = "who called the AWS API?"
- Falco = "what's happening inside the Pod?"

### Falco vs Tetragon vs Tracee

| Tool | Strength |
|------|----------|
| **Falco** | Mature, broad ruleset, multi-platform (kernel + k8s audit) |
| **Tetragon** | eBPF-native, lower-level, can enforce (not just detect) |
| **Tracee** | eBPF, Aqua Security, simpler |

For most teams, Falco wins on ruleset coverage. For "must enforce" (block the syscall), Tetragon.

### Network policy required

Falco sends events to Sidekick / Loki / Slack. If your egress NetworkPolicy is too tight, alerts silently drop. Add explicit egress rules for Falco pods.

### Multi-tenancy

A noisy neighbour's pod can trigger Falco events that flood your alerts. Use the `k8s.ns.name` field in exceptions to scope.

### Falco output to PagerDuty

Sidekick can route to PagerDuty:

```yaml
falcosidekick:
  config:
    pagerduty:
      apikey: "${PAGERDUTY_KEY}"
      minimumpriority: "critical"
```

Test with `minimumpriority: "debug"` first; you'll discover that "critical" is too noisy in early days.

### Don't ship Falco logs as the only audit

Falco events are operational, not compliance-grade. For SOC2 / PCI, also enable:
- kube-apiserver audit logs (always on, shipped to SIEM).
- Cloud audit logs (CloudTrail, Cloud Audit Logs).
- Application-level audit (auth events, data access).
