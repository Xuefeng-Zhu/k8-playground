# Lab 04 — Multi-cluster cost & placement optimizer

> **Topic:** Infrastructure Strategy (hybrid/multi-cloud, cost optimization)
> **Goal:** Use the local cost-simulator to make real placement decisions
> under different constraints — capacity, GPU availability, data residency —
> and understand *why* the optimizer chose what it chose.

---

## Why this matters

In a real multi-cloud setup, the question isn't "is cloud A cheaper?" — it's
"given this workload's constraints (latency, residency, GPU needs, replicas),
and given today's capacity in each cluster, where should each replica land?"

The cost-simulator (`scripts/cost-sim.py`) is a small, readable version of
the kind of optimizer that a Karpenter/Cluster Autoscaler/Cast.ai team would
build. You can read it end-to-end in ~150 lines and *then* deliberately
break it on purpose — which is the most important part of this lab.

## Pre-flight

```bash
cd /path/to/k8s-platform-lab
# Default config: small on-prem (capacity-bound) + large AWS (elastic).
jq '.clusters | keys' data/cluster-pricing.json
jq '.workloads[] | .id' data/cluster-pricing.json
```

You should see:
- Clusters: `aws-us-east-1`, `on-prem-eu`
- Workloads: `checkout-api`, `search-indexer`, `llm-train`

## Steps

### 1. Baseline: where does everything land with default config?

```bash
python3 scripts/cost-sim.py
```

**Expected output (numbers will match exactly):**
```
WORKLOAD         PLACEMENT                        $/HR  NOTES
checkout-api     on-prem-eu×6                    1.08  stayed in eu (data residency)
search-indexer   on-prem-eu×2                    0.72  landed in EU (cheapest valid)
llm-train        on-prem-eu×1                    3.10  landed in EU (cheapest valid)
TOTAL                                            4.90 $/hr  (3528.00 $/month projected)
  · on-prem-eu                    4.90 $/hr
```

Three things to notice:
- `checkout-api` has `zone_pref: "eu"`, so it never goes to AWS even though
  AWS has more capacity. **Data residency is a hard constraint.**
- `llm-train` needs GPU. It lands on-prem because on-prem GPU is
  10x cheaper per hour (3.10 vs 6.20).
- All replicas fit in on-prem capacity. AWS isn't used at all.

### 2. Make on-prem GPU scarce: what happens?

```bash
# Simulate "we sold our GPUs to another team" — drop on-prem GPU to 0
# and bump the LLM training job from 1 to 3 replicas.
jq '.clusters["on-prem-eu"].capacity["gpu.a100"]=0 | .workloads[2].replicas=3' \
   data/cluster-pricing.json > /tmp/cp.json && mv /tmp/cp.json data/cluster-pricing.json
python3 scripts/cost-sim.py
```

**Expected output:**
```
WORKLOAD         PLACEMENT                        $/HR  NOTES
checkout-api     on-prem-eu×6                    1.08  stayed in eu (data residency)
search-indexer   on-prem-eu×2                    0.72  landed in EU (cheapest valid)
llm-train        aws-us-east-1×3                18.60  landed in elastic tier
TOTAL                                           20.40 $/hr  (14688.00 $/month projected)
  · on-prem-eu                    1.80 $/hr
  · aws-us-east-1                18.60 $/hr
```

The training job now spills to AWS at 6x the cost. The on-prem workload
*didn't* get displaced — the optimizer only changed the GPU job. This is
the multi-cloud moment: pay 6x for elastic GPU, or wait. There's no
"best" answer — only a decision a platform team has to make.

### 3. Pin the LLM job to on-prem and watch it become UNSCHEDULABLE

```bash
# Restore GPU capacity, but force the training job to *require* on-prem.
# This is the "data can't leave the EU" scenario for a model that needs
# training data that lives on-prem.
jq '.clusters["on-prem-eu"].capacity["gpu.a100"]=2 | .workloads[2].zone_pref="eu" | .workloads[2].replicas=1' \
   data/cluster-pricing.json > /tmp/cp.json && mv /tmp/cp.json data/cluster-pricing.json
python3 scripts/cost-sim.py
```

