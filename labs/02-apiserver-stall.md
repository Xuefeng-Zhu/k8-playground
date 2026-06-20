# Lab 02 — What breaks when `kube-apiserver` stalls

**Chapter tie-in:** `02-control-plane.md` — why the apiserver is the chokepoint, and what reads/writes look like from the kubelet's side when it stops responding.
**Time:** ~25 min.
**Cluster:** `kind-prod-lab`.

---

## Goal

Kill `kube-apiserver`, watch what happens to already-running workloads
vs new scheduling, then bring it back. Build intuition for the difference
between **data plane** (your pods are fine) and **control plane** (nothing
can change) — and how that surfaces as alert symptoms.

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl config use-context kind-prod-lab

# Deploy a workload so we have something observably running
kubectl create namespace lab-02
kubectl -n lab-02 create deployment api --image=nginx:1.27 --replicas=4
kubectl -n lab-02 wait --for=condition=available deployment/api --timeout=60s

# Note: pods work, the apiserver is reachable
curl -sk https://127.0.0.1:<nodeport>/healthz   # should print "ok"
```

To find the apiserver host port for your cluster:

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
```

## Step 1 — Find the apiserver pod and the container it lives in

```bash
APISERVER_POD=$(kubectl -n kube-system get pod -l component=kube-apiserver \
  -o jsonpath='{.items[0].metadata.name}')
echo "apiserver pod: $APISERVER_POD"
# The pod runs inside the kind control-plane container, named prod-lab-control-plane
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep prod-lab-control-plane
```

The kind design is: control-plane container runs both Docker and the K8s
control plane components as processes. So `kube-apiserver` is a *container
inside that container*, managed as a static pod. To break it from the
outside, we stop the kind container itself.

## Step 2 — Pause the apiserver (don't delete it — we want to recover)

```bash
# SIGSTOP freezes the entire control-plane container, apiserver included.
# SIGCONT later resumes it. This is exactly what an apiserver stall looks
# like to the rest of the cluster (no response, no errors yet).
docker pause prod-lab-control-plane
```

## Step 3 — Observe from outside

In one terminal:

```bash
# This will hang because the apiserver isn't answering.
time kubectl get nodes
```

In another:

```bash
# curl the healthz — also hangs. The connection is established, then nothing.
time curl -sk --max-time 10 https://127.0.0.1:<nodeport>/healthz; echo
```

In a third:

```bash
# Existing pod-to-pod TCP connections are still alive. New connections
# (e.g. via wget from another pod) will fail in kind because the CNI
# control plane shares the paused container. Try from a debug pod:
kubectl -n kube-system run netcheck --image=alpine --restart=Never --command -- sleep 600
kubectl -n kube-system wait --for=condition=ready pod/netcheck
TARGET=$(kubectl -n lab-02 get pod -l app=api -o jsonpath='{.items[0].status.podIP}')
kubectl -n kube-system exec netcheck -- wget -q -O - --timeout=5 http://$TARGET/
# In kind: this will time out. In a real cluster with a host-agent CNI,
# it returns nginx's default page.
```

**Observe the cascade:**

1. `kubectl get` hangs (no apiserver response).
2. `curl /healthz` hangs the same way — same underlying TCP connection to apiserver.
3. Any pods already running keep their *existing* TCP connections alive.
   New connections (e.g. another pod trying to dial them, or a pod trying
   to reach a service) will fail in kind because the CNI control plane is
   also paused. In a production CNI (Cilium/Calico with host agents) the
   data plane survives — see Step 6.
4. **The kubelet will start logging "failed to sync" entries every ~10s.**
   It will NOT restart your pods — they're already running, no decision needs
   the apiserver.

This is the difference between apiserver down and a node down. With apiserver
down, **existing workloads keep running**, but the cluster can't reconcile any
drift, and (in kind, unlike prod) new network flows between pods also break.

## Step 4 — Try a write (you guessed it — it fails)

```bash
kubectl -n lab-02 scale deployment/api --replicas=10
# Hangs forever. Ctrl-C after 15s.
```

If you wait, you'll see `--request-timeout` (default 60s) kick in eventually.

## Step 5 — Resume

```bash
docker unpause prod-lab-control-plane
# Give it a few seconds, then:
kubectl get nodes        # back fast
kubectl -n lab-02 get pods --field-selector=status.phase=Running
```

**Observe the recovery cascade:**

1. Apiserver answers immediately (it's just paused, not restarted).
2. Pending writes that timed out need to be retried — nothing was applied.
3. Watch kubelet log volume: during the pause it kept trying to sync, so
   there's a backlog of identical events. This is what floods your alerting
   during a real apiserver stall.

## Step 6 — Data plane survival (caveat for kind specifically)

The narrative "data plane survives apiserver stall" is mostly true in
production with a properly architected CNI (Cilium, Calico with host
agents) — but **not in kind by default**, because kind's `kindnet`
CNI runs *inside* the control-plane container. Pause the container,
the CNI control plane also pauses, and existing flows get ARP/NAT
entries that age out. New connections from running pods time out.

Verified empirically with the lab verifier:
```
PASS netcheck can reach 10.244.2.22 (rc=0, 615 bytes)    # before pause
FAIL netcheck->10.244.2.22 in 10.2s (rc=1, 0B)          # during pause
```

In EKS / GKE / AKS / any cloud cluster, this would be a PASS instead
of a FAIL because the CNI agents run as DaemonSets on each worker,
independent of the apiserver. **This is a real reason to test
apiserver failure modes in an environment that matches production.**

## Step 7 — Cleaner version with `crictl`

If you want to pause only the apiserver (not the whole control-plane
container — which gives you a fairer test of "apiserver down but
data plane fine"), drop into the kind control plane and use crictl:

```bash
docker exec -it prod-lab-control-plane bash
# Inside the container:
crictl ps | grep kube-apiserver
# Get the container ID, then:
crictl stop <apiserver-container-id>
# Wait, observe, then:
crictl rm <apiserver-container-id>
# The static pod will be recreated by the kubelet on the host.
```

This is closer to a real production failure — kubelet restarts the
static pod, apiserver comes back with **no etcd corruption** because
we didn't touch the disk. Real production outages that lose etcd
are a different beast (see chapter 09 on etcd backups).

The data-plane should survive THIS variant of the failure, because
only `kube-apiserver` is down — etcd, scheduler, controller-manager,
and kindnet are all still running.

## Restore

```bash
kubectl delete namespace lab-02
```

---

## Write up (`labs/postmortems/02-apiserver-stall.md`)

1. **What hangs, what doesn't?** List the read paths and write paths
   separately. Which depended on the apiserver, which didn't?
2. **What alert would have fired first?** `apiserver latency` (lagging),
   `pod restarts` (false positive here), or something else? What's the
   SLO you'd set on apiserver availability?
3. **Why didn't pods restart?** Connect this to the kubelet's "if the
   apiserver is down, don't change anything" behaviour — it's the same
   reason a node network partition doesn't kill pods.
4. **Where does etcd come in?** A real apiserver stall from disk pressure
   looks similar from outside. How would you tell them apart?
5. **How would an AI agent fail here?** It might try to "fix" the apiserver
   by killing the pod — fine. But if it then also retries pending writes
   without checking whether they were already applied against etcd, it
   will double-apply. How would you detect that?

## Bonus: prod reading

- [Kubernetes API server internals](https://kubernetes.io/docs/reference/access-authn-authz/controlling-access/)
- [HA apiserver patterns](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/ha-topology/)
- Your reference: `02-control-plane.md`, section on etcd fsync and the
  request → etcd → watch path.