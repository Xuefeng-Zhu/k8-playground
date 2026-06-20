"""
Production-readiness checks. Each check is a callable that takes a
ClusterData and returns a CheckResult. Check IDs are namespaced:
  WL = Workloads, SC = Security, RL = Reliability, OB = Observability,
  RS = Resources, ST = Storage, NT = Network
"""
from typing import Callable
from ..collector import ClusterData, is_prod_namespace
from ..runner import CheckResult, Severity, Category

CheckFn = Callable[[ClusterData], CheckResult]


# ──────────────────────────────────────────────────────────────────────
# Workloads (WL)
# ──────────────────────────────────────────────────────────────────────

def check_pdb_on_deployments(d: ClusterData) -> CheckResult:
    """Every prod Deployment should have a PodDisruptionBudget."""
    def _freeze(m):
        return tuple(sorted((m or {}).items()))
    pdbs = {(_freeze(p["spec"].get("selector", {}).get("matchLabels", {})), p["metadata"]["namespace"])
            for p in d.pdbs}
    affected = []
    for dep in d.deployments:
        ns = dep["metadata"].get("namespace", "")
        if ns in ("kube-system", "kube-public", "argocd", "flux-system", "cert-manager", "monitoring"):
            continue
        labels = dep["metadata"].get("labels", {})
        if (_freeze(labels), ns) not in pdbs:
            affected.append(f"{ns}/{dep['metadata']['name']}")
    return CheckResult(
        id="WL-001", title="PodDisruptionBudget on Deployments",
        category=Category.WORKLOADS,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} deployment(s) missing a PodDisruptionBudget" if affected
                else "All deployments have a PDB",
        detail="Without a PDB, node maintenance can drain every replica at once.",
        affected=affected,
        fix="Add a PodDisruptionBudget per app: minimum 2, or 50% of replicas.",
        reference="04-workloads-scaling.md#4.1",
    )


def check_liveness_probe(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            if "livenessProbe" not in c:
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="WL-002", title="Liveness probe defined",
        category=Category.WORKLOADS,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) without livenessProbe" if affected
                else "All containers have liveness probes",
        detail="Without liveness, kubelet cannot detect a stuck process.",
        affected=affected,
        fix="Add a livenessProbe: HTTP /healthz or TCP socket.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_readiness_probe(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            if "readinessProbe" not in c:
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="WL-003", title="Readiness probe defined",
        category=Category.WORKLOADS,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) without readinessProbe" if affected
                else "All containers have readiness probes",
        detail="Without readiness, traffic can hit Pods that are not yet ready to serve.",
        affected=affected,
        fix="Add a readinessProbe on a path that becomes 200 only when the app is ready.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_replicas_min(d: ClusterData) -> CheckResult:
    """Deployments should have >= 2 replicas for HA."""
    affected = []
    for dep in d.deployments:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        r = dep["spec"].get("replicas", 1)
        if r < 2:
            affected.append(f"{ns}/{dep['metadata']['name']} (replicas={r})")
    return CheckResult(
        id="WL-004", title="Minimum 2 replicas in prod",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} deployment(s) with < 2 replicas" if affected
                else "All prod deployments have >= 2 replicas",
        detail="Single-replica deployments are not HA. One node failure = 0 replicas.",
        affected=affected,
        fix="Set replicas: 2+ or use HPA with minReplicas >= 2.",
        reference="04-workloads-scaling.md#43-horizontal-pod-autoscaler-hpa",
    )


