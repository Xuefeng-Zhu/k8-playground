#!/usr/bin/env python3
"""
Cost & placement optimizer — Infrastructure Strategy exercise.

Reads data/cluster-pricing.json, runs a small greedy bin-packing +
placement optimizer, and prints where each workload should land to
minimize hourly $ while respecting:
  - per-cluster capacity
  - zone preference (EU prefers on-prem; stateless can go either)
  - GPU workloads only go to nodes that have GPU capacity

This is intentionally small (~150 LOC) so you can read it end-to-end
and then deliberately change the policy. The whole point of lab 04 is
to break it on purpose and see what happens.
"""
from __future__ import annotations
import json, sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "cluster-pricing.json"


@dataclass
class Workload:
    id: str
    cpu: int
    mem_gb: int
    gpu: int
    replicas: int
    kind: str
    zone_pref: str


def load(path: Path = DATA) -> tuple[dict, list[Workload]]:
    raw = json.loads(path.read_text())
    workloads = [Workload(**{k: w[k] for k in ("id", "cpu", "mem_gb", "gpu", "replicas", "kind", "zone_pref")})
                 for w in raw["workloads"]]
    return raw["clusters"], workloads


def place(workload: Workload, clusters: dict, used_by_cluster: dict) -> dict[str, int] | None:
    """
    Greedy: choose an ordered list of clusters whose region matches the
    workload's zone_pref, then place each replica on the cheapest fit.
    Returns {cluster_id: replicas_placed} or None if any replica is unschedulable.

    Capacity model: each cluster has N physical nodes of each type.
    A replica consumes ONE whole node from the cluster's capacity pool.

    zone_pref semantics:
      - "any": every cluster is eligible (we try in cheapest-first order)
      - "eu":  only clusters whose region starts with "eu-" (e.g. eu-central-1)
              or whose name is in the EU are eligible. If none can fit,
              the workload is UNSCHEDULABLE — we do NOT silently fall back
              to a non-EU cluster, because that violates data residency.
      - anything else: clusters whose region starts with that prefix.
    """
    def region_matches(cluster: dict) -> bool:
        pref = workload.zone_pref
        if pref == "any":
            return True
        region = cluster.get("region", "").lower()
        return region.startswith(pref)

    # Eligible clusters in a stable order. Without a real cost model for
    # cross-region networking, "stable" = whatever the JSON defines. The
    # main constraint the placement string tells you about is WHICH clusters
    # were tried, not in what order — because zone_pref gates the set.
    eligible = [cid for cid, c in clusters.items() if region_matches(c)]
    # Sort eligible clusters by cheapest-available node price so the
    # optimizer naturally prefers the cheapest valid region.
    def cheapest_node_price(cid: str) -> float:
        return min((n["price_hr"] for n in clusters[cid]["node_types"].values()), default=1e9)
    eligible.sort(key=cheapest_node_price)

    # Make sure per-cluster state is initialized.
    for cid in eligible:
        used_by_cluster.setdefault(cid, {})

    per_cluster: dict[str, int] = {}

    for replica in range(workload.replicas):
        placed = False
        for cid in eligible:
            cluster = clusters[cid]
            types_by_price = sorted(cluster["node_types"].items(),
                                    key=lambda kv: kv[1]["price_hr"])
            for ntype, nspec in types_by_price:
                if workload.gpu > nspec["gpu"]:    continue
                if workload.cpu   > nspec["cpu"]:   continue
                if workload.mem_gb > nspec["mem_gb"]: continue
                cap = cluster["capacity"].get(ntype, 0)
                consumed = used_by_cluster[cid].get(ntype, 0)
                if consumed >= cap:
                    continue
                used_by_cluster[cid][ntype] = consumed + 1
                per_cluster[cid] = per_cluster.get(cid, 0) + 1
                placed = True
                break
            if placed:
                break
        if not placed:
            return None

    return per_cluster


def main() -> int:
    clusters, workloads = load()
    used_by_cluster: dict[str, dict] = {}
    grand_total = 0.0
    cluster_costs: dict[str, float] = {}

    print(f"{'WORKLOAD':<16} {'PLACEMENT':<28} {'$/HR':>8}  NOTES")
    print("-" * 76)
    for wl in workloads:
        result = place(wl, clusters, used_by_cluster)
        if result is None:
            print(f"{wl.id:<16} {'UNSCHEDULABLE':<28} {'-':>8}  capacity exhausted")
            continue

        # Build a per-replica $ figure by spreading replica cost across clusters.
        # Each replica is one whole node; we have the cluster id per replica via `result`.
        # Re-compute per-replica cost for display.
        per_cluster_cost = {}
        # Use the same per-type price the place() function used; we look it up by
        # type counts now in used_by_cluster, attributed by walking workloads again.
        # Simpler: re-run a read-only cost tally from used_by_cluster snapshot.
        # Since cost is the same as: (#nodes_consumed_in_cluster) * (avg node price for that cluster)
        # we instead just sum cost during placement — refactor: place returns both.
        # To keep this small, recompute here from the per-cluster replica counts and current price list.
        for cid, n in result.items():
            cluster = clusters[cid]
            # All replicas of this workload went to the same node type per cluster.
            # Find the cheapest node type that fits the workload and was used.
            chosen = None
            for ntype, nspec in sorted(cluster["node_types"].items(), key=lambda kv: kv[1]["price_hr"]):
                if wl.gpu > nspec["gpu"]: continue
                if wl.cpu > nspec["cpu"]:  continue
                if wl.mem_gb > nspec["mem_gb"]: continue
                if used_by_cluster[cid].get(ntype, 0) >= 1:
                    chosen = nspec["price_hr"]
                    break
            if chosen is None:
                chosen = 0.0
            per_cluster_cost[cid] = n * chosen

        workload_cost = sum(per_cluster_cost.values())
        grand_total += workload_cost
        placement_str = ", ".join(f"{cid}×{n}" for cid, n in result.items())
        for cid, cost in per_cluster_cost.items():
            cluster_costs[cid] = cluster_costs.get(cid, 0.0) + cost

        note = ""
        if len(result) == 1:
            only_cid = next(iter(result))
            placed_region = clusters[only_cid].get("region", "")
            if wl.zone_pref == "any" and placed_region.startswith("eu-"):
                note = "landed in EU (cheapest valid)"
            elif wl.zone_pref == "any" and not placed_region.startswith("eu-"):
                note = "landed in elastic tier"
            elif wl.zone_pref != "any" and placed_region.startswith(wl.zone_pref):
                note = f"stayed in {wl.zone_pref} (data residency)"
            else:
                note = f"fell back to {placed_region}"
        else:
            note = "spilled across clusters"
        print(f"{wl.id:<16} {placement_str:<28} {workload_cost:>7.2f}  {note}")

    print("-" * 76)
    print(f"{'TOTAL':<16} {'':<28} {grand_total:>7.2f} $/hr  ({(grand_total*24*30):.2f} $/month projected)")
    for cid, cost in cluster_costs.items():
        print(f"  · {cid:<26} {cost:>7.2f} $/hr")
    return 0


if __name__ == "__main__":
    sys.exit(main())
