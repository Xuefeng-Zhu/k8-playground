# Part III — Networking

Networking is where Kubernetes stops feeling like a single machine and starts feeling like a distributed system. Most production outages have a network component; many begin there.

## 3.1 The four problems Kubernetes networking must solve

Every cluster needs:

1. **Container-to-container** within a Pod — solved by sharing the Pod's network namespace (`pause` container holds it).
2. **Pod-to-Pod** across nodes — solved by the CNI plugin assigning a routable IP to every Pod.
3. **Pod-to-Service** — solved by `kube-proxy` programming iptables/IPVS rules so that a `Service` ClusterIP load-balances to backing Pods.
4. **External-to-Service** — solved by `Ingress`, `Service` of type `LoadBalancer`, or `NodePort`.

The **Cluster Networking Contract** (from the Kubernetes networking model):

- All Pods can communicate with all other Pods without NAT.
- All Nodes can communicate with all Pods without NAT.
- Pods see their own IP as the source when communicating with other Pods (no address hiding).

This contract is what CNI plugins implement. Break it (e.g. with overly restrictive NetworkPolicies) deliberately; don't break it accidentally.

## 3.2 CNI — Container Network Interface

The CNI is a plug-in contract: when kubelet sees a Pod scheduled, it calls the CNI plugin to attach that Pod to the cluster network.

Common CNIs in production:

| CNI | Model | Notes |
|-----|-------|-------|
| **Calico** | BGP or IP-in-IP, policy | Battle-tested, strong NetworkPolicy, scales large |
| **Cilium** | eBPF | Replaces kube-proxy, identity-aware policy, observability built-in |
| **AWS VPC CNI** | Real VPC IPs on Pods | EKS default; uses ENIs, IP-hungry |
| **Azure CNI** | Real VNet IPs on Pods | AKS default; same IP-hunger trade-off |
| **GKE Dataplane V2** | eBPF, Cilium-based | GKE default for newer clusters |
| **Flannel** | Simple VXLAN overlay | Lightweight, dev-friendly, weak policy |
| **Weave** | Overlay | Less popular today |

### Decision points for CNI selection

- **Overlay vs underlay.** Overlay (Calico VXLAN, Flannel, Cilium tunnel mode) hides Pod IPs from the underlying network — easy IP planning, no IP exhaustion in VPC, but extra MTU/encap overhead. Underlay (Calico BGP, AWS VPC CNI) puts Pod IPs on the real network — better performance, observability, but you must plan IP space.
- **Policy enforcement.** Calico and Cilium support full NetworkPolicy and extended policy. Flannel is permissive.
- **eBPF vs iptables.** eBPF (Cilium) scales better for large clusters with many Services.
- **Cloud-native integrations.** AWS VPC CNI gets you AWS PrivateLink, security groups per Pod, ENI-level metrics. On EKS it's worth the IP management burden.

### IP address management

You must allocate a Pod CIDR per node, and it must not overlap with the node network, Service CIDR, or anything routed in your VPC.

**Production rule:** plan for 110–125% of current node count × max Pods per node. Re-IPing a live cluster is painful.

### MTU and overlay caveats

VXLAN adds 50 bytes of overhead. If your underlying MTU is 1500, the overlay MTU must be 1450. Most CNIs auto-detect, but cross-cloud or weird VPCs bite. Symptoms: large packets silently fail, you see partial TCP handshakes, your monitoring works (small packets) but file transfers break.

## 3.3 Services in detail

### Service types

| Type | What it does | Use case |
|------|--------------|----------|
| **ClusterIP** | Virtual IP inside cluster, reachable from any Pod or Node | Internal microservices |
| **NodePort** | Opens a port on every Node | Rare in production; sometimes used for ingress nodes |
| **LoadBalancer** | Provisions a cloud L4 LB | External L4, when you don't need HTTP routing |
| **ExternalName** | DNS CNAME | Aliasing to external services |
| **Headless (ClusterIP: None)** | No VIP; DNS returns Pod IPs | StatefulSets, when client wants to talk to a specific Pod |

### Endpoints and EndpointSlices

A Service has a selector; the endpoints controller watches Pods matching that selector and populates the endpoints list. Since 1.21, this is done via **EndpointSlices** (default 100 endpoints per slice) for scalability.