def check_rolling_update_strategy(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments:
        strat = dep["spec"].get("strategy", {})
        if strat.get("type") == "Recreate":
            affected.append(f"{dep['metadata']['namespace']}/{dep['metadata']['name']}")
    return CheckResult(
        id="WL-005", title="Rolling update strategy",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} deployment(s) using Recreate strategy" if affected
                else "All deployments use RollingUpdate or default",
        detail="Recreate causes downtime during deploys.",
        affected=affected,
        fix="Use RollingUpdate with explicit maxSurge and maxUnavailable.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_no_latest_tag(d: ClusterData) -> CheckResult:
    """Reject images with :latest tag or no tag at all."""
    import re
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            img = c.get("image", "")
            # :latest (explicit)
            if img.endswith(":latest") or img.endswith(":LATEST"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']} ({img})")
                continue
            # No tag at all (defaults to :latest per Docker convention)
            if ":" not in img.split("/")[-1]:
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']} ({img})")
                continue
            # Mutable shorthand like :main, :master, :develop (often aliased to latest)
            tail = img.split(":")[-1]
            if re.match(r"^(main|master|develop|trunk|HEAD|edge)$", tail):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']} ({img})")
    return CheckResult(
        id="WL-006", title="No :latest (or untagged) image references",
        category=Category.WORKLOADS,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) using mutable image refs" if affected
                else "All image references are pinned",
        detail=":latest and untagged refs are mutable; deploys become non-reproducible and rollbacks can break.",
        affected=affected,
        fix="Pin images by digest (image@sha256:...) or by an explicit version tag (:1.4.2, not :latest).",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_image_pull_policy(d: ClusterData) -> CheckResult:
    """Warn if imagePullPolicy is Never or IfNotPresent without a good reason.

    For prod: Always is safest (kubelet re-checks registry). IfNotPresent is fine
    when the tag is pinned (immutable). Never is almost always wrong in prod.
    """
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            policy = c.get("imagePullPolicy", "")
            if policy == "Never":
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']} (Never — image must be pre-loaded)")
    return CheckResult(
        id="WL-007", title="imagePullPolicy: no 'Never' in prod",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) with imagePullPolicy: Never" if affected
                else "No 'Never' image pull policies",
        detail="'Never' requires pre-loading images on every node. A new node or a scaled deployment will fail to pull.",
        affected=affected,
        fix="Use 'Always' (default) or 'IfNotPresent' for prod. Reserve 'Never' for air-gapped clusters.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_priority_class(d: ClusterData) -> CheckResult:
    """Prod workloads should set priorityClassName so they aren't evicted by dev."""
    affected = []
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        pc = dep["spec"]["template"]["spec"].get("priorityClassName", "")
        if not pc:
            affected.append(f"{ns}/{dep['metadata']['name']}")
    return CheckResult(
        id="WL-008", title="priorityClassName set in prod",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} prod workload(s) without priorityClassName" if affected
                else "All prod workloads set priorityClassName",
        detail="Without a priorityClassName, the scheduler treats prod and dev workloads equally. Cluster autoscaler scale-down or preemption can evict prod for dev.",
        affected=affected,
        fix="Add priorityClassName: 'production-critical' (or similar). See Part IV §10.",
        reference="04-workloads-scaling.md#410-priority-classes-and-preemption",
    )


def check_termination_grace_period(d: ClusterData) -> CheckResult:
    """terminationGracePeriodSeconds default is 30s. Anything below that risks in-flight requests."""
    affected = []
    for dep in d.deployments + d.statefulsets:
        tgp = dep["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")
        if tgp is not None and tgp < 30:
            ns = dep["metadata"].get("namespace", "")
            affected.append(f"{ns}/{dep['metadata']['name']} (={tgp}s)")
    return CheckResult(
        id="WL-009", title="terminationGracePeriodSeconds ≥ 30s",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} workload(s) with grace period < 30s" if affected
                else "All workloads respect the 30s minimum grace period",
        detail="A short grace period drops in-flight TCP connections mid-request. The default (30s) is the minimum for graceful drain.",
        affected=affected,
        fix="Either omit the field (use the 30s default) or set it explicitly to >= 30s. Some apps need 60–120s for connection draining.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def check_topology_spread(d: ClusterData) -> CheckResult:
    """Multi-replica prod workloads should have topology spread constraints."""
    affected = []
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        if dep["spec"].get("replicas", 1) < 3:
            continue
        spread = dep["spec"]["template"]["spec"].get("topologySpreadConstraints", [])
        if not spread:
            # Soft signal — many clusters rely on default scheduler behaviour
            affected.append(f"{ns}/{dep['metadata']['name']}")
    return CheckResult(
        id="WL-010", title="topologySpreadConstraints for multi-replica prod",
        category=Category.WORKLOADS,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} workload(s) without explicit topology spread" if affected
                else "All multi-replica workloads specify topology spread",
        detail="Without topologySpreadConstraints, all replicas can land on the same node or zone. One node failure = service down despite 'HA'.",
        affected=affected,
        fix="Add topologySpreadConstraints with maxSkew: 1, topologyKey: topology.kubernetes.io/zone (or kubernetes.io/hostname for node-level).",
        reference="04-workloads-scaling.md#43-horizontal-pod-autoscaler-hpa",
    )


