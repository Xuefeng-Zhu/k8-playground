#!/usr/bin/env python3
"""
Cost-topology refresh helper for dashboards/cost.html.

Runs the cost-simulator, captures its text output, and saves it as
JSON alongside the latest snapshot so the dashboard can show fresh
numbers without re-running python on every page load.

Usage:
  python3 scripts/dashboard-data.py            # one-shot refresh
  python3 scripts/dashboard-data.py --watch    # refresh every 30s
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data" / "cluster-pricing.json"
OUT = ROOT / "dashboards" / "data.json"

# Re-use the cost-sim module (filename has a hyphen, so we load by path).
sys.path.insert(0, str(HERE))
import importlib.util
_spec = importlib.util.spec_from_file_location("cost_sim", HERE / "cost-sim.py")
cost_sim = importlib.util.module_from_spec(_spec)  # type: ignore[name-defined]
sys.modules["cost_sim"] = cost_sim  # required for @dataclass
_spec.loader.exec_module(cost_sim)  # type: ignore[union-attr]


def compute() -> dict:
    clusters, workloads = cost_sim.load(DATA)
    used: dict = {}
    rows = []
    grand = 0.0
    cluster_costs: dict[str, float] = {}
    unschedulable = []

    for wl in workloads:
        result = cost_sim.place(wl, clusters, used)
        if result is None:
            unschedulable.append({
                "id": wl.id, "reason": "capacity exhausted",
                "replicas": wl.replicas, "zone_pref": wl.zone_pref,
            })
            continue

        per_cluster_cost = {}
        for cid, n in result.items():
            cluster = clusters[cid]
            chosen = None
            for ntype, nspec in sorted(cluster["node_types"].items(), key=lambda kv: kv[1]["price_hr"]):
                if wl.gpu > nspec["gpu"]: continue
                if wl.cpu > nspec["cpu"]:  continue
                if wl.mem_gb > nspec["mem_gb"]: continue
                if used[cid].get(ntype, 0) >= 1:
                    chosen = nspec["price_hr"]; break
            if chosen is None: chosen = 0.0
            per_cluster_cost[cid] = n * chosen

        wc = sum(per_cluster_cost.values())
        grand += wc
        for cid, c in per_cluster_cost.items():
            cluster_costs[cid] = cluster_costs.get(cid, 0.0) + c
        rows.append({
            "id": wl.id,
            "placement": ", ".join(f"{cid}×{n}" for cid, n in result.items()),
            "cost_hr": round(wc, 2),
            "replicas": wl.replicas,
            "kind": wl.kind,
            "zone_pref": wl.zone_pref,
        })

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": [
            {"id": cid, "name": c["name"], "region": c["region"],
             "label": c["label"], "node_types": list(c["node_types"].keys()),
             "capacity": c["capacity"]}
            for cid, c in clusters.items()
        ],
        "workloads": rows,
        "unschedulable": unschedulable,
        "totals": {
            "grand_hr": round(grand, 2),
            "grand_month": round(grand * 24 * 30, 2),
            "per_cluster_hr": {cid: round(v, 2) for cid, v in cluster_costs.items()},
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true", help="refresh every 30s")
    args = p.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    while True:
        snapshot = compute()
        OUT.write_text(json.dumps(snapshot, indent=2))
        print(f"[{snapshot['generated_at']}] wrote {OUT}  (${snapshot['totals']['grand_hr']}/hr)")
        if not args.watch:
            break
        time.sleep(30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
