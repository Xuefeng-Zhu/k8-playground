#!/usr/bin/env bash
# Probe eBPF program counts and write them to dashboards/bpf-prog-counts.json
# so the dashboard can render them without shelling out from the browser.
#
# Usage:  scripts/bpf-prog-count.sh
# Loop:   while true; do scripts/bpf-prog-count.sh; sleep 30; done
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${LAB_ROOT}/dashboards/bpf-prog-counts.json"

# Cilium's eBPF programs live inside the agent's mount namespace.
# bpftool is at /usr/local/bin/bpftool inside the agent image.
RAW=$(kubectl --context kind-platform-lab -n kube-system exec ds/cilium -- \
  /usr/local/bin/bpftool prog show 2>/dev/null | awk '/^[0-9]+:/ {print $2}' | sort | uniq -c || echo "")

# Build a small JSON object.
TOTAL=$(echo "$RAW" | awk '{s+=$1} END{print s+0}')
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

{
  echo "{"
  echo "  \"generated_at\": \"${TIMESTAMP}\","
  echo "  \"total\": ${TOTAL},"
  echo "  \"by_type\": {"
  FIRST=1
  while read -r count type; do
    [ -z "$type" ] && continue
    [ $FIRST -eq 0 ] && echo ","
    FIRST=0
    echo -n "    \"${type}\": ${count}"
  done <<< "$RAW"
  echo ""
  echo "  }"
  echo "}"
} > "${OUT}"

echo "wrote ${OUT} (total=${TOTAL} programs)"
