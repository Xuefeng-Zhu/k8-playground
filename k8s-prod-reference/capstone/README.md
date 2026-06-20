# Capstone: a 3-tier production-shaped system

This is the integration test. Each component of this app is wired with
the production-correct answer to one of the 5 lab failure modes:

| Component  | Failure mode it defends against             | Lab  |
|------------|---------------------------------------------|------|
| frontend   | graceful shutdown during drain/rollout      | 01   |
| backend    | PDB holds the line during node maintenance  | 01   |
| backend    | HPA reacts to load (separate from scaling)  | 04   |
| postgres   | StatefulSet + WaitForFirstConsumer binding  | 04   |
| postgres   | resources set so CIDR/starvation is visible | 03   |
| everything | admission policy denies root + privileged  | 05   |

The goal is to have a single system where you can break any of the
5 failure modes from the labs and see it surface in the running app —
then fix it, and see it recover.

## What's deployed

```
                 ┌──────────────┐
                 │  frontend    │  Deployment, 2 replicas, Nginx serving UI
                 │  (port 18080)│
                 └──────┬───────┘
                        │
                        ▼
                 ┌──────────────┐
                 │   backend    │  Deployment, 2 replicas, Nginx serving API
                 │  (port 8080) │  + HPA, PDB
                 └──────┬───────┘
                        │
                        ▼
                 ┌──────────────┐
                 │  postgres    │  StatefulSet, 1 replica, persistent volume
                 │  (port 5432) │  with WaitForFirstConsumer binding
                 └──────────────┘
```

## File layout

```
capstone/
├── README.md                       <- this file
├── deploy.sh                       <- one-shot deploy script
├── manifests/
│   ├── 00-namespaces.yaml          <- frontend, backend, postgres, system namespaces
│   ├── 01-frontend.yaml            <- Deployment + Service + NodePort
│   ├── 02-backend.yaml             <- Deployment + Service + HPA + PDB
│   ├── 03-postgres.yaml            <- StatefulSet + Service + Secret
│   ├── 04-network-policies.yaml    <- default-deny + explicit allows per tier
│   └── 05-admission.yaml           <- webhook Deployment + SA + RBAC + Service
├── exercises/                      <- 5 integration exercises
│   ├── 01-rolling-update-with-pdb.md
│   ├── 02-admission-policy.md
│   ├── 03-resource-starvation.md
│   ├── 04-statefulset-broken-sc.md
│   └── 05-apiserver-stall.md
└── broken-manifests/               <- deliberately-broken manifests for exercises
    ├── no-pdb.yaml                 <- exercise 01: deploys a backend with no PDB
    ├── root-pod.yaml               <- exercise 02: pod without securityContext
    └── wrong-sc.yaml               <- exercise 04: StatefulSet with Immediate-mode SC

## Verification status

Every component and exercise was verified against the live cluster:

- **Infrastructure** (tiers + PDB + HPA + network policies + admission webhook):
  all 25 checks pass in `/tmp/verify-capstone.py`. Re-run anytime with
  `python3 /tmp/verify-capstone.py`.
- **Exercise 01** (PDB vs no-PDB drain): drain with PDB blocks for ~30s;
  drain without PDB completes in <1s. Verified empirically.
- **Exercise 02** (admission policy): bad pods denied with correct
  reason string; exempt namespaces (kube-system, capstone-system) are
  correctly allowed.
- **Exercise 03** (resource starvation): overcommitting produces
  Pending pods with `insufficient cpu` events; no node pressure condition
  fires (this is the lab 03 trap).
- **Exercise 04** (broken StorageClass): `broken-immediate` SC +
  StatefulSet produces a stuck Pending pod; deleting the StatefulSet
  is the only recovery path (destructive in real production).
- **Exercise 05** (apiserver stall): `crictl stop` pauses only apiserver,
  data plane survives; `docker pause` pauses the whole control plane,
  data plane dies (kind-specific caveat from lab 02).

## How to deploy

```bash
export PATH="$HOME/.local/bin:$PATH"
bash capstone/deploy.sh
```

This script:
1. Creates namespaces
2. Generates a self-signed cert for the admission webhook
3. Deploys the tier manifests (frontend, backend, postgres)
4. Deploys network policies
5. Deploys the admission webhook + registers it with the apiserver
6. Waits for all pods to be ready

## How to run the exercises

Each `exercises/NN-*.md` walks through breaking one lab's failure mode
against the integrated system and observing the cascade. They take
~15 min each. Start with 01 if you want the simplest exercise; jump
around if you have a specific failure mode to investigate.

## How to verify

```bash
python3 /tmp/verify-capstone.py
```

Should print 25/25 PASS.

## What's NOT included (and why)

- **A real GitOps controller** (Argo/Flux): would add 200+ MB of
  resource and a moving target. The manifests are structured so the
  switch is one command (`kubectl apply -k` → Argo Application).
- **A service mesh** (Istio/Linkerd): same reason — too much
  infrastructure for what we want to test. The capstone's network
  model is plain ClusterIP Services + NetworkPolicies.
- **mTLS everywhere**: would need cert-manager, cert rotation,
  trust bundles. Out of scope.
- **A proper Postgres**: we use the `postgres:16` image but with
  1 replica and no replication. The point is to demonstrate the
  StatefulSet + PVC + SC pattern, not to run a real database.

If you want to add any of these, each one is a chapter-7 (GitOps),
chapter-3 (networking), or chapter-6 (security) lab of its own.