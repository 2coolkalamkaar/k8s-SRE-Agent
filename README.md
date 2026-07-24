#  Kubernetes AI SRE Agent

An air-gapped, production-grade **Kubernetes AI SRE Operator** built using Python, [Kopf](https://kopf.readthedocs.io/), and in-cluster [Ollama](https://ollama.ai/) (`deepseek-coder:6.7b-instruct`).

The agent continuously observes cluster workloads, detects unhealthy pod states (`CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `CreateContainerConfigError`), runs log analysis and error fingerprinting, queries Ollama for diagnosis and remediation, and generates **Human-in-the-Loop `PatchRequest` CRDs** for SRE review and approval.

---

##  Infrastructure & Architecture

```
                       ┌─────────────────────────────────────────┐
                       │           Kubernetes API Server         │
                       └──────────────────┬──────────────────────┘
                                          │
                               Pod Watch  │  Apply approved patch
                               & Events   │  (human approval required)
                                          ▼
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                              SRE Controller                                  │
 │                                                                              │
 │  ┌───────────────────────┐   ┌──────────────────────┐   ┌─────────────────┐  │
 │  │ Layer 1: Dampening    ├──►│ Layer 2: Fingerprint ├──►│ Layer 3: Active │  │
 │  │ (3 events / 5 min)    │   │ Cache (1h/4h TTL)    │   │ PR Check (API)  │  │
 │  └───────────────────────┘   └──────────────────────┘   └────────┬────────┘  │
 └──────────────────────────────────────────────────────────────────┼───────────┘
                                                                    │ Pass all 3
                                                                    ▼
 ┌──────────────────────────────────────────────┐       ┌───────────────────────┐
 │               In-Cluster Ollama              │       │  PatchRequest CRD     │
 │  Node: worker2 (Tainted: node-role=ai-infra) │◄──────┤  Status: Pending      │
 │  Model: deepseek-coder:6.7b-instruct        │       └──────────┬────────────┘
 └──────────────────────────────────────────────┘                  │
                                                                   │ SRE Approves via
                                                                   │ `kubectl patch`
                                                                   ▼
                                                        ┌───────────────────────┐
                                                        │ Kopf Executor Handler │
                                                        │ Applies patch to Pod  │
                                                        └───────────────────────┘
```

### Key Components

1. **Kopf Operator (`controller/main.py`)**
   - **Watcher Handler**: Scans pod status changes and triggers diagnosis pipeline.
   - **Startup Catch-Up Handler**: Scans for pods that failed during controller downtime.
   - **Executor Handler**: Listens for `PatchRequest` objects set to `approvalState: Approved` and applies proposed patches to Deployments/StatefulSets.
2. **3-Layer Deduplication (`controller/dedup.py`)**
   - **Layer 1 (Event Dampening)**: Dampens transient blips by requiring 3 events in 5 minutes before calling Ollama. *OOMKilled events bypass dampening for immediate response.*
   - **Layer 2 (Log Fingerprint Cache)**: SHA-256 fingerprinting of cleaned crash logs prevents redundant LLM inference for identical crash patterns (1h default TTL, 4h for recurring incidents).
   - **Layer 3 (Active PR API Check)**: Queries K8s API for active `Pending` or `Approved` PatchRequests to survive controller pod restarts.
3. **State Machine (`controller/states.py` & `controller/incident.py`)**
   - Strict lifecycle: `Open → Investigating → Resolved → Closed`.
   - Enforces transactional RCA (Root Cause Analysis) validation before closure (`worked=True` & ≥ 30 chars RCA).
4. **Log Preprocessor (`controller/log_preprocessor.py`)**
   - Strips timestamps and noise (healthchecks, startup chatter) to preserve token context budget.
5. **CRD Specifications (`k8s/crd-patchrequest.yaml` & `k8s/crd-incidentrecord.yaml`)**
   - `PatchRequest` (`pr`): Namespaced custom resource containing root cause analysis, confidence scores, suggested fixes, and proposed patches.
   - `IncidentRecord` (`inc`): Cluster-scoped incident audit trail for CLI observability.

---

## 🚀 Quickstart & Setup Guide

### 1. Prerequisites

- **Linux OS** (or WSL2)
- **Docker** & **Kind** (Kubernetes in Docker)
- **kubectl** CLI
- **Python 3.11+**

### 2. Create the Kind Cluster

Provision a 3-node Kind cluster (Control Plane, App Worker, Dedicated AI Worker):

```bash
kind create cluster --config kind-config.yaml --name sre-agent-cluster
```

### 3. Deploy Custom Resource Definitions (CRDs) & RBAC

```bash
kubectl apply -f k8s/crd-patchrequest.yaml
kubectl apply -f k8s/crd-incidentrecord.yaml
kubectl apply -f k8s/rbac.yaml
```

### 4. Build and Deploy Controller

```bash
# Build local docker image
docker build -t sre-controller:latest .

# Load image into Kind cluster
kind load docker-image sre-controller:latest --name sre-agent-cluster

# Deploy controller
kubectl apply -f k8s/controller-deployment.yaml
```

Verify controller status:
```bash
kubectl get pods -n monitoring
```

---

## 🧪 Running Unit Tests

The repository includes unit tests covering the state machine transitions, log preprocessing, fingerprinting, and error state detection.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
pytest tests/ -v
```

---

## 🎮 Demo & Remediation Workflow

### 1. Simulate a Pod Failure

Deploy a failing workload (e.g., `ImagePullBackOff`):

```bash
kubectl apply -f demo-apps/frontend-web-failing.yaml
```

### 2. Inspect Generated PatchRequests

Watch the controller detect the issue and issue a `PatchRequest`:

```bash
kubectl get pr -n production
```

Output:
```
NAME                             DEPLOYMENT     ERROR              SEVERITY   STATE     SEEN   AGE
frontend-web-pr-2026-0724-85d7   frontend-web   ImagePullBackOff   low        Pending   1      2m
```

Inspect the LLM root cause analysis and suggested fix:
```bash
kubectl get pr frontend-web-pr-2026-0724-85d7 -n production -o yaml
```

### 3. Approve Remediation

SRE approves the proposed patch via `kubectl`:

```bash
kubectl patch pr frontend-web-pr-2026-0724-85d7 -n production \
  --subresource=status --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"sre-team@company.com"}}'
```

The operator will automatically apply the fix to the Deployment and heal the workload!

---

## 🛡️ Security & Privacy

- **100% Air-Gapped**: Runs entirely in-cluster. No external API calls or telemetry.
- **Least Privilege RBAC**: Observer SA has read/CRD privileges; Executor SA only has patch rights on Deployments, StatefulSets, and ConfigMaps.
