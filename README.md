<div align="center">

# Kubernetes AI SRE Agent

**An autonomous, production-grade Site Reliability Engineering system that detects Kubernetes failures, diagnoses them with a multi-agent AI pipeline, proposes validated fixes, and self-monitors the outcome — all without human intervention until it matters.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.30-blue?logo=kubernetes)](https://kubernetes.io)
[![Kopf](https://img.shields.io/badge/Kopf-Operator_Framework-orange)](https://kopf.readthedocs.io/)
[![Vertex AI](https://img.shields.io/badge/Vertex_AI-Gemini_2.5_Flash-green?logo=google-cloud)](https://cloud.google.com/vertex-ai)
[![CI](https://github.com/2coolkalamkaar/k8s-SRE-Agent/actions/workflows/ci.yaml/badge.svg)](https://github.com/2coolkalamkaar/k8s-SRE-Agent/actions/workflows/ci.yaml)
[![Security](https://github.com/2coolkalamkaar/k8s-SRE-Agent/actions/workflows/security-scan.yaml/badge.svg)](https://github.com/2coolkalamkaar/k8s-SRE-Agent/actions/workflows/security-scan.yaml)
[![Helm](https://img.shields.io/badge/Helm-Chart_Ready-blue?logo=helm)](./charts/sre-agent/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Traces_%26_Metrics-blueviolet)](https://opentelemetry.io/)

</div>

---

##  What Is This?

Most teams detect a pod crash, someone gets paged at 2AM, they SSH in, look at logs, guess the fix, apply it, and hope it works. **This project automates that entire workflow end-to-end.**

The SRE Agent is a **Kubernetes Operator** that:

1. **Detects** pod failures across all namespaces in real-time
2. **Preprocesses** logs to eliminate noise and extract meaningful signals
3. **Runs a 3-agent AI pipeline** (Analyst → Fixer → Validator) powered by **Gemini 2.5 Flash** via Vertex AI
4. **Creates a structured `PatchRequest` CRD** containing the root cause, severity, proposed fix, and a pre-validated patch — ready for one-click SRE approval
5. **Applies the patch** once a human approves
6. **Monitors the deployment for 10 minutes** post-patch, automatically closing the incident if healthy or rolling back if the crash repeats

This is not a demo. It runs inside a real multi-node Kubernetes cluster with full observability (distributed traces to Grafana Tempo, custom metrics to Prometheus).

---

## ⚡ Verified Performance (Live Benchmark)

All numbers are from automated tests on a live 3-node Kind cluster. Run `python scripts/run_benchmarks.py` to reproduce.

| KPI | Result |
|-----|--------|
| **Detection Rate** | **7/10** incident types (all pod-level failures) |
| **Avg MTTR** (fault → patch generated) | **< 30 seconds** |
| **API Cost Savings** (3-layer dedup) | **80%+** over raw event count |
| **False Positive Rate** | **0%** — stable pods never generate alerts |
| **Rollback Safety** | Automatic `kubectl rollout undo` on bad patches |

### Detection Coverage

| Incident Type | Detected | MTTR | Diagnosis Quality |
|---------------|----------|------|--------------------|
| CrashLoopBackOff (bad entrypoint) | ✅ | ~25s | Root cause + spec patch |
| OOMKilled (memory limit) | ✅ | ~23s | Memory increase patch |
| ImagePullBackOff (bad tag) | ✅ | ~13s | Image correction |
| CreateContainerConfigError (missing ConfigMap) | ✅ | ~15s | ConfigMap guidance |
| CreateContainerConfigError (missing Secret) | ✅ | ~15s | Secret guidance |
| Init:CrashLoopBackOff (migration failure) | ✅ | ~25s | Init container fix |
| App crash with DB connection error (High) | ✅ | ~40s | Reads logs, identifies DB conn issue |

> Full benchmark methodology and results: [`docs/benchmark_results.md`](./docs/benchmark_results.md)

---

##  System Architecture

### High-Level Flow

```
  Kubernetes Cluster
  ═══════════════════════════════════════════════════════════════════════════
  
  Pod Fails
  (CrashLoopBackOff / OOMKilled / ImagePullBackOff / CreateContainerConfigError)
       │
       ▼
  ┌──────────────────────────────────────────────────────┐
  │            SRE Controller (kopf Operator)            │
  │                                                      │
  │  ┌──────────────┐  ┌───────────────┐  ┌───────────┐ │
  │  │ Layer 1      │  │ Layer 2       │  │ Layer 3   │ │
  │  │ Dampening    │─▶│ Fingerprint   │─▶│ Active PR │ │
  │  │ (3 in 5 min) │  │ Cache (SHA256)│  │ API Check │ │
  │  └──────────────┘  └───────────────┘  └─────┬─────┘ │
  │                          3-layer DEDUP       │       │
  └──────────────────────────────────────────────┼───────┘
                                                 │ Passes all 3 layers
                                                 ▼
  ┌──────────────────────────────────────────────────────┐
  │                Log Preprocessor                      │
  │   • Strips timestamps, k8s healthcheck noise         │
  │   • Extracts signal lines from crash logs            │
  │   • Pulls live K8s Events for context                │
  └──────────────────────────────────────────────────────┘
       │
       ▼  (cleaned logs + pod context)
  ┌──────────────────────────────────────────────────────┐
  │          Multi-Agent Remediation Pipeline             │
  │                                                      │
  │  ┌─────────────┐    ┌─────────────┐    ┌──────────┐  │
  │  │  Analyst    │───▶│   Fixer     │───▶│Validator │  │
  │  │   Agent     │    │   Agent     │    │  Agent   │  │
  │  │             │    │             │    │          │  │
  │  │ Root Cause  │    │ Proposes a  │    │ Dry-runs │  │
  │  │ Analysis    │    │ K8s patch   │    │ against  │  │
  │  │ via Gemini  │    │ via Gemini  │    │ real API │  │
  │  └─────────────┘    └─────────────┘    └──────────┘  │
  │       RCA                Patch         ✅ Valid?      │
  │                      (retry ×3 with                   │
  │                      error feedback)                  │
  └──────────────────────────────────────────────────────┘
       │
       ▼
  ┌──────────────────────────────────────────────────────┐
  │              PatchRequest CRD (K8s Resource)         │
  │                                                      │
  │  spec:                                               │
  │    incidentId: INC-2026-0809-7829                    │
  │    rootCause: "Invalid entrypoint script..."         │
  │    severity: high                                    │
  │    proposedPatch: { spec: { template: { ... }}}      │
  │    autoRestartSafe: true                             │
  │    confidence: high                                  │
  │                                                      │
  │  status:                                             │
  │    approvalState: Pending ◀──── SRE reviews here     │
  └──────────────────────────────────────────────────────┘
       │
       │  SRE: kubectl patch ... approvalState: Approved
       ▼
  ┌──────────────────────────────────────────────────────┐
  │               Executor Handler                        │
  │  Applies validated patch to the target Deployment    │
  └──────────────────────────────────────────────────────┘
       │
       ▼ (every 30s for 10 minutes)
  ┌──────────────────────────────────────────────────────┐
  │               Outcome Checker                         │
  │                                                      │
  │  Healthy? ──Yes──▶ Incident CLOSED (MTTR recorded)  │
  │     │                                                │
  │    No                                                │
  │     └──────────▶ Auto ROLLBACK + new PR created     │
  └──────────────────────────────────────────────────────┘
```

---

### Cluster Topology (3-Node Kind)

```
  sre-agent-cluster
  ├── control-plane          → K8s API Server, etcd, kube-scheduler
  │
  ├── worker (tier: production-apps)
  │   ├── Namespace: production    → Application workloads (crash-demo, order-svc, etc.)
  │   └── Namespace: monitoring    → sre-controller pod
  │
  └── worker2 (tier: ai-infra)     ← Tainted: dedicated to AI infra
      ├── Namespace: observability  → Prometheus, Grafana, Grafana Tempo
      └── (Previously: Ollama)      → Migrated to Vertex AI (Gemini 2.5 Flash)
```

---

##  Multi-Agent AI Pipeline (Deep Dive)

The brain of the system. Three specialized agents collaborate in sequence, each with a distinct responsibility:

```
  Pod Failure Signal
       │
       ▼
  ┌────────────────────────────────────────────────────────────┐
  │  AGENT 1: AnalystAgent                                     │
  │                                                            │
  │  Input:  preprocessed logs, K8s events, pod metadata,     │
  │          historical incident context                       │
  │                                                            │
  │  Task:   Root Cause Analysis — WHY did this pod fail?      │
  │                                                            │
  │  Output: { "root_cause": "...", "severity": "high",        │
  │            "likely_recurring": true,                       │
  │            "estimated_impact": "..." }                     │
  └─────────────────────────┬──────────────────────────────────┘
                            │ RCA JSON
                            ▼
  ┌────────────────────────────────────────────────────────────┐
  │  AGENT 2: FixerAgent                                       │
  │                                                            │
  │  Input:  RCA from Analyst, current Deployment spec,        │
  │          (on retry: Validator's rejection reason)          │
  │                                                            │
  │  Task:   Propose a concrete K8s strategic merge patch      │
  │                                                            │
  │  Output: { "patch": { "spec": { "template": {...}}},       │
  │            "suggested_fix": "human-readable description",  │
  │            "auto_restart_safe": true }                     │
  └─────────────────────────┬──────────────────────────────────┘
                            │ Patch JSON
                            ▼
  ┌────────────────────────────────────────────────────────────┐
  │  AGENT 3: ValidatorAgent                                   │
  │                                                            │
  │  Input:  Proposed patch from Fixer                         │
  │                                                            │
  │  Task:   Dry-run the patch against the LIVE Kubernetes API │
  │          (application/strategic-merge-patch+json)          │
  │                                                            │
  │  ✅ Valid  → Pipeline completes, PatchRequest CRD created  │
  │  ❌ Invalid → Error fed back to FixerAgent (retry ×3)      │
  └────────────────────────────────────────────────────────────┘
```

**Why this matters:** The Validator uses Kubernetes' own dry-run API — meaning the patch is proven syntactically and semantically valid *before* it ever reaches an SRE's eyes. No more proposing fixes that can't actually be applied.

---

##  3-Layer Deduplication Engine

A real Kubernetes cluster generates hundreds of pod status events per minute. Without deduplication, the LLM would be called thousands of times for the same crash. The dedup engine filters noise in three stages:

```
Event Received
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Event Dampening                                   │
│                                                             │
│  Require ≥3 events within a 5-minute sliding window         │
│  before triggering diagnosis. Eliminates transient blips.   │
│  (OOMKilled bypasses this — memory kills are always real)   │
└──────────────────────────┬──────────────────────────────────┘
                           │ Threshold crossed
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: Log Fingerprint Cache                             │
│                                                             │
│  SHA-256 hash of the cleaned crash log.                     │
│  If we've seen this exact crash pattern, skip the LLM.      │
│  TTL: 1h standard, 4h for recurring incidents.              │
└──────────────────────────┬──────────────────────────────────┘
                           │ New fingerprint
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Active PatchRequest API Check                     │
│                                                             │
│  Query the live K8s API for any existing Pending/Approved   │
│  PatchRequest for this deployment. Survives pod restarts.   │
│  If PR exists → increment seenCount, trigger ESCALATION     │
│  alert after 10 occurrences.                                │
└──────────────────────────┬──────────────────────────────────┘
                           │ No active PR
                           ▼
              Trigger Multi-Agent Pipeline
```

---

## Observability Stack

Full-stack distributed observability — not just logs.

```
  sre-controller
  ├── Distributed Traces ──▶ Grafana Tempo (via OTLP/gRPC)
  │   Every LLM call, every K8s API request, every pipeline
  │   execution is a traced span. Latency is observable.
  │
  └── Custom Prometheus Metrics (exposed on :9090/metrics)
      ├── sre_agent_incidents_total          → incidents detected
      ├── sre_agent_dedup_hits_total         → noise suppressed per layer
      ├── sre_agent_llm_duration_seconds     → Vertex AI latency histogram
      ├── sre_agent_llm_errors_total         → LLM call failures
      ├── sre_agent_patchrequests_total      → patches proposed
      ├── sre_agent_patch_outcomes_total     → success vs rollback rate
      └── sre_agent_mttr_seconds             → Mean Time to Resolution

  Grafana Dashboard (localhost:3000)
  ├── Prometheus datasource → all sre_agent_* metrics
  └── Tempo datasource      → distributed trace explorer
```

The `sre_agent_mttr_seconds` histogram is the north-star metric — it directly measures how much the system is reducing engineer toil.

---

##  Incident Lifecycle (State Machine)

Every incident is tracked as a `PatchRequest` CRD that transitions through a strict state machine:

```
  ┌─────────┐    pipeline    ┌─────────────┐    SRE approves   ┌─────────┐
  │  Open   │ ─────────────▶ │ Investigating│ ─────────────────▶ │ Applied │
  └─────────┘                └─────────────┘                    └────┬────┘
                                                                     │
                                          ┌──────────────────────────┤
                                          │   Outcome Checker (30s)  │
                                          │                          │
                                    ┌─────▼──────┐           ┌──────▼──────┐
                                    │  Healthy   │           │  Crashed    │
                                    │  for 10min │           │  again      │
                                    └─────┬──────┘           └──────┬──────┘
                                          │                         │
                                          ▼                         ▼
                                    ┌──────────┐             ┌──────────────┐
                                    │  CLOSED  │             │  ROLLBACK    │
                                    │ MTTR rec.│             │ + new PR     │
                                    └──────────┘             └──────────────┘
```

---

##  Project Structure

```
K8s/
├── controller/                    # The Operator — all Python source
│   ├── main.py                    # Kopf entry point: event handlers, auth override
│   ├── llm_client.py              # Multi-Agent Pipeline (Analyst/Fixer/Validator)
│   ├── log_preprocessor.py        # Log noise reduction + error state detection
│   ├── dedup.py                   # 3-layer deduplication engine
│   ├── states.py                  # Incident state machine
│   ├── incident.py                # Incident CRD management
│   ├── outcome_checker.py         # Post-patch health monitoring + auto-rollback
│   └── telemetry.py               # OpenTelemetry traces + Prometheus metrics
│
├── k8s/                           # All Kubernetes manifests
│   ├── crd-patchrequest.yaml      # PatchRequest custom resource definition
│   ├── crd-incidentrecord.yaml    # IncidentRecord custom resource definition
│   ├── controller-deployment.yaml # SRE Controller deployment
│   ├── rbac.yaml                  # Least-privilege ServiceAccounts + ClusterRoles
│   ├── network-policy.yaml        # Network isolation policies
│   └── observability/             # Prometheus, Grafana, Tempo manifests
│
├── demo-apps/                     # Failure scenarios for simulation
│   ├── crash-demo.yaml            # CrashLoopBackOff (bad entrypoint)
│   ├── payment-gateway-oom.yaml   # OOMKilled (memory limit exceeded)
│   ├── frontend-image-error.yaml  # ImagePullBackOff (bad image tag)
│   └── shipping-service-failure.yaml # Missing env var simulation
│
├── docs/                          # Operational documentation
│   ├── rca_dns_resolution_failure.md  # RCA: kopf DNS race condition fix
│   ├── simulation_guide_new_failure.md
│   └── ...                        # Architecture docs, incident reports
│
├── tests/                         # Unit tests
│   ├── test_preprocessor.py       # Log preprocessing + fingerprinting
│   └── test_states.py             # State machine transitions
│
├── charts/                        # Helm chart for one-command install
│   └── sre-agent/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml
│           ├── rbac.yaml
│           ├── crds.yaml
│           └── networkpolicy.yaml
│
├── .github/workflows/             # CI/CD pipelines
│   ├── ci.yaml                    # Test → Build → Push (GHCR)
│   └── security-scan.yaml         # Bandit + pip-audit + Trivy
│
├── scripts/                       # Operational tooling
│   └── run_benchmarks.py          # Automated capability benchmark suite
│
├── kind-config.yaml               # 3-node cluster topology
├── Dockerfile                     # Operator container image
└── requirements.txt               # Python dependencies
```

---

##  Quick Start

### Prerequisites

- Docker + [Kind](https://kind.sigs.k8s.io/) installed
- `kubectl` CLI
- A Google Cloud project with Vertex AI enabled
- A GCP service account JSON key

### 1. Create the Cluster

```bash
kind create cluster --config kind-config.yaml --name sre-agent-cluster
```

This provisions a 3-node cluster:
- `control-plane` — API server
- `worker` — application workloads (`tier: production-apps`)
- `worker2` — AI/observability infrastructure (`tier: ai-infra`)

### 2. Deploy CRDs, RBAC & Observability

```bash
# Custom Resource Definitions
kubectl apply -f k8s/crd-patchrequest.yaml
kubectl apply -f k8s/crd-incidentrecord.yaml

# RBAC (least-privilege service accounts)
kubectl apply -f k8s/rbac.yaml

# Observability stack (Prometheus + Grafana + Tempo)
kubectl apply -f k8s/observability/
```

### 3. Configure Vertex AI Credentials

```bash
# Create secret from your GCP service account key
kubectl create secret generic gcp-credentials \
  --from-file=credentials.json=/path/to/your/sa-key.json \
  -n monitoring
```

### 4. Build & Deploy the Controller

```bash
# Build the operator image
docker build -t sre-controller:latest .

# Load into Kind (no registry needed)
kind load docker-image sre-controller:latest --name sre-agent-cluster

# Deploy
kubectl apply -f k8s/controller-deployment.yaml

# Verify
kubectl get pods -n monitoring
# NAME                              READY   STATUS    RESTARTS
# sre-controller-xxx-xxx            1/1     Running   0
```

### Option B: Helm Install (Recommended)

```bash
helm install sre-agent ./charts/sre-agent/ \
  --set controller.env.vertexProject="your-gcp-project" \
  --set gcpCredentials.enabled=true \
  --set controller.env.alertWebhookUrl="https://hooks.slack.com/services/..."
```

See [`charts/sre-agent/values.yaml`](./charts/sre-agent/values.yaml) for all configurable options.

### Option C: Webhook Alerting (Slack/Discord/PagerDuty)

The controller can automatically pipe structured JSON alerts containing the AI's root cause analysis and the proposed patch preview to any webhook destination. 

If installing via standard manifests, simply add your webhook URL to the `ALERT_WEBHOOK_URL` environment variable in [`k8s/controller-deployment.yaml`](./k8s/controller-deployment.yaml). 

If using Helm, pass it as a flag:
```bash
--set controller.env.alertWebhookUrl="https://discord.com/api/webhooks/..."
```

---

##  End-to-End Demo

### 1. Simulate a CrashLoopBackOff Failure

```bash
kubectl apply -f demo-apps/shipping-service-failure.yaml
```

The controller detects it within seconds. Watch the pipeline run:

```bash
kubectl logs -n monitoring deploy/sre-controller -f
```

```
[INFO ] [INC-2026-0809-7829] New incident: production/shipping-service → CrashLoopBackOff
[INFO ] [INC-2026-0809-7829] Starting Multi-Agent Remediation Pipeline
[INFO ] [INC-2026-0809-7829] Analyst RCA: Invalid entrypoint command causes immediate exit
[INFO ] [INC-2026-0809-7829] Fixer Agent attempt 1/3
[INFO ] [INC-2026-0809-7829] Validator Agent dry-running patch
[INFO ] [INC-2026-0809-7829] ✅ Validator Agent approved patch
[INFO ] [INC-2026-0809-7829] PatchRequest CRD created: shipping-service-pr-2026-0809-7829
```

### 2. Inspect the PatchRequest

```bash
kubectl get pr -n production
# NAME                              DEPLOYMENT        ERROR             SEVERITY   STATE
# shipping-service-pr-2026-0809-7829 shipping-service  CrashLoopBackOff  high       Pending

kubectl get pr shipping-service-pr-2026-0809-7829 -n production -o yaml
```

The PatchRequest contains the full AI diagnosis, a human-readable fix description, and the exact Kubernetes patch to apply.

### 3. Approve the Fix

```bash
kubectl patch pr shipping-service-pr-2026-0809-7829 -n production \
  --subresource=status --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"sre@company.com"}}'
```

The operator immediately applies the patch. The outcome checker monitors health every 30 seconds. After 10 minutes of stability, it marks the incident **Closed** and records MTTR.

---

## Key Technical Decisions

| Decision | Rationale |
|---|---|
| **Kopf over client-go** | Python enables faster iteration; Kopf handles leader election, status patching, and retry backoff out of the box |
| **Vertex AI (Gemini 2.5 Flash)** | Fastest response time for structured JSON output; no GPU infra to manage; production-grade reliability |
| **Strategic Merge Patch for validation** | Standard JSON merge patch rejects partial container specs; strategic merge supports list-by-name semantics for containers |
| **SHA-256 log fingerprinting** | Prevents the same crash pattern from generating duplicate LLM calls — critical for cost control and alert fatigue |
| **CRD-based state machine** | Incident state lives in etcd, not in-memory. The controller survives pod restarts without losing context |
| **`KUBERNETES_SERVICE_HOST` for auth** | Bypasses `kopf`'s hardcoded `kubernetes.default.svc` DNS lookup which fails under CoreDNS race conditions on server restart |
| **Separate RBAC ServiceAccounts** | Observer SA (read + CRD write) and Executor SA (deployment patch only) follow least-privilege principle |

---

## Security & DevSecOps

Security is built into the pipeline, not bolted on:

- **Least-Privilege RBAC**: Two separate ServiceAccounts — the observer can only read and write CRDs; the executor can only patch Deployments and StatefulSets. Neither can touch Secrets, Nodes, or PersistentVolumes.
- **Network Policies**: Applied to restrict inter-namespace traffic.
- **Automated CI Security Scanning** (`.github/workflows/security-scan.yaml`) on every Pull Request:
  - **SAST** → `Bandit` static analysis on all Python operator code
  - **SCA** → `pip-audit` dependency vulnerability scanning
  - **IaC + Container Scan** → `Trivy` scans all Kubernetes manifests and the Dockerfile for misconfigurations and secrets

---

## Running Tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
pytest tests/ -v
```

Tests cover: log preprocessor, SHA-256 fingerprinting, error state detection, and state machine transitions.

---

##  Documentation

All operational documentation is in the [`/docs`](./docs) directory:

| Document | Description |
|---|---|
| [rca_dns_resolution_failure.md](./docs/rca_dns_resolution_failure.md) | Full RCA of the kopf DNS race condition on server restart |
| [simulation_guide_new_failure.md](./docs/simulation_guide_new_failure.md) | End-to-end simulation report of the shipping-service failure |
| [end_to_end_simulation_guide.md](./docs/end_to_end_simulation_guide.md) | Step-by-step guide to reproduce any failure scenario |
| [observability_implementation_guide.md](./docs/observability_implementation_guide.md) | How the full observability stack is wired |

---

##  Roadmap

- [x] **3-layer deduplication engine** — dampening + fingerprint cache + active PR check
- [x] **Init container failure detection** — `kopf.on.field` for `initContainerStatuses`
- [x] **CreateContainerConfigError bypass** — immediate trigger for non-restarting stuck pods
- [x] **Automated benchmark suite** — `scripts/run_benchmarks.py` with concrete metrics
- [x] **Helm chart** — one-command install via `helm install`
- [x] **GitHub Actions CI/CD** — test + build + push to GHCR on every PR/merge
- [x] **Webhook Alerts** — real-time JSON payloads for Slack/Discord/PagerDuty
- [ ] **Prometheus AlertManager webhook** — detect HTTP 500s, latency spikes
- [ ] **MTTD/MTTR Grafana dashboard** — business-level SLO tracking
- [ ] **Multi-namespace isolation** — per-namespace severity thresholds
- [ ] **Historical incident learning** — vector similarity search over past IncidentRecords
