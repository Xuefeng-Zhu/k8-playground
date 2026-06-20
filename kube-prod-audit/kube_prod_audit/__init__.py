"""
kube-prod-audit — production-readiness audit for Kubernetes clusters.

Runs ~22 production-readiness checks across workloads, security, reliability,
observability, resources, storage, and network. Produces a terminal summary
and a self-contained HTML report.

Two modes:
  - live:   queries a cluster via `kubectl get -o yaml` (requires kubectl + creds)
  - demo:   runs against realistic mock data so you can see the full report
            without a cluster

Usage:
  python -m kube_prod_audit                     # auto-detect (demo if no cluster)
  python -m kube_prod_audit --mode demo         # force demo
  python -m kube_prod_audit --mode live         # force live
  python -m kube_prod_audit --output report.html
"""
from .runner import AuditResult, CheckResult, Severity, Category
from .report import render_html, render_terminal

__version__ = "0.1.0"


def run_audit(mode: str = "auto", kubeconfig=None, context=None):
    """Programmatic entry point. Used by the CLI and by tests."""
    from .collector import ClusterData
    import socket
    import shutil

    if mode == "auto":
        mode = "live" if shutil.which("kubectl") else "demo"

    if mode == "live":
        data = ClusterData.from_live(kubeconfig=kubeconfig, context=context)
        cluster = context or socket.gethostname()
    elif mode == "demo":
        data = ClusterData.from_demo()
        cluster = "demo-cluster"
    else:
        raise ValueError(f"unknown mode: {mode}")

    # Imported here to avoid a circular import at module load time
    from .checks import ALL_CHECKS

    result = AuditResult(cluster=cluster, mode=mode, checks=[])
    for fn in ALL_CHECKS:
        try:
            result.checks.append(fn(data))
        except Exception as e:
            result.checks.append(CheckResult(
                id="XX-000",
                title=f"Check {fn.__name__} failed to run",
                category=Category.RELIABILITY,
                severity=Severity.WARN,
                summary=f"Internal error: {type(e).__name__}",
                detail=str(e),
                fix="Check the audit logs.",
            ))
    return result


__all__ = [
    "run_audit", "AuditResult", "CheckResult", "Severity", "Category",
    "render_html", "render_terminal",
]
