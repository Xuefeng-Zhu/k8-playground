"""
Data collector. Provides a uniform view of cluster resources to the checks,
whether sourced from a live cluster (kubectl) or mock data (demo mode).
"""
import json
import shlex
import subprocess
from typing import Optional


def _run_kubectl(args: list[str], kubeconfig: Optional[str] = None, context: Optional[str] = None) -> str:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    if context:
        cmd += ["--context", context]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


class ClusterData:
    """Unified view of cluster objects for the checks."""

    def __init__(self, raw: dict):
        self.raw = raw
        # Convenience accessors with sensible defaults
        self.namespaces: list[dict] = raw.get("namespaces", [])
        self.deployments: list[dict] = raw.get("deployments", [])
        self.statefulsets: list[dict] = raw.get("statefulsets", [])
        self.daemonsets: list[dict] = raw.get("daemonsets", [])
        self.pods: list[dict] = raw.get("pods", [])
        self.services: list[dict] = raw.get("services", [])
        self.pvcs: list[dict] = raw.get("pvcs", [])
        self.network_policies: list[dict] = raw.get("network_policies", [])
        self.pdbs: list[dict] = raw.get("pdbs", [])
        self.hpas: list[dict] = raw.get("hpas", [])
        self.service_accounts: list[dict] = raw.get("service_accounts", [])
        self.configmaps: list[dict] = raw.get("configmaps", [])
        self.secrets: list[dict] = raw.get("secrets", [])
        self.events: list[dict] = raw.get("events", [])

    @classmethod
    def from_live(cls, kubeconfig: Optional[str] = None, context: Optional[str] = None) -> "ClusterData":
        """Collect from a live cluster via kubectl."""
        raw: dict = {}

        def fetch(resource: str) -> list[dict]:
            out = _run_kubectl(
                ["get", resource, "--all-namespaces", "-o", "json"],
                kubeconfig=kubeconfig, context=context,
            )
            return json.loads(out).get("items", [])

        for r in [
            "namespaces", "deployments", "statefulsets", "daemonsets", "pods",
            "services", "pvc", "networkpolicies", "poddisruptionbudgets.policy",
            "horizontalpodautoscalers.autoscaling", "serviceaccounts",
            "configmaps", "secrets", "events",
        ]:
            try:
                key = {
                    "pvc": "pvcs",
                    "networkpolicies": "network_policies",
                    "poddisruptionbudgets.policy": "pdbs",
                    "horizontalpodautoscalers.autoscaling": "hpas",
                    "serviceaccounts": "service_accounts",
                }.get(r, r)
                raw[key] = fetch(r)
            except subprocess.CalledProcessError:
                raw[key] = []
        return cls(raw)

    @classmethod
    def from_demo(cls) -> "ClusterData":
        """Realistic mock data so the report renders meaningfully without a cluster."""
        from .demo_data import DEMO
        return cls(DEMO)


def is_prod_namespace(ns: dict) -> bool:
    """Heuristic: namespace is 'production' if not kube-system/public, or explicitly labelled."""
    name = ns.get("metadata", {}).get("name", "")
    if name in ("kube-system", "kube-public", "default", "argocd", "flux-system",
                "cert-manager", "ingress-nginx", "monitoring", "prometheus", "loki"):
        return False
    labels = ns.get("metadata", {}).get("labels", {}) or {}
    # PSS restricted label is a strong signal
    if labels.get("pod-security.kubernetes.io/enforce") == "restricted":
        return True
    return True  # treat unknown namespaces as prod for audit purposes