# ──────────────────────────────────────────────────────────────────────
# Security (SC)
# ──────────────────────────────────────────────────────────────────────

def check_pod_security_standard(d: ClusterData) -> CheckResult:
    """Production namespaces should enforce Pod Security Standard 'restricted'."""
    bad = []
    for ns in d.namespaces:
        name = ns["metadata"]["name"]
        if not is_prod_namespace(ns):
            continue
        labels = ns["metadata"].get("labels", {}) or {}
        enforce = labels.get("pod-security.kubernetes.io/enforce", "missing")
        if enforce not in ("restricted",):
            bad.append(f"{name} (enforce={enforce})")
    return CheckResult(
        id="SC-001", title="Pod Security Standard 'restricted' in prod",
        category=Category.SECURITY,
        severity=Severity.FAIL if bad else Severity.PASS,
        summary=f"{len(bad)} prod namespace(s) not enforcing 'restricted'" if bad
                else "All prod namespaces enforce 'restricted'",
        detail="PSS restricted is the production baseline; it blocks privileged, hostNetwork, etc.",
        affected=bad,
        fix="Set label pod-security.kubernetes.io/enforce=restricted on the namespace.",
        reference="06-security.md#62-pod-security-standards-pss",
    )


def check_privileged_containers(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            sc = c.get("securityContext", {}) or {}
            if sc.get("privileged"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="SC-002", title="No privileged containers",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} privileged container(s)" if affected
                else "No privileged containers",
        detail="A privileged container can escape to the host. Almost never required.",
        affected=affected,
        fix="Drop privileged: true. Use specific capabilities (NET_ADMIN, SYS_TIME) if needed.",
        reference="06-security.md#68-runtime-security",
    )


def check_run_as_non_root(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            sc = c.get("securityContext", {}) or {}
            if not sc.get("runAsNonRoot"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="SC-003", title="runAsNonRoot: true on all containers",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) without runAsNonRoot" if affected
                else "All containers set runAsNonRoot",
        detail="Containers running as root can read any file the kubelet can.",
        affected=affected,
        fix="Add runAsNonRoot: true and an explicit runAsUser (non-zero).",
        reference="06-security.md#68-runtime-security",
    )


def check_seccomp_profile(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            sc = c.get("securityContext", {}) or {}
            sp = sc.get("seccompProfile", {}) or {}
            if sp.get("type") in (None, "Unconfined"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="SC-004", title="seccompProfile RuntimeDefault",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) without RuntimeDefault seccomp" if affected
                else "All containers set seccompProfile.type=RuntimeDefault",
        detail="seccomp reduces the kernel attack surface available to a compromised container.",
        affected=affected,
        fix="Set seccompProfile.type: RuntimeDefault.",
        reference="06-security.md#68-runtime-security",
    )


