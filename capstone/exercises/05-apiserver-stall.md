# Exercise 05 — Apiserver stall on a live system

**Linked lab:** `labs/02-apiserver-stall.md`
**Linked chapter:** `02-control-plane.md` (apiserver, etcd, kubelet)

**Time:** ~10 min
**What you'll break:** the apiserver, and watch what the running
capstone does. The lesson is what already-running workloads survive
versus what doesn't.

---

## Before

The capstone is fully running:

```bash
kubectl get pods -A -l 'app in (frontend, backend, postgres)'
# All Running

# Verify the frontend is reachable (via port-forward if needed):
kubectl -n capstone-frontend port-forward svc/frontend 18080:80 &
sleep 2
curl -sf http://localhost:18080/ -o /dev/null -w "HTTP %{http_code}\n"
# 200 (or 301 — nginx default page)
kill %1 2>/dev/null
```

## Break — pause only the apiserver, not the whole control plane

The lab-05-style approach: use `crictl stop` on the apiserver
container inside the kind control-plane. The kubelet restarts it
within ~5 seconds, so you have a brief window to observe.

```bash
APISERVER_CID=$(docker exec prod-lab-control-plane crictl ps -a 2>&1 \
  | grep kube-apiserver | head -1 | awk '{print $1}')
echo "apiserver container: $APISERVER_CID"

# Quick test: pause only the apiserver
docker exec prod-lab-control-plane crictl stop $APISERVER_CID
sleep 1
```

In a separate terminal (or after a short wait), check what happens:

```bash
# kubectl calls block (the apiserver is restarting)
time kubectl get pods -A
# Expected: hangs for ~3-5s, then succeeds (kubelet restarts apiserver)

# Existing pods keep running (they were already scheduled)
kubectl -n capstone-frontend get pods   # may hang
kubectl -n capstone-backend get pods    # may hang
kubectl -n capstone-postgres get pods   # may hang

# After ~5s, the apiserver is back:
sleep 6
kubectl get nodes  # should respond normally
```

## Observe — the cascade

| What you tried | What happened |
|---|---|
| `kubectl get pods` during pause | Hangs then succeeds (kubelet restarted apiserver) |
| `kubectl get pods -w` during pause | Connection drops, has to reconnect |
| New pod creation during pause | Hangs then succeeds (after apiserver restart) |
| Existing pods serving traffic | Continue serving (data plane intact in kind via crictl) |
| Network connectivity between pods | Preserved if you used crictl (data plane intact) |
| Network connectivity between pods | BROKEN if you used `docker pause` (lab 02 caveat) |

## The two ways to break an apiserver

You can demonstrate the difference:

```bash
# Variant 1: pause only the apiserver (crictl stop) — data plane survives
docker exec prod-lab-control-plane crictl stop $APISERVER_CID
sleep 1
kubectl -n capstone-frontend exec deploy/frontend -- /bin/sh -c \
  "echo > /dev/tcp/backend.capstone-backend/8080 && echo 'TCP_OK'"
# Expected: TCP_OK (in kind, IF you used crictl; crictl kills only
# the apiserver, not the kindnet CNI)

# Wait for apiserver to come back
sleep 8
docker exec prod-lab-control-plane crictl ps -a | grep kube-apiserver
```

```bash
# Variant 2: pause the entire control plane (docker pause) — data plane breaks
docker pause prod-lab-control-plane
sleep 3
# Data plane is broken because kindnet CNI shares the paused container
kubectl -n capstone-frontend exec deploy/frontend -- /bin/sh -c \
  "echo > /dev/tcp/backend.capstone-backend/8080 && echo 'TCP_OK'"
# Expected: hangs (timeout) — CNI control plane is also paused

# Resume
docker unpause prod-lab-control-plane
sleep 5
kubectl get nodes  # recovered
```

**The lesson:** in kind (which uses kindnet CNI running inside the
control-plane container), `docker pause` takes down the data plane
too. In production with a host-agent CNI (Cilium, Calico), it
wouldn't. This is why production failure-mode testing should happen
in production-like environments, not just kind.

## Recover

The capstone should self-recover within ~10 seconds of any apiserver
interruption. Verify:

```bash
kubectl get pods -A -l 'app in (frontend, backend, postgres)'
# All Running

# Verify the policy still rejects bad pods
kubectl -n capstone-frontend run bad --image=nginx:1.27 --restart=Never
# Expected: denied by admission webhook (the policy survives apiserver
# restarts because it's stored in etcd, which is also restarted cleanly)
```

## Postmortem prompt (`postmortems/05-capstone-apiserver.md`)

1. **How long was each tier actually unavailable?** Quantify:
   frontend, backend, postgres. They might be different durations.
2. **What was the user impact?** Could a user have completed a
   request? At what point did the system recover?
3. **What data was at risk?** Specifically: was postgres still
   accepting writes during the stall? (Probably yes, since the
   data plane is intact with crictl stop.) What if a write
   succeeded but the apiserver never knew?
4. **What's the production detection story?** SLO on apiserver
   availability vs SLO on the data plane. They're different
   metrics with different failure modes.
5. **What does the chapter-2 lesson teach here?** The apiserver
   restart is automatic because the kubelet treats it as a static
   pod. What would happen if etcd (not just apiserver) was killed?
   (Hint: chapter 9 on backups.)