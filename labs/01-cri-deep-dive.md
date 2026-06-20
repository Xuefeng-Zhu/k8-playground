# Lab 01 — CRI deep-dive: from kubelet→CRI→runc

> **Topic:** Cloud-Native Depth (Kubernetes internals, container runtimes/CRI)
> **Goal:** Trace a single `kubectl run` pod from API request all the way down to
> the OCI runtime that execve()s the container process, and prove each layer
> exists on this cluster.

---

## Why this matters

A surprising amount of "Kubernetes" work actually happens **below** kubelet — in
the Container Runtime Interface (CRI) shim (here: `containerd` + `cri-containerd`)
and the OCI runtime (here: `runc`). When pods fail to start, the error almost
always traces back to one of these layers. Knowing the path means you can read
the error message and know *which* layer is broken, instead of grepping randomly.

```
kubectl run ─► kube-apiserver
            ─► etcd (write Pod spec)
            ─► scheduler (binds Pod → Node)
            ─► kubelet (watches bound Pods)
            ─► CRI gRPC to containerd
            ─► containerd pulls image, sets up cgroups+namespaces
            ─► runc execve()s the container process
```

## Pre-flight

```bash
export PATH="$HOME/.local/bin:$PATH"
kubectl --context kind-platform-lab get nodes
docker exec platform-lab-control-plane crictl --version
docker exec platform-lab-control-plane ctr --version
docker exec platform-lab-control-plane runc --version
```

You should see:
- `crictl v1.x.x`           — the CRI CLI (talks to the CRI shim over gRPC)
- `ctr containerd.io ...`   — the containerd CLI (debug-level)
- `runc version 1.x.x`      — the OCI runtime that actually creates processes

## Steps

### 1. Launch a pod and capture its container ID across layers

```bash
kubectl --context kind-platform-lab run nginx --image=nginx:1.27 --restart=Never
# Wait for the pod to be Running, not just "created". On a fresh cluster the
# image pull can take 30-60s; on a warm cluster, 2-5s. The `kubectl wait`
# form below handles both.
kubectl --context kind-platform-lab wait --for=condition=Ready pod/nginx --timeout=60s
POD_UID=$(kubectl --context kind-platform-lab get pod nginx -o jsonpath='{.metadata.uid}')
echo "Pod UID: $POD_UID"
```

### 2. Look at it through each lens

**Lens A — kubectl (control-plane view):**
```bash
kubectl --context kind-platform-lab get pod nginx -o yaml | head -40
```

**Lens B — crictl (CRI shim view, same data kubelet sees):**
```bash
# Get the container ID from crictl ps (NOT the pod UID — `crictl inspect` takes a container ID).
CID=$(docker exec platform-lab-control-plane crictl ps | awk '/nginx/ {print $1}')
echo "container id: $CID"
docker exec platform-lab-control-plane crictl inspect "$CID" | head -40
docker exec platform-lab-control-plane crictl inspectp "$POD_UID" | head -20  # pod-level view
```

**Lens C — ctr (containerd-native view, deeper than CRI):**
```bash
docker exec platform-lab-control-plane ctr -n k8s.io containers ls | grep $POD_UID
docker exec platform-lab-control-plane ctr -n k8s.io tasks ls | grep $POD_UID
```

**Lens D — runc (the OCI process itself):**
```bash
# `crictl inspect` of a CONTAINER (not pod) gives us the host PID of the OCI process.
PID=$(docker exec platform-lab-control-plane crictl inspect "$CID" \
      | jq -r '.info.pid')
echo "nginx PID inside the node: $PID"
docker exec platform-lab-control-plane cat /proc/$PID/status | head -5
docker exec platform-lab-control-plane ls -la /proc/$PID/ns/   # each is a Linux namespace
```

### 3. Trigger a runtime-layer error and read it correctly

```bash
kubectl --context kind-platform-lab run badimage --image=thisdoesnotexist:zzz --restart=Never
sleep 8
kubectl --context kind-platform-lab describe pod badimage | tail -10
```

You'll see `ImagePullBackOff`. The events tell you **which CRI layer** failed:
- `Failed to pull image ... no such host`  →  registry DNS / network (CNI/Cilium)
- `pull access denied`                      →  CRI shim (containerd) auth config
- `failed to extract layer`                →  OCI image format / storage driver

## What to observe

| Observation | Confirms |
|------------|----------|
| `crictl ps` shows the nginx container | CRI shim is healthy and accepting kubelet calls |
| `ctr ... tasks ls` shows a task ID | containerd has *spawned* the process (not just stored the image) |
| `/proc/$PID/ns/` shows `pid`, `net`, `mnt`, `uts` symlinks | runc actually created the namespaces — this is what "isolation" means |
| `kubectl describe` events name "kubelet" or "FailedCreate" | the failure was caught *above* CRI; the runtime never tried |

## Restore

```bash
kubectl --context kind-platform-lab delete pod nginx badimage --ignore-not-found
```

## Postmortem prompts

Answer these in `postmortems/01-cri-deep-dive.md` after running the lab:

1. **What did the system actually do?** (Not what you expected — what you saw.)
   - List every layer (kubectl → crictl → ctr → /proc) and what unique info
     each one gave you that the layer above did NOT.
2. **What would the on-call impact have been?**
   - If a real outage pinned every pod at "ContainerCreating", which layer's
     logs would you grep first, and how do you know?
3. **What's the production pattern?**
   - Why do production clusters run a separate CRI shim (containerd/CRI-O)
     rather than letting kubelet call runc directly? Hint: think about
     image pull throughput, image GC, and runtime sandboxing.
4. **How would an AI agent fail here?**
   - An AI sees "ContainerCreating" and might recommend `kubectl delete pod`.
     What's the constraint it doesn't know to check first?

## Stretch (optional)

Replace containerd with CRI-O on a *throwaway* cluster and re-run this lab.
Compare the output of `crictl ps` and the kubelet logs — they should be
identical, because CRI is exactly that: an interface.
