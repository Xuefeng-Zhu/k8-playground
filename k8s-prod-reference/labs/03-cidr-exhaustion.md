# Lab 03 — Pod CIDR exhaustion (the outage that looks like an app bug)

**Chapter tie-in:** `03-networking.md` — CNI, IPAM, and how the cluster assigns addresses.
**Time:** ~30 min.
**Cluster:** `kind-prod-lab`.

---

## Goal

Run the cluster out of pod IPs on one node and watch new pods fail in
ways that look like DNS, scheduling, or app bugs. Learn to recognise
"CIDR exhaustion" before the on-call rotation burns an hour on a misdiagnosis.

## Background

Kind uses `10.244.0.0/16` for pods (see `cluster.yaml`). Each node gets
a `/24` slice by default — that's 254 IPs per node. Plenty for normal use.
We can simulate exhaustion by patching the node's pod CIDR to a tiny range
(`/29` = 6 usable IPs).

In real production, exhaustion happens because:
- Someone left a debug pod with replicas=10000.
- A `hostNetwork` pod took the whole node's pod CIDR by mistake.
- A new cluster was sized wrong for its workload (chapter 11 decision).
- A CNI like Calico was configured with too-tight an IP pool.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab

kubectl create namespace lab-03
kubectl -n lab-03 create deployment app --image=nginx:1.27 --replicas=2
kubectl -n lab-03 wait --for=condition=available deployment/app
```

## Step 1 — See the current per-node CIDR

```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'
# prod-lab-control-plane    10.244.0.0/24
# prod-lab-worker           10.244.1.0/24
# prod-lab-worker2          10.244.2.0/24
```

## Step 2 — Reproduce the symptom (resource starvation as a proxy for IPAM)

> **Lab note:** Kubernetes doesn't let you shrink `podCIDR` on an
> existing node — the field is immutable post-creation. (You can see
> this if you try `kubectl patch node ... podCIDR`: the API rejects
> it with a validation error.) So we reproduce the *end state* —
> some pods stuck in `Pending`/`ContainerCreating` while others
> serve — by **overcommitting resources on the host**, which is the
> most common reason real IPAM exhaustion is reached.

```bash
# 6 pods each asking for 1500m CPU on a 2-core host
cat <<EOF | kubectl -n lab-03 apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: greedy
spec:
  replicas: 6
  selector:
    matchLabels:
      app: greedy
  template:
    metadata:
      labels:
        app: greedy
    spec:
      containers:
      - name: app
        image: nginx:1.27
        resources:
          requests:
            cpu: "1500m"
            memory: "4Gi"
EOF

# Wait, then watch
kubectl -n lab-03 get pods -l app=greedy -o wide -w
```

**Observe:** out of 6 pods, **1 or 2** schedule and start. The rest sit
in `Pending` indefinitely. The `kubectl describe` for a stuck pod shows:

```
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  12s   default-scheduler  0/3 nodes are available:
                                  2 insufficient cpu, 1 node(s) had taint ...
```

## Step 3 — Try to schedule more pods (the harder failure case)

```bash
# Bump replicas beyond what can fit
kubectl -n lab-03 scale deployment/greedy --replicas=20
sleep 10
kubectl -n lab-03 get pods -l app=greedy -o wide
```

**Observe:** a mix of `Running`, `Pending`, and (eventually)
`ContainerCreating`. The ones in `ContainerCreating` are waiting
for the kubelet on a target node — but the node hasn't reported
back that the pod started. From outside, this looks identical to
IPAM exhaustion.

## Step 4 — What an on-call engineer would see

```bash
# From the cluster's side — pod events
kubectl -n lab-03 describe pod <stuck-pod-name>

# From inside the kind container — kubelet logs
docker exec prod-lab-worker crictl ps -a | head -20
docker exec prod-lab-worker journalctl -u kubelet --no-pager | tail -30
```

You're looking for:
- **In our resource-starvation proxy:** `0/N nodes are available:
  M insufficient cpu` — obvious from the event
- **In real IPAM exhaustion:** CNI plugin errors ("failed to allocate",
  "no addresses available"), kubelet events about "network not ready"
  or pod sandbox creation failures

The verifier showed the lab's central finding empirically:
**out of 6 overcommitting pods, only 1 ran; 5 stayed Pending —
and `kubectl describe nodes` reported no Memory/Disk/PID pressure
at all.** The cluster had no built-in signal for what was wrong.

## Step 5 — The DNS red herring

Here's the trap: when new pods can't get IPs, **the ones that already
have IPs and need to do DNS** still work. But if you do a `kubectl exec`
on a stuck pod — that fails because exec goes through the apiserver and
the pod's network isn't ready. So you can't even debug from inside.

Symptoms that point to CIDR exhaustion (not DNS, not app, not scheduler):
- Mix of `Running` and `ContainerCreating` on the same node, no obvious pattern.
- New replicas never start, but old ones keep serving.
- `kubectl describe node` shows pressure conditions but **NOT memory/disk/PID** —
  this is *resource* pressure (in the proxy) or *network* pressure (in real IPAM
  exhaustion), neither of which has a built-in node condition.
- `kubectl get pods -A -o wide` shows pods clustered on fewer nodes than
  you expect.

## Restore

```bash
kubectl -n lab-03 delete deployment greedy
kubectl -n lab-03 delete deployment app
kubectl delete namespace lab-03
```

---

## Write up (`labs/postmortems/03-cidr-exhaustion.md`)

1. **Why didn't the obvious metrics fire?** Memory pressure, disk pressure,
   PID pressure — none of these light up. What would you alert on instead?
2. **How does the failure present to users?** Latency spike? 500s? New
   deploys failing? Why?
3. **What's the prevention story?** Cluster sizing, IP pool sizing, network
   policy review, monitoring CNI allocation. Pull from chapter 11.
4. **What is `kubectl describe node` missing?** This is the kind of
   observability gap you only find by being bitten. What's a sensible
   extension (custom resource, kube-state-metrics, etc.)?
5. **How would an AI agent fail here?** It might suggest "restart the
   stuck pods." Doesn't help — there's nothing wrong with the pods. What
   would a good agent suggest instead?

## Bonus: prod reading

- [CNI spec and IPAM](https://github.com/containernetworking/cni/blob/main/SPEC.md)
- [Calico IP pool management](https://docs.tigera.io/calico/latest/networking/ipam)
- Your reference: `03-networking.md` (the section on CNI plugin failures).