LLM lands on-prem (cheap, in-EU, GPU OK). Now break it on purpose:```bash
# Force a hard EU-only constraint on the LLM job AND exhaust on-prem GPU.
# Real-world: the customer's training data is in EU only, and the EU GPUs
# are all in use.
jq '.clusters["on-prem-eu"].capacity["gpu.a100"]=0 | .workloads[2].replicas=1 | .workloads[2].zone_pref="eu"' \
   data/cluster-pricing.json > /tmp/cp.json && mv /tmp/cp.json data/cluster-pricing.json
python3 scripts/cost-sim.py
```

**Expected output:**
```
...
llm-train        UNSCHEDULABLE         -  capacity exhausted
```

This is the answer that the dashboard surfaces. The optimizer doesn't lie:
there's no valid placement, and **the right next step is to talk to a human
about the constraint**, not to silently fall back to AWS. A production
optimizer would page the platform team with "EU data residency conflict".

### 4. Add a 3rd cluster and watch the optimizer rebalance

```bash
# Realistic: you've added a small gcp-eu-west region with mid-tier pricing
# and limited GPU capacity. Stick it into the config:
jq '.clusters += {
  "gcp-eu-west": {
    "name": "gcp (europe-west2)",
    "region": "europe-west2",
    "label": "in-EU, mid pricing, 1 GPU",
    "node_types": {
      "general.large":  {"cpu": 8,  "mem_gb": 32, "gpu": 0, "price_hr": 0.25},
      "general.xlarge": {"cpu": 16, "mem_gb": 64, "gpu": 0, "price_hr": 0.50},
      "gpu.a100":       {"cpu": 32, "mem_gb": 256,"gpu": 8, "price_hr": 4.50}
    },
    "capacity": { "general.large": 10, "general.xlarge": 4, "gpu.a100": 1 }
  }
} | .workloads[2].replicas=1 | .workloads[2].zone_pref="eu"' \
   data/cluster-pricing.json > /tmp/cp.json && mv /tmp/cp.json data/cluster-pricing.json
python3 scripts/cost-sim.py
```

You'll see the optimizer choose between on-prem, gcp, and aws based on
the cheapest node that fits — and data residency is preserved because
`zone_pref: "eu"` now matches three clusters.

### 5. The real production rule (which this sim *doesn't* do)

The cost-sim is a single point-in-time decision. Production systems
add these on top:

- **Spot pricing** — AWS spot is 60-90% off, but reclaimable
- **Sustained-use discounts** — AWS/GCP commit discounts after a month
- **Cross-region latency** — `eu ↔ us-east-1` adds 80ms, breaks SLOs
- **Egress costs** — moving data OUT of AWS costs $0.02-0.09/GB
- **Carbon cost** — on-prem in EU has different carbon intensity than AWS us-east-1

The skill you need is to *recognize* when the optimizer is wrong because
one of these is missing. The point of this lab is the muscle, not the model.

## Restore

```bash
# Restore the default config so other labs see a clean state.
git checkout data/cluster-pricing.json   # or just rewrite from lab doc
python3 scripts/cost-sim.py               # verify it matches step 1
```

## Postmortem prompts

Answer these in `postmortems/04-cost-placement.md`:

1. **What did the system actually do?**
   - When you removed on-prem GPU capacity, did the LLM job **all** move
     to AWS, or did it stay partly on-prem? What does the placement
     string tell you?
2. **What would the on-call impact have been?**
   - A real platform team sees the AWS bill spike. What's the FIRST
     command they should run to confirm "yes, it's the optimizer choosing
     AWS, and here's why"?
3. **What's the production pattern?**
   - Production cost optimizers (Karpenter, Cast.ai, Spot.io) are NOT
     deterministic — they use scoring, not just cheapest. Why does a
     "deterministically cheapest" strategy fail in practice? Think
     about: churn, predictability, ML training jobs that prefer
     stability.
4. **How would an AI agent fail here?**
   - An AI looking at "UNSCHEDULABLE" might recommend "scale up on-prem
     capacity" without asking the human. What's the constraint it
     doesn't know — and how would you teach it to ask?

## Stretch (optional)

Read `scripts/cost-sim.py` end-to-end. There are at least two bugs or
sharp edges you can find:
- The capacity counter is per-node-type, not per-CPU — what does that
  mean for a workload that doesn't fill an entire node?
- The `zone_pref` is checked against `"eu"` and `"any"` only — what
  happens if a workload's `zone_pref` is `"us"`?