def check_default_service_account(d: ClusterData) -> CheckResult:
    """Pods should not use the 'default' SA in prod."""
    affected = []
    default_sa = {(sa["metadata"]["namespace"], sa["metadata"]["name"])
                  for sa in d.service_accounts}
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        sa_name = dep["spec"]["template"]["spec"].get("serviceAccountName", "default")
        if sa_name == "default" and (ns, "default") in default_sa:
            affected.append(f"{ns}/{dep['metadata']['name']}")
    return CheckResult(
        id="SC-005", title="Workloads use a specific ServiceAccount, not 'default'",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} prod workload(s) using the default SA" if affected
                else "All prod workloads use a specific SA",
        detail="A leaked 'default' SA token gives cluster-scoped read in many setups.",
        affected=affected,
        fix="Create a ServiceAccount per app and reference it via spec.serviceAccountName.",
        reference="06-security.md#63-rbac",
    )


def check_automount_disabled(d: ClusterData) -> CheckResult:
    """Pod specs that don't need the API should disable automount."""
    candidates = []
    for dep in d.deployments + d.statefulsets:
        spec = dep["spec"]["template"]["spec"]
        if "serviceAccountName" in spec:
            continue  # we set a specific SA, acceptable
        if not spec.get("automountServiceAccountToken") is False:
            ns = dep["metadata"].get("namespace", "")
            candidates.append(f"{ns}/{dep['metadata']['name']}")
    return CheckResult(
        id="SC-006", title="automountServiceAccountToken disabled where unnecessary",
        category=Category.SECURITY,
        severity=Severity.WARN if candidates else Severity.PASS,
        summary=f"{len(candidates)} workload(s) with default token automount" if candidates
                else "Token automount disabled where not needed",
        detail="Default automount means every Pod carries a token it probably doesn't need.",
        affected=candidates,
        fix="Set automountServiceAccountToken: false, or set a scoped ServiceAccount.",
        reference="06-security.md#64-serviceaccounts-and-tokens",
    )


def check_secrets_in_env(d: ClusterData) -> CheckResult:
    """Detect env vars that look like hardcoded secrets (heuristic)."""
    import re
    secret_pat = re.compile(r"(password|secret|api[_-]?key|token|credential)", re.I)
    affected = []
    for dep in d.deployments + d.statefulsets:
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            for env in c.get("env", []):
                v = env.get("value", "")
                if secret_pat.search(env.get("name", "")) and v and not v.startswith("$(ENV)"):
                    if len(v) > 8 and not v.startswith("/") and "Ref" not in str(env.get("valueFrom", "")):
                        affected.append(
                            f"{dep['metadata'].get('namespace','')}/{dep['metadata']['name']}/{c['name']}/{env['name']}"
                        )
    return CheckResult(
        id="SC-007", title="No hardcoded secrets in env vars",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} hardcoded secret(s) in env" if affected
                else "No obvious hardcoded secrets in env vars",
        detail="Env-var secrets are visible in kubectl describe, crash dumps, and child processes.",
        affected=affected[:10],
        fix="Mount secrets as files via secretStore CSI / Vault, or use External Secrets Operator.",
        reference="06-security.md#65-secrets-management",
    )


def check_no_host_namespaces(d: ClusterData) -> CheckResult:
    """hostNetwork, hostPID, hostIPC share the node's namespace — a container
    escape gives full node access.
    """
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        ps = dep["spec"]["template"]["spec"]
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            if ps.get(flag):
                affected.append(f"{ns}/{dep['metadata']['name']} ({flag}=true)")
    return CheckResult(
        id="SC-008", title="No hostNetwork / hostPID / hostIPC",
        category=Category.SECURITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} workload(s) sharing host namespace" if affected
                else "No workloads share host namespaces",
        detail="Sharing the host namespace means a container escape = full node compromise. Almost never required outside node agents.",
        affected=affected,
        fix="Drop hostNetwork/hostPID/hostIPC. If you must use them (kube-proxy, node-local DNS), run on dedicated, tainted nodes.",
        reference="06-security.md#68-runtime-security",
    )


# ──────────────────────────────────────────────────────────────────────
# Reliability (RL)
# ──────────────────────────────────────────────────────────────────────

