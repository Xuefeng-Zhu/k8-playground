#!/usr/bin/env bash
# Tear down the platform-lab cluster cleanly.
set -euo pipefail

CLUSTER_NAME="${1:-platform-lab}"

echo "==> Tearing down cluster ${CLUSTER_NAME}"
kind delete cluster --name "${CLUSTER_NAME}"

echo "==> Cleaning up dangling container networks (best-effort)"
docker network ls --filter "name=kind" --format '{{.Name}}' | xargs -r docker network rm >/dev/null 2>&1 || true

echo "==> Done."
