# 🏗️ K8s AI SRE Agent — Infrastructure Setup & Architecture Guide

> **Document Purpose**: Complete technical reference for the Kubernetes cluster, security model, database layer, custom resources (CRDs), and AI isolation infrastructure built for the K8s AI SRE Agent.

---

## 1. Executive Summary

We have established a local **3-node Kubernetes cluster** using `kind` (Kubernetes in Docker) tuned for an air-gapped, in-cluster AI SRE Agent. The infrastructure strictly implements node taints, RBAC isolation, priority classes, and network policies to ensure that AI workloads never starve production applications of resources or expose cluster secrets.

### Local Toolchain Installed
- **Docker Engine**: Installed and running (`docker-ce` 29.6.2)
- **kubectl**: `v1.36.3` (`/usr/local/bin/kubectl`)
- **kind**: `v0.23.0` (`/usr/local/bin/kind`)
- **helm**: `v3.21.3` (`/usr/local/bin/helm`)

---

## 2. Cluster Topology & Node Isolation

The cluster **`sre-agent-cluster`** runs 3 nodes with explicit scheduling roles and taints:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                               kind Cluster: sre-agent-cluster                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                 │
      ┌──────────────────────────────────────────┼──────────────────────────────────────────┐
      ▼                                          ▼                                          ▼