def check_replicas_ha(d: ClusterData) -> CheckResult:
    """Deployments + HPA minReplicas should be >= 2 for HA."""
    affected = []
    hpas = {(h["spec"]["scaleTargetRef"].get("name"), h["metadata"]["namespace"])
            for h in d.hpas}
    for dep in d.deployments:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        r = dep["spec"].get("replicas", 1)
        if (dep["metadata"]["name"], ns) in hpas:
            h = next(h for h in d.hpas
                     if h["spec"]["scaleTargetRef"].get("name") == dep["metadata"]["name"]
                     and h["metadata"]["namespace"] == ns)
            if h["spec"].get("minReplicas", 1) < 2:
                affected.append(f"{ns}/{dep['metadata']['name']} (HPA min={h['spec'].get('minReplicas')})")
        elif r < 2:
            affected.append(f"{ns}/{dep['metadata']['name']} (replicas={r})")
    return CheckResult(
        id="RL-001", title="HA replica count (>= 2 or HPA min >= 2)",
        category=Category.RELIABILITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} workload(s) below HA threshold" if affected
                else "All prod workloads meet HA replica count",
        detail="HA requires at least 2 replicas so one can fail without taking the service down.",
        affected=affected,
        fix="Bump replicas to 2+ or set HPA minReplicas: 2.",
        reference="04-workloads-scaling.md#43-horizontal-pod-autoscaler-hpa",
    )


def check_max_unavailable(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments:
        strat = dep["spec"].get("strategy", {})
        ru = strat.get("rollingUpdate", {}) if strat.get("type") != "Recreate" else {}
        mu = ru.get("maxUnavailable")
        if mu is None:
            continue  # using default 25%
        if isinstance(mu, int) and mu >= dep["spec"].get("replicas", 1):
            affected.append(f"{dep['metadata']['namespace']}/{dep['metadata']['name']} (maxUnavailable={mu})")
        elif isinstance(mu, str) and mu.endswith("%") and int(mu.rstrip("%")) >= 100:
            affected.append(f"{dep['metadata']['namespace']}/{dep['metadata']['name']} (maxUnavailable={mu})")
    return CheckResult(
        id="RL-002", title="maxUnavailable won't zero replicas",
        category=Category.RELIABILITY,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(rolling_up_doom(d))} unsafe rolling-update setting(s)" if affected
                else "Rolling-update limits are within bounds",
        detail="A maxUnavailable >= replicas will zero the service during a deploy.",
        affected=affected,
        fix="Set maxUnavailable to 25% or less, or to an integer < replicas.",
        reference="04-workloads-scaling.md#41-deployments-and-the-rolling-update-contract",
    )


def rolling_up_doom(d: ClusterData) -> list[str]:
    out = []
    for dep in d.deployments:
        strat = dep["spec"].get("strategy", {})
        ru = strat.get("rollingUpdate", {}) if strat.get("type") != "Recreate" else {}
        mu = ru.get("maxUnavailable")
        if mu is None:
            continue
        r = dep["spec"].get("replicas", 1)
        if isinstance(mu, int) and mu >= r:
            out.append(dep["metadata"]["name"])
    return out


def check_oom_events(d: ClusterData) -> CheckResult:
    affected = [e.get("involvedObject", {}).get("name", "?")
                for e in d.events if e.get("reason") == "OOMKilled"]
    return CheckResult(
        id="RL-003", title="No recent OOMKilled events",
        category=Category.RELIABILITY,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} OOMKilled event(s) in cluster" if affected
                else "No OOMKilled events observed",
        detail="OOMKilled means memory limits are too tight or there is a leak.",
        affected=affected[:10],
        fix="Raise memory limits, or profile the app for a leak.",
        reference="04-workloads-scaling.md#42-resource-management",
    )


