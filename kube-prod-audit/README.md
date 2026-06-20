# kube-prod-audit

A production-readiness auditor for Kubernetes clusters. Runs ~22 checks across workloads, security, reliability, observability, resources, storage, and network. Outputs a terminal summary and a self-contained HTML report.

Ships as part of the **Kubernetes in Production** reference. The check rules are extracted from Parts I–XI of the reference; findings link back to the relevant section.

## What it checks

| Category | Checks |
|----------|--------|
| **Workloads** | PDBs, liveness/readiness probes, min replicas, rolling update strategy |
| **Security** | PSS restricted, privileged containers, runAsNonRoot, seccomp, default SA, automount, hardcoded secrets |
| **Reliability** | HA replica count, maxUnavailable, OOMKilled events, FailedScheduling |
| **Observability** | HPAs present, log shipper, metrics pipeline |
| **Resources** | CPU/memory limits, CPU/memory requests |
| **Storage** | All PVCs Bound |
| **Network** | NetworkPolicy in every prod namespace |

## Install

```bash
git clone <repo> kube-prod-audit
cd kube-prod-audit
pip install -e .   # no deps; pure stdlib + PyYAML is auto-discovered
```

## Usage

```bash
# auto-detect: live if `kubectl` is available, else demo
python -m kube_prod_audit

# force live (queries cluster via kubectl)
python -m kube_prod_audit --mode live

# demo data (so you can see the report without a cluster)
python -m kube_prod_audit --mode demo

# specify kubeconfig + context
python -m kube_prod_audit --kubeconfig ~/.kube/config --context prod-us-east-1

# output to a custom path
python -m kube_prod_audit --output reports/2026-06-18.html

# CI mode: exit non-zero on failures
python -m kube_prod_audit --exit-on-fail
```

## Exit codes

- `0` — all checks passed (or only warns)
- `2` — one or more FAIL checks (with `--exit-on-fail`)
- `3` — audit itself failed to run

## Architecture

```
kube_prod_audit/
├── __main__.py        # python -m kube_prod_audit entry point
├── cli.py             # argparse CLI
├── collector.py       # live (kubectl) or demo (mock) data source
├── runner.py          # result types, severity, score
├── checks/            # one fn per check
├── report.py          # terminal + HTML renderers
└── demo_data.py       # realistic mock cluster
```

Each check is a function: `(ClusterData) -> CheckResult`. Adding a new check = one function, one entry in `ALL_CHECKS`.

## Demo

```bash
$ python -m kube_prod_audit --mode demo --no-color
```

Expected output (truncated):

```
Score  68/100  [██████████████░░░░░░░░░░░░░░░░]
  PASS  14   WARN   5   FAIL   3

By category
  ! Security        pass  2   warn  2   fail  3
  ✓ Workloads       pass  4   warn  1   fail  0
  ! Reliability     pass  2   warn  2   fail  0
  ! Resources       pass  1   warn  1   fail  0
  ✓ Observability  pass  3   warn  0   fail  0
  ✓ Network         pass  1   warn  0   fail  0
  ✓ Storage         pass  1   warn  0   fail  0

Findings
  ✗ FAIL SC-002  No privileged containers
      1 privileged container(s)
        · prod/legacy-billing/billing
      fix: Drop privileged: true. Use specific capabilities (NET_ADMIN, SYS_TIME) if needed.
  ✗ FAIL SC-005  Workloads use a specific ServiceAccount, not 'default'
      1 prod workload(s) using the default SA
        · prod/worker
      fix: Create a ServiceAccount per app and reference it via spec.serviceAccountName.
  …
```

HTML report written to `audit-report.html` — open in any browser.