**Production gotcha:** if you create a Service with a selector and no Pods match, the Service has no endpoints. Health-check failures of backends ≠ Service failures. They're separate signals.

### kube-proxy internals

`kube-proxy` watches Services + EndpointSlices and programs the data plane.

- **iptables mode (legacy):** every Service becomes a chain of iptables rules. Per-packet cost is O(rules). Scales to ~5000 Services before latency shows.
- **IPVS mode:** uses IP Virtual Server (LVS). O(1) lookup. Better for clusters with thousands of Services.
- **eBPF mode (Cilium):** programs the kernel directly. Best for scale. Requires Cilium.

**Production rule:** if you have >2000 Services, use IPVS or eBPF kube-proxy. Otherwise iptables is fine.

### Session affinity and traffic policy

- `sessionAffinity: ClientIP` — sticky to a Pod. Used for stateful protocols (legacy TCP). Don't use it for HTTP.
- `externalTrafficPolicy: Cluster` (default) — kube-proxy SNATs to a Pod, original source IP is lost.
- `externalTrafficPolicy: Local` — only sends to Pods on the receiving Node, preserving source IP. Drops traffic if no local Pod. Use for source-IP-sensitive workloads.
- `internalTrafficPolicy: Local` — same idea, but for ClusterIP traffic from inside the cluster.
- `topologyKeys` — old API for topology-aware routing, deprecated. Use `topologyAwareHints` instead.

## 3.4 DNS — CoreDNS

Kubernetes clusters run CoreDNS as a Deployment (or in older setups, kube-dns). Every Service gets a DNS record:
- `<svc>.<ns>.svc.cluster.local` → ClusterIP
- `<pod-ip>.<ns>.pod.cluster.local` → for StatefulSet Pods (when enabled)
- `Headless services` → A record per Pod

### Production DNS issues

- **Negative caching (nodelocaldns).** Old NXDOMAIN answers stick for 5 seconds (default). A bad Service DNS that resolves once will keep failing for 5s.
- **Search path.** Pods inherit a search path of `<ns>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`. `curl postgres` from a Pod resolves to `postgres.<ns>.svc.cluster.local`.
- **DNS load.** Every Service lookup goes to CoreDNS. Thousands of Services and short-lived connections = DNS bottleneck. Mitigations: NodeLocal DNSCache, caching in app, IP-based service discovery (client-go loadbalancer).
- **Split DNS.** You can run different CoreDNS for different namespaces.

## 3.5 Ingress

Ingress is HTTP(S) routing at L7. It's an API; you install a controller that implements it.

### Controllers in production

| Controller | Notes |
|------------|-------|
| **ingress-nginx** | Default in many distros; high feature coverage; perf issues at scale |
| **Traefik** | Good DX, middleware concept, nice for mixed HTTP/TCP |
| **HAProxy Ingress** | Solid performance, less ergonomic |
| **GKE/Gateway API** | Cloud-managed; preferred if on GKE |
| **Envoy Gateway / Istio** | Gateway API implementation, more powerful, more ops |

### Ingress → Gateway API

`Ingress` is deprecated in spirit. **Gateway API** is the successor: more expressive, multi-protocol, role-oriented (provider/consumer). If you're starting a new cluster, plan to use Gateway API.

Production gotcha: an Ingress without a controller does nothing. The YAML is just a request to a plugin you haven't installed.

### TLS termination

- **TLS termination at the Ingress** — controller handles cert, Pods see HTTP. Simple.
- **TLS passthrough** — controller forwards to Pod. Needed if Pods need client certs.
- **mTLS** — handled by service mesh (Istio, Linkerd) or by the app.

Cert management: **cert-manager** with Let's Encrypt / ACME is the standard. Use DNS-01 for wildcard certs.

## 3.6 NetworkPolicy

NetworkPolicy is the Kubernetes firewall. It's pod-to-pod, namespace-to-namespace, with allow rules. **No policy = all traffic allowed.**

### Production usage

- **Default-deny in every namespace.** Add a "deny all ingress/egress" policy, then allow only what you need.
- **Egress is often forgotten.** Without explicit egress rules, Pods can reach the internet (via NAT) and any Pod in any namespace. Most data exfil is egress.
- **CNI support required.** Flannel does not enforce NetworkPolicy; Calico/Cilium do.

