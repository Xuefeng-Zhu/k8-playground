"""
Realistic mock cluster data. Represents a mid-sized prod cluster with a
mix of healthy and problematic resources, so the audit shows realistic signal.
"""

DEMO = {
    "namespaces": [
        {"metadata": {"name": "prod", "labels": {"pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/audit": "restricted"}}},
        {"metadata": {"name": "staging", "labels": {"pod-security.kubernetes.io/enforce": "baseline"}}},
        {"metadata": {"name": "kube-system"}},
        {"metadata": {"name": "monitoring", "labels": {"pod-security.kubernetes.io/enforce": "baseline"}}},
    ],
    "deployments": [
        # Healthy: PDB, resources, probes, no SA mounted, pinned image, priority class
        {
            "metadata": {"name": "api-server", "namespace": "prod", "labels": {"app": "api"}},
            "spec": {"replicas": 5, "template": {"spec": {
                "serviceAccountName": "api-server",
                "automountServiceAccountToken": False,
                "priorityClassName": "production-critical",
                "terminationGracePeriodSeconds": 60,
                "topologySpreadConstraints": [{
                    "maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "labelSelector": {"matchLabels": {"app": "api"}},
                }],
                "containers": [{
                    "name": "api",
                    "image": "ghcr.io/myorg/api:1.4.2",
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                  "limits": {"cpu": "500m", "memory": "512Mi"}},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10, "failureThreshold": 3},
                    "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}, "periodSeconds": 5, "failureThreshold": 2},
                    "startupProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 5, "failureThreshold": 30},
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 1000,
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "env": [{"name": "LOG_LEVEL", "value": "info"}],
                }],
                "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "api", "effect": "NoSchedule"}],
            }}},
        },
        # No PDB, no resource limits, default SA, :latest tag, hardcoded secret, no priority
        {
            "metadata": {"name": "worker", "namespace": "prod", "labels": {"app": "worker"}},
            "spec": {"replicas": 3, "template": {"spec": {
                "hostNetwork": True,   # shares host net ns — SC-008 fail
                "containers": [{
                    "name": "worker",
                    "image": "myorg/worker:latest",   # mutable ref — WL-006 fail
                    "imagePullPolicy": "Never",        # air-gap pattern — WL-007 warn
                    "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}},  # no limits
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 5, "failureThreshold": 3},
                    # no readiness, no startup
                    "env": [
                        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db-creds", "key": "password"}}},
                        {"name": "API_KEY", "value": "sk-liv...f456"},  # hardcoded secret in env
                    ],
                }],
            }}},
        },
        # Privileged container, no probes, no resources, hostPID, :main tag (mutable)
        {
            "metadata": {"name": "legacy-billing", "namespace": "prod", "labels": {"app": "billing"}},
            "spec": {"replicas": 2, "template": {"spec": {
                "terminationGracePeriodSeconds": 5,  # too short — WL-009 warn
                "hostPID": True,                      # shares host PID ns — SC-008 fail
                "containers": [{
                    "name": "billing",
                    "image": "myorg/billing:main",    # mutable :main — WL-006 fail
                    "imagePullPolicy": "Always",
                    "securityContext": {"privileged": True},  # bad
                    # Memory limit < request — RS-003 fail
                    "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "512Mi"}},
                    # no probes at all
                }],
            }}},
        },
        # Staging, no replicas defined explicitly, ok otherwise
        {
            "metadata": {"name": "api-staging", "namespace": "staging", "labels": {"app": "api"}},
            "spec": {"replicas": 1, "template": {"spec": {
                "containers": [{
                    "name": "api",
                    "image": "ghcr.io/myorg/api:1.5.0-rc.1",   # pinned (rc, but pinned)
                    "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
                    "livenessProbe": {"httpGet": {"path": "/api/v1/health", "port": 8080}},   # non-standard path — OB-004 warn
                    "readinessProbe": {"httpGet": {"path": "/api/v1/ready", "port": 8080}},
                }],
            }}},
        },
    ],
    "statefulsets": [
        {
            "metadata": {"name": "postgres", "namespace": "prod", "labels": {"app": "postgres"}},
            "spec": {"replicas": 3, "serviceName": "postgres", "template": {"spec": {
                "containers": [{
                    "name": "postgres",
                    "image": "postgres:15",
                    "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
                    "livenessProbe": {"tcpSocket": {"port": 5432}, "periodSeconds": 10},
                    "readinessProbe": {"tcpSocket": {"port": 5432}, "periodSeconds": 5},
                }],
                "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "gp3", "resources": {"requests": {"storage": "100Gi"}}}}],
            }}},
        },
    ],
    "daemonsets": [
        {
            "metadata": {"name": "fluentbit", "namespace": "monitoring"},
            "spec": {"template": {"spec": {
                "containers": [{
                    "name": "fluentbit", "image": "fluent/fluent-bit:2.2",
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                }],
                "tolerations": [{"operator": "Exists"}],
            }}},
        },
    ],
    "pods": [
        # Pod with OOMKilled event
        {"metadata": {"name": "worker-abc", "namespace": "prod"}, "status": {"phase": "Running"}},
        {"metadata": {"name": "api-server-xyz", "namespace": "prod"}, "status": {"phase": "Running"}},
        {"metadata": {"name": "api-server-pending", "namespace": "prod"}, "status": {"phase": "Pending"}},
    ],
    "services": [
        {"metadata": {"name": "api-server", "namespace": "prod"}, "spec": {"type": "ClusterIP", "selector": {"app": "api"}}},
        {"metadata": {"name": "postgres", "namespace": "prod"}, "spec": {"type": "ClusterIP", "selector": {"app": "postgres"}}},
        {"metadata": {"name": "external-lb", "namespace": "prod"}, "spec": {"type": "LoadBalancer", "selector": {"app": "api"}}},
    ],
    "pvcs": [
        {"metadata": {"name": "data-postgres-0", "namespace": "prod"}, "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "100Gi"}}, "storageClassName": "gp3"}, "status": {"phase": "Bound"}},
    ],
    "network_policies": [
        {"metadata": {"name": "default-deny-ingress", "namespace": "prod"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}},
    ],
    "pdbs": [
        {"metadata": {"name": "api-server", "namespace": "prod"}, "spec": {"minAvailable": 2, "selector": {"matchLabels": {"app": "api"}}}},
        # worker has NO PDB
    ],
    "hpas": [
        {"metadata": {"name": "api-server", "namespace": "prod"}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "api-server"}, "minReplicas": 5, "maxReplicas": 50}},
    ],
    "service_accounts": [
        {"metadata": {"name": "api-server", "namespace": "prod"}, "automountServiceAccountToken": False},
        {"metadata": {"name": "default", "namespace": "prod"}},
        {"metadata": {"name": "default", "namespace": "staging"}},
        {"metadata": {"name": "default", "namespace": "monitoring"}},
    ],
    "configmaps": [
        {"metadata": {"name": "api-config", "namespace": "prod"}},
    ],
    "secrets": [
        {"metadata": {"name": "db-creds", "namespace": "prod"}, "type": "Opaque"},
    ],
    "events": [
        {"metadata": {"namespace": "prod"}, "reason": "OOMKilled", "message": "Container worker exceeded memory limits", "involvedObject": {"name": "worker-abc"}},
        {"metadata": {"namespace": "prod"}, "reason": "FailedScheduling", "message": "0/5 nodes are available: insufficient cpu"},
    ],
}
