#!/usr/bin/env bash
# Bring up the platform-lab cluster + Cilium CNI + lab namespaces.
# Idempotent: re-running after a partial failure is safe.
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="platform-lab"

echo "==> Creating kind cluster (this takes ~30-60s)"
kind create cluster --config "${LAB_ROOT}/cluster.yaml" --wait 240s || {
  echo "!! Cluster create failed; checking existing state"
  kind get clusters
  exit 1
}

# Single-node clusters have no workers — that's expected on this 2-CPU host.
echo "==> Cluster status"
kubectl get nodes -o wide

echo "==> Installing Cilium CNI (replaces kindnet + kube-proxy)"
# Pin to a known-stable Cilium version so lab 03 reproduces.
CILIUM_VERSION="1.15.4"
helm repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1

helm install cilium cilium/cilium \
  --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  --kube-context "kind-${CLUSTER_NAME}" \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="${CLUSTER_NAME}-control-plane" \
  --set k8sServicePort=6443 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.metrics.enabled="{dns,drop,tcp,flow,port-distribution,icmp,httpV2:exemplars=true}" \
  --set operator.unmanagedPodWatcher.enabled=true \
  --set ipam.mode=kubernetes \
  --set bpf.masquerade=true \
  --wait --timeout 300s

echo "==> Verifying CNI is up"
# It takes ~30s after the Helm install for Cilium to start handling traffic.
for i in {1..30}; do
  if kubectl -n kube-system rollout status ds/cilium --timeout=5s >/dev/null 2>&1; then
    echo "Cilium DaemonSet is ready"
    break
  fi
  echo "  ...waiting for Cilium ($i/30)"
  sleep 5
done

echo "==> Waiting for node to become Ready (CNI now active)"
kubectl wait --for=condition=Ready node --all --timeout=120s

echo "==> Removing control-plane gpu taint (kept for simulated GPU scheduling in labs)"
# The lab demonstrates GPU-only scheduling via tolerations on a workload, NOT
# by tainting the control-plane (which would block coredns/hubble/etc).
# The kind kubeadmConfigPatch applies the taint at node join; we strip it here.
kubectl taint nodes --all gpu=true:NoSchedule- 2>/dev/null || true

echo "==> Removing the leftover kube-proxy DaemonSet"
# Cilium replaced kube-proxy (`kubeProxyReplacement=true`). The original DS
# keeps CrashLoopBackOff-looping; safe to delete.
kubectl -n kube-system delete ds kube-proxy --ignore-not-found 2>/dev/null || true

echo "==> Creating lab namespaces"
kubectl apply -f "${LAB_ROOT}/manifests/namespaces.yaml"

echo "==> Confirming DNS is up"
kubectl -n kube-system rollout status deploy/coredns --timeout=180s

echo "==> Lab cluster is up."
kubectl get nodes
kubectl get pods -A