def check_failed_scheduling(d: ClusterData) -> CheckResult:
    affected = [e.get("involvedObject", {}).get("name", "?")
                for e in d.events if e.get("reason") == "FailedScheduling"]
    return CheckResult(
        id="RL-004", title="No FailedScheduling events",
        category=Category.RELIABILITY,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} FailedScheduling event(s)" if affected
                else "No FailedScheduling events",
        detail="Pods that can't be scheduled sit Pending — either resource shortage or affinity bug.",
        affected=affected[:10],
        fix="Check node capacity, taints, and pod affinity. Investigate pending Pods.",
        reference="02-control-plane.md#25-the-scheduler-in-detail",
    )


# ──────────────────────────────────────────────────────────────────────
# Observability (OB)
# ──────────────────────────────────────────────────────────────────────

def check_hpa_present(d: ClusterData) -> CheckResult:
    """At least 1 HPA suggests autoscaling is configured. Soft signal."""
    return CheckResult(
        id="OB-001", title="HPA configured for prod services",
        category=Category.OBSERVABILITY,
        severity=Severity.WARN if not d.hpas else Severity.PASS,
        summary=f"{len(d.hpas)} HPA(s) configured" if d.hpas
                else "No HPAs configured anywhere in the cluster",
        detail="HPAs let you scale on metrics, not just replica counts.",
        affected=[],
        fix="Add a HorizontalPodAutoscaler targeting your prod Deployments.",
        reference="04-workloads-scaling.md#43-horizontal-pod-autoscaler-hpa",
    )


def check_logging_agent(d: ClusterData) -> CheckResult:
    """A node-level log shipper (DaemonSet in kube-system or monitoring)."""
    has = any(
        ds["metadata"].get("namespace") in ("kube-system", "monitoring", "logging")
        for ds in d.daemonsets
    )
    return CheckResult(
        id="OB-002", title="Cluster-wide log shipper (DaemonSet)",
        category=Category.OBSERVABILITY,
        severity=Severity.FAIL if not has else Severity.PASS,
        summary="No cluster-wide log shipper detected" if not has
                else "Log shipper DaemonSet detected",
        detail="Without a node-level log agent, container logs are lost on Pod restart.",
        affected=[],
        fix="Install Fluent Bit, Vector, or Filebeat as a DaemonSet with tolerations.",
        reference="08-observability.md#83-logs",
    )


def check_metrics_pipeline(d: ClusterData) -> CheckResult:
    """metrics-server presence (proxied by HPA existing or kube-state-metrics)."""
    has_ksm = any(
        "kube-state" in ds["metadata"]["name"] or "ksm" in ds["metadata"]["name"]
        for ds in d.daemonsets + d.deployments
    )
    return CheckResult(
        id="OB-003", title="Cluster metrics pipeline present",
        category=Category.OBSERVABILITY,
        severity=Severity.WARN if not has_ksm and not d.hpas else Severity.PASS,
        summary="No metrics-server or kube-state-metrics detected" if not has_ksm
                else "Metrics pipeline present (HPA or kube-state-metrics)",
        detail="HPA, kubectl top, and many dashboards depend on metrics-server / kube-state-metrics.",
        affected=[],
        fix="Install metrics-server and kube-state-metrics.",
        reference="08-observability.md#82-metrics-prometheus-and-the-ecosystem",
    )