```yaml
# Default-deny ingress
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
spec:
  podSelector: {}
  policyTypes: ["Ingress"]
---
# Allow DNS to kube-system
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

### Cilium-specific extensions

Cilium adds:
- **L7 policy** (HTTP, gRPC, Kafka aware).
- **FQDN policy** (egress by DNS name, with care for caching).
- **Identity-based policy** (labels of source workload, not IPs).

If you choose Cilium, lean into these.

## 3.7 Service mesh

A service mesh (Istio, Linkerd, Consul Connect, Cilium service mesh) puts a sidecar proxy in every Pod. The mesh handles:
- mTLS between services, automatic.
- L7 routing, retries, circuit breaking.
- Telemetry (request rate, latency, error rate per service).
- Authorization policy.

### Production trade-offs

- **Cost:** sidecars consume memory and CPU. 1000 Pods × 100 MiB = 100 GB. Mitigations: eBPF mesh (Cilium, Linkerd 2.x with eBPF), Istio Ambient Mesh (sidecar-less).
- **Latency:** sidecar adds a hop. p99 goes up a few ms.
- **Operational complexity:** mesh upgrades, control plane HA, debug story.
- **Value:** if you have many services and need mTLS at scale, a mesh is cheaper than building it yourself. If you have 10 services, it's overhead.

**Recommendation:** start without a mesh. Add one when you have a concrete need (mTLS at scale, L7 routing across many services, advanced traffic shifting).

## 3.8 IPv6 and dual-stack

Kubernetes 1.21+ supports dual-stack (IPv4 + IPv6 simultaneously).

**Current state:** IPv6 is workable but most tooling and CNIs are still IPv4-first. Real benefit: doesn't run out of IPv4 addresses in large clusters. Practical advice: stick with IPv4 single-stack unless you have a strong reason; AWS VPC CNI on dual-stack has gotchas.

## 3.9 Production failure modes (Part III scope)

- **CNI MTU mismatch.** Large packets drop silently. Looks like "random flakiness."
- **ClusterIP exhaustion.** /16 Cluster CIDR gives you ~65k IPs. With 200 Services × 200 endpoint slices, you can hit limits.
- **DNS as the bottleneck.** A spike in DNS queries (misconfigured Service, missing cache) melts CoreDNS.
- **NetworkPolicy that blocks DNS.** Pods can't resolve. Cascading failures.
- **kube-proxy iptables scaling.** 10k Services = noticeable latency per packet.
- **ExternalTrafficPolicy mishandling.** `Cluster` loses client IP; `Local` drops traffic on nodes with no Pods.
- **Cloud L4 LB idle timeout longer than app keepalive.** Connections get RST'd by the cloud.
- **No source NAT policy.** Pod-to-external traffic goes through node NAT; logging shows node IPs, not Pod IPs. Audit nightmares.

## 3.10 Decision table

| Choice | Pick when | Avoid when |
|--------|-----------|------------|
| Cilium CNI | Need scale, eBPF, L7 policy | You need legacy kube-proxy and a small cluster |
| Calico | Need strong policy, BGP networking | You don't want to think about network engineering |
| AWS VPC CNI | EKS, want security groups per Pod | You can't afford the IP planning |
| Overlay vs underlay | Overlay: easier, fewer IPs. Underlay: performance, observability | Don't choose underlay without an IP plan |
| Service mesh | Many services, mTLS at scale, L7 routing | < 20 services, no security requirement |
| Gateway API | Greenfield, multi-team | Stuck on Ingress-only ecosystem |
| NodeLocal DNSCache | > 1000 Services, many short connections | Small clusters (added complexity) |

## 3.11 Further reading

- [Kubernetes Networking Model](https://kubernetes.io/docs/concepts/services-networking/)
- [CNI Specification](https://github.com/containernetworking/cni)
- [Calico Documentation](https://docs.tigera.io/calico/latest/about)
- [Cilium Documentation](https://docs.cilium.io/)
- [Gateway API](https://gateway-api.sigs.k8s.io/)
- [NetworkPolicy Reference](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.30/#networkpolicy-v1-networking-k8s-io)