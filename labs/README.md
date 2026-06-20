# Labs — break things on purpose

Each lab ties a chapter of the reference to a hands-on failure-mode exercise.
Read the chapter first, then run the lab, then write the postmortem.

## Cluster

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab
```

3-node cluster: 1 control-plane + 2 workers, IPv4 only, ports 18080/18443
mapped for ingress work.

## Create / destroy

```bash
# Bring it up
kind create cluster --config cluster.yaml

# Tear it down (start fresh if you break it badly)
kind delete cluster --name prod-lab
```

## Labs

| #  | Title                                                     | Chapter | Failure mode exercised                                          | Status              |
|----|-----------------------------------------------------------|---------|-----------------------------------------------------------------|---------------------|
| 01 | [Drain a node mid-rolling-update](./01-drain-during-rollout.md) | 09      | Eviction budget vs grace period                                 | ✅ verified          |
| 02 | [What breaks when `kube-apiserver` stalls](./02-apiserver-stall.md) | 02      | Data plane vs control plane                                     | ✅ verified (with caveat) |
| 03 | [Pod CIDR exhaustion](./03-cidr-exhaustion.md)            | 03      | IPAM exhaustion masquerading as DNS / scheduling bugs           | ✅ verified          |
| 04 | [StatefulSet through a zone loss](./04-statefulset-zone-loss.md) | 05      | Wrong `volumeBindingMode` bricks a StatefulSet                  | ✅ verified          |
| 05 | [Malicious image vs. admission webhook](./05-admission-vs-agent.md) | 06      | Webhook policy binds the agent's surface area                   | ✅ verified          |

## Why this format

Reading the chapter gives you the lens. Breaking the cluster gives you the
intuition. Writing the postmortem locks it in and forces you to state what
you saw in your own words — the part an AI can't do for you.

## What to do after a lab

Drop a 1-page postmortem in `postmortems/NN-short-name.md`. There's no
required template, but answer: *what did the system actually do? what did
I expect? what's the production pattern? how would an AI agent fail here?*

That last question is the one that compounds. Every lab you do trains you
to spot the constraint the AI doesn't know to check.

## Verification status — what "verified" actually means here

Every lab above was executed end-to-end against your live `kind-prod-lab`
cluster with a Python verifier script. The verifiers:

1. Set up the lab conditions (deploy workloads, configure policies, etc.)
2. Provoke the failure mode described in the lab
3. Observe and capture what actually happened
4. Clean up (delete namespaces, configs, images)

If a lab says "verified", you can run it as written and it will work. The
verifier scripts are in `/tmp/verify-lab-NN.py` if you want to re-run them.