def check_health_endpoint_defined(d: ClusterData) -> CheckResult:
    """Heuristic: does the container expose a health endpoint?

    Looks for /healthz, /health, /ready, /live, /ping, /status, or actuator paths
    in liveness/readiness probes. Containers without ANY probe and no obvious
    health path are flagged — they're likely DOA.
    """
    import re
    health_paths = re.compile(
        r"/(healthz|health|ready|readyz|live|livez|ping|status|actuator/health)",
        re.I,
    )
    affected = []
    for dep in d.deployments + d.statefulsets:
        ns = dep["metadata"].get("namespace", "")
        if not is_prod_namespace({"metadata": {"name": ns}}):
            continue
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            probes = (c.get("livenessProbe"), c.get("readinessProbe"))
            has_path = False
            for probe in probes:
                if not probe:
                    continue
                # httpGet has a path
                http = probe.get("httpGet") or {}
                if http.get("path") and health_paths.search(http["path"]):
                    has_path = True
                    break
                # exec probes with curl/wget commands
                exec_ = probe.get("exec") or {}
                cmd = " ".join(exec_.get("command", []) or [])
                if health_paths.search(cmd):
                    has_path = True
                    break
            if not has_path:
                # Skip if NO probe at all (caught by WL-002/003 already)
                if "livenessProbe" in c or "readinessProbe" in c:
                    affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="OB-004", title="Probes use a known health endpoint",
        category=Category.OBSERVABILITY,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) with probes pointing to unknown paths" if affected
                else "All probes use standard health endpoints",
        detail="Probes that point to non-existent paths are false positives. The container appears healthy even when broken.",
        affected=affected,
        fix="Use /healthz (or framework's equivalent: Spring actuator/health, FastAPI /health, Express /health).",
        reference="08-observability.md#82-metrics-prometheus-and-the-ecosystem",
    )


# ──────────────────────────────────────────────────────────────────────
# Resources (RS)
# ──────────────────────────────────────────────────────────────────────

def check_resource_limits(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            r = c.get("resources", {}) or {}
            limits = r.get("limits", {}) or {}
            if not limits.get("cpu") or not limits.get("memory"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="RS-001", title="CPU and memory limits set",
        category=Category.RESOURCES,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) missing limits" if affected
                else "All containers have CPU and memory limits",
        detail="Without memory limits, a leak fills the node and gets the Pod OOM-killed.",
        affected=affected,
        fix="Add resources.limits.cpu and resources.limits.memory for every container.",
        reference="04-workloads-scaling.md#42-resource-management",
    )


def check_resource_requests(d: ClusterData) -> CheckResult:
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            r = c.get("resources", {}) or {}
            reqs = r.get("requests", {}) or {}
            if not reqs.get("cpu") or not reqs.get("memory"):
                affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']}")
    return CheckResult(
        id="RS-002", title="CPU and memory requests set",
        category=Category.RESOURCES,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) missing requests" if affected
                else "All containers have CPU and memory requests",
        detail="Requests drive scheduling. Without them, Pods are BestEffort and killed first.",
        affected=affected,
        fix="Add resources.requests.cpu and resources.requests.memory.",
        reference="04-workloads-scaling.md#42-resource-management",
    )


def _parse_mem(v) -> int:
    """Parse Kubernetes memory strings to bytes. Handles K, M, G, T suffixes."""
    if v is None: return 0
    if isinstance(v, (int, float)): return int(v)
    s = str(v).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGTP]i?)?$", s)
    if not m: return 0
    n = float(m.group(1))
    suf = (m.group(2) or "").rstrip("i")
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}.get(suf, 1)
    return int(n * mult)


def check_memory_limit_vs_request(d: ClusterData) -> CheckResult:
    """Memory limit must be >= memory request.

    A limit lower than the request causes immediate OOM on the first spike.
    A limit only marginally above the request causes OOM on bursts.
    A limit equal to the request causes OOM on any allocation burst.
    """
    import re as _re
    affected = []
    for dep in d.deployments + d.statefulsets + d.daemonsets:
        ns = dep["metadata"].get("namespace", "")
        for c in dep["spec"]["template"]["spec"].get("containers", []):
            r = (c.get("resources", {}) or {}).get("requests", {}) or {}
            l = (c.get("resources", {}) or {}).get("limits", {}) or {}
            mem_req = r.get("memory")
            mem_lim = l.get("memory")
            if mem_req and mem_lim:
                req_b = _parse_mem(mem_req)
                lim_b = _parse_mem(mem_lim)
                if req_b > 0 and lim_b > 0 and lim_b < req_b:
                    affected.append(f"{ns}/{dep['metadata']['name']}/{c['name']} (request={mem_req}, limit={mem_lim})")
    return CheckResult(
        id="RS-003", title="Memory limit >= memory request",
        category=Category.RESOURCES,
        severity=Severity.FAIL if affected else Severity.PASS,
        summary=f"{len(affected)} container(s) with limit < request" if affected
                else "All memory limits are >= requests",
        detail="A limit lower than the request is a hard OOM on first spike — the kubelet won't even let the container reach its request.",
        affected=affected,
        fix="Set limit >= request (typically 1.5–2× the request for headroom on bursts).",
        reference="04-workloads-scaling.md#42-resource-management",
    )