┌───────────────────────────┐      ┌───────────────────────────┐      ┌───────────────────────────┐
│ Control Plane Node        │      │ Worker 1: App Workloads   │      │ Worker 2: AI Infra        │
│ sre-agent-cluster-control-│      │ sre-agent-cluster-worker  │      │ sre-agent-cluster-worker2 │
│ plane                     │      │ (tier=production-apps)    │      │ (tier=ai-infra)           │
├───────────────────────────┤      ├───────────────────────────┤      ├───────────────────────────┤
│ • K8s API Server          │      │ • PostgreSQL 15 DB        │      │ • Ollama StatefulSet      │
│ • CoreDNS & etcd          │      │   (monitoring namespace)  │      │   (ai-infra namespace)    │
│ • IncidentRecord CRD      │      │ • Kopf Controller (Target)│      │ • Taint:                  │
│ • PatchRequest CRD        │      │ • Production App Pods     │      │   ai-specialist=true:     │
│                           │      │   (auth-svc, pay-gw)      │      │   NoSchedule              │
└───────────────────────────┘      └───────────────────────────┘      └───────────────────────────┘
```

### Node Details
1. **Control Plane** (`sre-agent-cluster-control-plane`): Runs standard Kubernetes control plane services and etcd.
2. **Worker 1** (`sre-agent-cluster-worker`): Designated for app workloads and monitoring control loops. Host port mappings bound to `127.0.0.1`:
   - Port `3000` → Grafana UI
   - Port `5432` → PostgreSQL DB
3. **Worker 2** (`sre-agent-cluster-worker2`): Dedicated AI infrastructure node.
   - **Taint**: `ai-specialist=true:NoSchedule`
   - Only pods with explicit toleration (`ai-specialist`) can schedule on this node, ensuring Ollama CPU/RAM spikes never degrade user workloads.

---

## 3. Namespaces & Security Architecture

### Namespaces
- **`monitoring`**: Houses the PostgreSQL database, Kopf operator controller, Patch executor, and Grafana dashboard.
- **`ai-infra`**: Houses the Ollama LLM StatefulSet and model init jobs. Fully air-gapped.

### Dual ServiceAccount RBAC Model ([k8s/rbac.yaml](file:///home/rahul/K8s/k8s/rbac.yaml))

To prevent **Prompt Injection** vulnerabilities from compromising cluster secrets, permissions are split into two distinct ServiceAccounts:

```
┌──────────────────────────────────────┐          ┌──────────────────────────────────────┐
│  Observer ServiceAccount             │          │  Executor ServiceAccount             │
│  (sre-observer-sa)                   │          │  (sre-executor-sa)                   │
├──────────────────────────────────────┤          ├──────────────────────────────────────┤
│ • READ: pods, pods/log, events, nodes│          │ • PATCH: deployments, statefulsets   │
│ • LIST: secrets (names ONLY)          │          │ • PATCH: configmaps                  │
│ • FULL: patchrequests, incidentrecords│         │ • UPDATE: patchrequests              │
│ ❌ CANNOT read Secret payload values  │          │ ❌ NO ACCESS to Secrets/Nodes/RBAC    │
└──────────────────────────────────────┘          └──────────────────────────────────────┘
```

---

## 4. Custom Resource Definitions (CRDs)

### 1. `PatchRequest` CRD ([k8s/crds/patch-request-crd.yaml](file:///home/rahul/K8s/k8s/crds/patch-request-crd.yaml))
Acts as the **Human-in-the-Loop firewall**. The AI proposes fixes by creating `PatchRequest` resources; no patch is applied until an SRE approves it.

- **Group**: `sre.yourdomain.io/v1alpha1`
- **Scope**: Namespaced
- **Short Name**: `pr`
- **Status States**: `Pending` → `Approved` | `Rejected` → `Applied`
- **Key Fields**:
  - `spec.targetDeployment`, `spec.errorState`, `spec.rootCause`, `spec.proposedPatch` (JSON Patch)
  - `spec.severity` (`low`, `medium`, `high`, `critical`), `spec.confidence` (`low`, `medium`, `high`)
  - `spec.autoRestartSafe` (boolean gate for safe restarts)

### 2. `IncidentRecord` CRD ([k8s/crds/incident-record-crd.yaml](file:///home/rahul/K8s/k8s/crds/incident-record-crd.yaml))
Persistent team incident history used for **Memory-Augmented Prompts** (few-shot context injection) and auto-generated runbooks.

- **Group**: `sre.yourdomain.io/v1alpha1`
- **Scope**: Cluster-wide
- **Short Name**: `inc`, `ir`
- **Key Fields**:
  - `spec.incidentId` (e.g., `INC-2026-0047`), `spec.errorFingerprint` (SHA-256 hash)
  - `spec.state` (`Open` → `Investigating` → `Resolved` → `Closed`)
  - `spec.resolution` (`approvedBy`, `resolutionNotes`, `worked`, `mttr`)

---

## 5. PostgreSQL Database Layer ([k8s/postgres-statefulset.yaml](file:///home/rahul/K8s/k8s/postgres-statefulset.yaml))

Deployed as a **StatefulSet** (`postgres-0`) in `monitoring` namespace using image `postgres:15-alpine`.

### Schema Summary ([db/schema.sql](file:///home/rahul/K8s/db/schema.sql))
The database maintains 4 indexed tables:
1. **`incidents`**: Master incident tracking table with full LLM JSON diagnosis, timestamps, and MTTR metrics.
2. **`state_transitions`**: Audit trail of every lifecycle transition (`Open` → `Investigating` → `Resolved` → `Closed`).
3. **`patch_requests`**: Log of all proposed patches and approval outcomes.
4. **`incident_metrics`**: Time-series metrics for Grafana performance graphs.

---

## 6. AI Layer & Air-Gapped Network Isolation

### Ollama StatefulSet ([k8s/ollama-statefulset.yaml](file:///home/rahul/K8s/k8s/ollama-statefulset.yaml))
- **Namespace**: `ai-infra`
- **Placement**: Binds to `sre-agent-cluster-worker2` via toleration `ai-specialist=true:NoSchedule`
- **Resource Constraints**: Request `1 CPU / 2Gi RAM`, Limit `3 CPU / 5Gi RAM`
- **Model Init Job** ([k8s/ollama-model-init-job.yaml](file:///home/rahul/K8s/k8s/ollama-model-init-job.yaml)): Automatically pulls `deepseek-coder:6.7b-instruct` once Ollama readiness probe succeeds.

### Air-Gapped Network Policy ([k8s/network-policy.yaml](file:///home/rahul/K8s/k8s/network-policy.yaml))
- **Ingress**: Only accepts traffic from `monitoring` namespace on TCP port `11434`.
- **Egress**: Completely blocked except internal DNS lookup (`kube-system` UDP 53). Ensures cluster logs never leak outside the VPC.

---

## 7. Priority Classes & Quality of Service ([k8s/priority-classes.yaml](file:///home/rahul/K8s/k8s/priority-classes.yaml))

To prevent resource contention during incident spikes:
- `production-critical` (Value: `1,000,000`): Production services (`auth-service`, `payment-gateway`).
- `sre-monitoring` (Value: `500,000`): Kopf Operator & PostgreSQL.
- `ai-low-priority` (Value: `100`): Ollama LLM pod.

---

## 8. Master Infrastructure File Index

| File Path | Description |
|---|---|
| [kind-config.yaml](file:///home/rahul/K8s/kind-config.yaml) | 3-node cluster topology & localhost port mappings |
| [k8s/namespace.yaml](file:///home/rahul/K8s/k8s/namespace.yaml) | Declarative namespaces (`monitoring`, `ai-infra`) |
| [k8s/rbac.yaml](file:///home/rahul/K8s/k8s/rbac.yaml) | Dual ServiceAccounts & ClusterRoles |
| [k8s/crds/patch-request-crd.yaml](file:///home/rahul/K8s/k8s/crds/patch-request-crd.yaml) | Custom Resource Definition for `PatchRequest` |
| [k8s/crds/incident-record-crd.yaml](file:///home/rahul/K8s/k8s/crds/incident-record-crd.yaml) | Custom Resource Definition for `IncidentRecord` |
| [db/schema.sql](file:///home/rahul/K8s/db/schema.sql) | Full PostgreSQL 15 relational schema |
| [k8s/postgres-statefulset.yaml](file:///home/rahul/K8s/k8s/postgres-statefulset.yaml) | PostgreSQL StatefulSet & auto-init ConfigMap |
| [k8s/ollama-statefulset.yaml](file:///home/rahul/K8s/k8s/ollama-statefulset.yaml) | Ollama StatefulSet with node toleration & limits |
| [k8s/ollama-model-init-job.yaml](file:///home/rahul/K8s/k8s/ollama-model-init-job.yaml) | Init Job for pulling `deepseek-coder:6.7b` |
| [k8s/priority-classes.yaml](file:///home/rahul/K8s/k8s/priority-classes.yaml) | K8s scheduling PriorityClass definitions |
| [k8s/network-policy.yaml](file:///home/rahul/K8s/k8s/network-policy.yaml) | Air-gapped network policy for `ai-infra` |

---

## 9. Useful Verification Commands

```bash
# Check cluster node status & node taints
kubectl get nodes -o wide

# Verify custom resource definitions
kubectl get crds

# Check all running infrastructure pods across namespaces
kubectl get pods -A -o wide

# Test CRD CLI access
kubectl get patchrequests
kubectl get incidentrecords

# Inspect PostgreSQL logs
kubectl logs -n monitoring statefulset/postgres

# Inspect Ollama status
kubectl logs -n ai-infra statefulset/ollama
```
