# Kubernetes in Production — Architecture & Design Deep-Dive

> A production engineer's reference for understanding Kubernetes as a *system*, not a tutorial for the basic objects. Optimised for people who already know what a Pod is and want to understand how the pieces fit together under load, on call, and during incidents.

**Audience:** platform engineers, SREs, and backend engineers running Kubernetes in production (or about to).

**Approach:** Each part builds mental models, then maps them to the concrete primitives, then calls out the production failure modes and decision trade-offs.

---

## Table of Contents

| Part | Topic | Why it matters in prod |
|------|-------|------------------------|
| I | [Foundations & mental models](./01-foundations.md) | Decide what Kubernetes is — and isn't — before buying in |
| II | [Control plane internals](./02-control-plane.md) | Know what runs your cluster, why `kube-apiserver` is the chokepoint, and how etcd holds the line |
| III | [Networking](./03-networking.md) | The most misunderstood surface; where most outages begin |
| IV | [Workloads & scaling](./04-workloads-scaling.md) | Deployments, StatefulSets, autoscaling, and the contracts you break when you skip them |
| V | [Storage](./05-storage.md) | Volumes, PVCs, StorageClasses, CSI, and the durability story that bites you during a node loss |
| VI | [Security](./06-security.md) | RBAC, ServiceAccounts, Secrets, supply chain, runtime; the bits auditors care about |
| VII | [GitOps & continuous delivery](./07-gitops.md) | Why declarative + Git-sourced wins, and how Argo/Flux actually work |
| VIII | [Observability](./08-observability.md) | Metrics, logs, traces, events — and the four signals you must wire first |
| IX | [Reliability & day-2 operations](./09-reliability-day2.md) | Upgrades, backups, DR, chaos, the on-call reality |
| X | [Multi-cluster & federation](./10-multi-cluster.md) | When one cluster is enough, and the patterns when it isn't |
| XI | [Production decision framework](./11-decisions.md) | Maturity model, RFC checklists, and the questions to ask before each choice |

---

## How to read this

1. **Read I–III linearly** if you're new to internals. They form the mental model everything else depends on.
2. **Parts IV–X are reference.** Jump to what you're building or breaking.
3. **Part XI is the field guide** — pull it out during RFCs or design reviews.

Every section ends with:
- **Production failure modes** — real ways this goes wrong.
- **Decision table** — when to choose X over Y.
- **Further reading** — primary sources only (Kubernetes docs, SIG readmes, post-mortems).

---

## Conventions

- Commands assume `kubectl` ≥ v1.28 and a cluster with RBAC enabled.
- "Production" means: paying customers on it, ≥ 99.9% SLO, on-call rotation attached.
- "Should" is "do this unless you can articulate why not." "Must" is non-negotiable.

---

## Disclaimer

This is a personal learning reference assembled from primary documentation, SIG meeting notes, and post-mortems. It is opinionated in places — call it out where you disagree.