# ──────────────────────────────────────────────────────────────────────
# Storage (ST)
# ──────────────────────────────────────────────────────────────────────

def check_storage_class_binding(d: ClusterData) -> CheckResult:
    affected = []
    for pvc in d.pvcs:
        if pvc.get("status", {}).get("phase") not in ("Bound",):
            affected.append(f"{pvc['metadata'].get('namespace','')}/{pvc['metadata']['name']} ({pvc.get('status',{}).get('phase')})")
    return CheckResult(
        id="ST-001", title="All PVCs Bound",
        category=Category.STORAGE,
        severity=Severity.WARN if affected else Severity.PASS,
        summary=f"{len(affected)} unbound PVC(s)" if affected
                else "All PVCs are Bound",
        detail="Pending PVCs mean the storage provisioner failed or the StorageClass is wrong.",
        affected=affected,
        fix="Check StorageClass and CSI driver health; look for events on the PVC.",
        reference="05-storage.md#54-storageclass-and-dynamic-provisioning",
    )


# ──────────────────────────────────────────────────────────────────────
# Network (NT)
# ──────────────────────────────────────────────────────────────────────

def check_network_policy(d: ClusterData) -> CheckResult:
    """At least one NetworkPolicy in each prod namespace (default-deny or similar)."""
    prod_nss = {ns["metadata"]["name"] for ns in d.namespaces
                if is_prod_namespace(ns)}
    have_nps: set[str] = set()
    for np in d.network_policies:
        have_nps.add(np["metadata"].get("namespace", ""))
    missing = sorted(prod_nss - have_nps - {"kube-system", "kube-public"})
    return CheckResult(
        id="NT-001", title="NetworkPolicy in each prod namespace",
        category=Category.NETWORK,
        severity=Severity.FAIL if missing else Severity.PASS,
        summary=f"{len(missing)} prod namespace(s) without NetworkPolicy" if missing
                else "Every prod namespace has a NetworkPolicy",
        detail="Without a NetworkPolicy, all Pods can talk to all Pods (and the internet).",
        affected=missing,
        fix="Add a default-deny NetworkPolicy per namespace, then allow only what you need.",
        reference="03-networking.md#36-networkpolicy",
    )


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────

ALL_CHECKS: list[CheckFn] = [
    # Workloads
    check_pdb_on_deployments,
    check_liveness_probe,
    check_readiness_probe,
    check_replicas_min,
    check_rolling_update_strategy,
    check_no_latest_tag,
    check_image_pull_policy,
    check_priority_class,
    check_termination_grace_period,
    check_topology_spread,
    # Security
    check_pod_security_standard,
    check_privileged_containers,
    check_run_as_non_root,
    check_seccomp_profile,
    check_default_service_account,
    check_automount_disabled,
    check_secrets_in_env,
    check_no_host_namespaces,
    # Reliability
    check_replicas_ha,
    check_max_unavailable,
    check_oom_events,
    check_failed_scheduling,
    # Observability
    check_hpa_present,
    check_logging_agent,
    check_metrics_pipeline,
    check_health_endpoint_defined,
    # Resources
    check_resource_limits,
    check_resource_requests,
    check_memory_limit_vs_request,
    # Storage
    check_storage_class_binding,
    # Network
    check_network_policy,
]  # total: 30 checks
