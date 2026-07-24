# 🛡️ K8s SRE Agent — Full Architecture Blueprint

> **Goal**: An **air-gapped, in-cluster** autonomous SRE agent that watches Kubernetes for errors, diagnoses them with a **local Ollama LLM**, proposes human-approved `PatchRequest` fixes, and sends alerts via **Slack / Email** — keeping all logs inside your VPC.

> **Updated**: Includes 16GB CPU-only feasibility analysis, prompt structure spec, and honest tradeoff table.

---

## 1. 30,000-Foot Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                           │
│                                                                     │
│  ┌──────────┐   Watch API   ┌──────────────┐   HTTP (internal)     │
│  │  K8s API │──────────────▶│  Kopf        │──────────────────────▶│
│  │  Server  │               │  Controller  │                        │
│  └──────────┘               │  (Python)    │   ┌────────────────┐  │
│       ▲                     └──────┬───────┘   │  Ollama        │  │
│       │ RBAC (scoped)              │            │  (StatefulSet) │  │
│       │                     ┌──────▼───────┐   │  DeepSeek /    │  │
│  ┌────┴─────┐               │  Log Pre-    │   │  Llama-3       │  │
│  │ Service  │               │  Processor   │──▶│  (In-cluster)  │  │
│  │ Account  │               └──────┬───────┘   └────────────────┘  │
│  └──────────┘                      │                   │            │
│                                    │ LLM Response      │            │
│                             ┌──────▼───────┐           │            │
│                             │ PatchRequest │◀──────────┘            │
│                             │    CRD       │                        │
│                             └──────┬───────┘                        │
│                                    │                                │
│                      ┌─────────────▼──────────────┐               │
│                      │    Notifier Service          │               │
│                      │  (Slack Bot + SMTP)          │               │
│                      └─────────────┬───────────────┘               │
│                                    │ (Human reviews & approves)     │
│                             ┌──────▼───────┐                        │
│                             │  Patch       │                        │
│                             │  Executor    │                        │
│                             │  (restricted │                        │
│                             │   loop)      │                        │
│                             └──────────────┘                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Deep-Dive

### 2.1 The Observer — `Kopf` Controller

| Detail | Value |
|---|---|
| **Language** | Python 3.11+ |
| **Framework** | [Kopf](https://kopf.readthedocs.io/) (Kubernetes Operator Framework) |
| **Deployed As** | `Deployment` in `monitoring` namespace |
| **Trigger Conditions** | Pod → `CrashLoopBackOff`, `OOMKilled`, `Pending > N mins`, `ImagePullBackOff` |
| **Watch Method** | Kubernetes Watch API (streaming, not polling) |

**What it watches:**

```yaml
# RBAC ClusterRole — minimum viable permissions
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events", "nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "replicasets"]
    verbs: ["get", "list", "watch", "patch"]   # patch only for executor
  - apiGroups: ["sre.yourdomain.io"]
    resources: ["patchrequests"]
    verbs: ["get", "list", "create", "update", "patch"]
```

**Dampening Logic (anti-noise filter):**

```
Error Event Detected
       │
       ▼
Is this error >= 3 occurrences in 5 min?  ──No──▶ Discard (self-healing)
       │ Yes
       ▼
Is there already an OPEN PatchRequest for this pod/error hash?  ──Yes──▶ Discard (dedup)
       │ No
       ▼
Trigger Log Pre-Processor
```

---

### 2.2 The Pre-Processor — Log Sanitizer

> This is the **most critical** piece for local LLM quality. Raw logs are noisy; the LLM is expensive on CPU.

**Steps:**

1. **Fetch context bundle:**
   - `kubectl logs --previous --tail=200 <pod>`
   - `kubectl get events --field-selector involvedObject.name=<pod>`
   - `Pod.Spec` (YAML snippet — env vars, resource limits, image)
   - `kubectl top pod <pod>` (CPU/Mem snapshot)

2. **Strip noise:**
   - Remove repeated timestamp patterns (`regex`)
   - Keep only `Exception`, `Error`, `FATAL`, `Caused by`, `OOMKilled` lines
   - Truncate repetitive stack frame blocks

3. **Compute a SHA-256 hash** of the cleaned log → check against **Redis/in-memory cache** to avoid re-diagnosing the same crash pattern within 1 hour.

4. **Build the Prompt Bundle** (structured JSON):

```json
{
  "system_prompt": "You are a Kubernetes SRE expert. Analyze the context and return ONLY valid JSON.",
  "cluster_context": {
    "pod_name": "auth-service-7f9d",
    "namespace": "production",
    "error_state": "CrashLoopBackOff",
    "restart_count": 7,
    "resource_limits": {"cpu": "500m", "memory": "256Mi"},
    "env_vars_present": ["DB_HOST", "DB_PORT"],
    "env_vars_missing_suspected": ["DB_PASSWORD"]
  },
  "logs_excerpt": "...(cleaned, max 2000 tokens)...",
  "events": "...",
  "task": "Diagnose root cause and propose a kubectl patch in JSON Patch format."
}
```

---

### 2.3 The Brain — Ollama (In-Cluster)

| Detail | Value |
|---|---|
| **Deployed As** | `StatefulSet` with a `PersistentVolumeClaim` (50Gi+) |
| **Namespace** | `ai-infra` (isolated) |
| **Service DNS** | `http://ollama-service.ai-infra.svc.cluster.local:11434` |
| **Recommended Models** | `deepseek-coder-v2:16b` (best for YAML/JSON), `llama3:8b` (fast, lower RAM) |
| **Node Placement** | Dedicated node with `Taint: ai-specialist=true:NoSchedule` |
| **GPU** | Optional — NVIDIA plugin if available; CPU-only is viable (15–40s latency) |

**StatefulSet highlights:**

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ollama
  namespace: ai-infra
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    spec:
      tolerations:
        - key: "ai-specialist"
          operator: "Equal"
          value: "true"
          effect: "NoSchedule"
      priorityClassName: ai-low-priority    # below production workloads
      containers:
        - name: ollama
          image: ollama/ollama:latest
          ports:
            - containerPort: 11434
          volumeMounts:
            - mountPath: /root/.ollama
              name: ollama-models
  volumeClaimTemplates:
    - metadata:
        name: ollama-models
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 60Gi
```

**Expected LLM Output (strictly enforced via prompt):**

```json
{
  "root_cause": "The pod is in CrashLoopBackOff because the Secret 'db-creds' is missing from the namespace. The env var DB_PASSWORD references it.",
  "confidence": "high",
  "proposed_patch": {
    "type": "kubectl_apply",
    "resource": "Secret",
    "manifest": {
      "apiVersion": "v1",
      "kind": "Secret",
      "metadata": {"name": "db-creds", "namespace": "production"},
      "data": {"DB_PASSWORD": "<BASE64_PLACEHOLDER>"}
    }
  },
  "human_action_required": "Replace <BASE64_PLACEHOLDER> with the actual base64-encoded secret value.",
  "severity": "critical",
  "estimated_fix_time_minutes": 5
}
```

---

### 2.4 The PatchRequest CRD — The "Secret Sauce"

This is a **Custom Resource Definition** that acts as the approval gate. The LLM never touches the cluster directly.

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: patchrequests.sre.yourdomain.io
spec:
  group: sre.yourdomain.io
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                targetPod:       { type: string }
                targetNamespace: { type: string }
                errorState:      { type: string }
                rootCause:       { type: string }
                confidence:      { type: string, enum: [low, medium, high] }
                proposedPatch:   { type: object, x-kubernetes-preserve-unknown-fields: true }
                humanNote:       { type: string }
                severity:        { type: string, enum: [low, medium, high, critical] }
            status:
              type: object
              properties:
                approvalState:   { type: string, enum: [Pending, Approved, Rejected, Applied] }
                approvedBy:      { type: string }
                appliedAt:       { type: string }
  scope: Namespaced
  names:
    plural: patchrequests
    singular: patchrequest
    kind: PatchRequest
    shortNames: ["pr"]
```

**SRE Workflow with the CRD:**

```bash
# See all pending AI recommendations
kubectl get patchrequests -n production

# Review the proposed fix
kubectl describe patchrequest auth-service-fix-7a3f -n production

# Approve it (triggers the Patch Executor)
kubectl patch patchrequest auth-service-fix-7a3f \
  -n production \
  --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"rahul@company.com"}}'
```

---

### 2.5 The Patch Executor — Restricted Apply Loop

A **separate, lightweight controller** (also Kopf-based) that only watches `PatchRequest` objects for `approvalState=Approved`.

**Security constraints:**
- Runs as a **separate ServiceAccount** with minimal `patch` rights — only on `Deployments`, `StatefulSets`, `ConfigMaps`.
- **Never** has rights to: `Secrets`, `ClusterRoles`, `RBAC`, `Nodes`.
- All patch operations are **logged to an audit ConfigMap** for traceability.

```
PatchRequest.status → Approved
         │
         ▼
  Validate patch schema against whitelist
         │
   Pass ──────────▶  Apply patch via apps/v1 API
         │                    │
   Fail  ──────────▶  Mark as Rejected + Alert
                              │
                       Update status → Applied
                       Record timestamp + approvedBy
```

---

### 2.6 The Notifier — Slack + Email Alerts

**Triggered at two points:**

| Event | Channel | Message Content |
|---|---|---|
| New `PatchRequest` created | Slack `#sre-alerts` | Root cause, severity, link to PR, `kubectl get pr` command |
| `PatchRequest` Applied | Slack `#sre-resolved` + Email | Patch applied, who approved, MTTR estimate |
| LLM low-confidence diagnosis | Slack `#sre-needs-human` | Flags for manual review with raw logs attached |
| Patch Executor validation failure | Slack `#sre-critical` | Security alert, patch blocked |

**Slack message format (Block Kit):**

```
🔴 [CRITICAL] CrashLoopBackOff Detected
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pod:        auth-service-7f9d (production)
Restarts:   7 in last 10 minutes
Root Cause: Missing Secret 'db-creds'
Confidence: HIGH ✅

AI Proposed Fix Ready → kubectl get pr auth-service-fix-7a3f -n production

⏱ Review & approve to fix. Est. resolution: 5 min
```

---

## 3. Full Data Flow (End-to-End)

```
[K8s Event: Pod → CrashLoopBackOff]
           │
           ▼
[Kopf Controller detects via Watch API]
           │
     Dampening Check (3 errors / 5 min)
           │ PASS
           ▼
[Log Pre-Processor]
  ├── Fetch: logs --previous, events, pod.spec
  ├── Clean: strip timestamps, keep exceptions
  ├── Hash: SHA-256 → Cache check (Redis)
  └── Build: structured prompt JSON
           │
           ▼
[POST to http://ollama-service.ai-infra.svc.cluster.local:11434/api/generate]
           │ (All traffic stays inside cluster)
           ▼
[Ollama LLM → DeepSeek-Coder]
  └── Returns: root_cause + proposedPatch JSON
           │
           ▼
[Kopf Controller creates PatchRequest CRD object]
  └── status: Pending
           │
           ▼
[Notifier Service]
  ├── Slack: #sre-alerts → Block Kit alert
  └── Email: SRE team DL
           │
     [SRE Reviews kubectl describe patchrequest]
           │
     [SRE approves: kubectl patch ... Approved]
           │
           ▼
[Patch Executor watches for Approved PRs]
  ├── Validates patch schema
  ├── Applies patch via K8s API
  ├── Updates PR status → Applied
  └── Notifier: Slack #sre-resolved + Email
```

---

## 4. Namespace & Deployment Layout

```
cluster/
├── monitoring/          # Kopf Controller, Log Pre-processor, Notifier, Patch Executor
│   ├── kopf-controller (Deployment)
│   ├── patch-executor   (Deployment)
│   └── notifier-svc     (Deployment)
│
├── ai-infra/            # Ollama — fully isolated
│   ├── ollama           (StatefulSet + PVC 60Gi)
│   └── ollama-service   (ClusterIP — internal only)
│
├── cache/               # Dedup & caching layer
│   └── redis            (Deployment)
│
└── production/          # Your actual workloads
    └── patchrequests    (CRD instances live here)
```

---

## 5. Security Architecture

```
┌─────────────────────────────────────────────────────┐
│  Security Layers                                    │
│                                                     │
│  1. No egress: Ollama is ClusterIP only.            │
│     NetworkPolicy blocks all external traffic       │
│     from ai-infra namespace.                        │
│                                                     │
│  2. RBAC: Controller SA ≠ Executor SA.              │
│     Controller: read-only + create CRD.             │
│     Executor: patch Deployments/StatefulSets only.  │
│                                                     │
│  3. PatchRequest schema validation:                 │
│     JSON Schema enforced at CRD admission level.    │
│     Executor rejects any patch not matching schema. │
│                                                     │
│  4. Audit Trail:                                    │
│     Every Apply is logged to ConfigMap + Slack.     │
│     approvedBy field is mandatory.                  │
│                                                     │
│  5. Node Isolation:                                 │
│     Taint: ai-specialist=true:NoSchedule            │
│     Ollama pod is the ONLY resident on AI node.     │
│                                                     │
│  6. PriorityClass:                                  │
│     production-critical > monitoring > ai-low       │
│     AI never steals CPU from production pods.       │
└─────────────────────────────────────────────────────┘
```

---

## 6. Tech Stack Summary

| Component | Technology | Why |
|---|---|---|
| Event Observer | Python + Kopf | Fastest K8s operator framework; native Watch API |
| LLM Engine | Ollama (DeepSeek-Coder-v2 / Llama-3) | Air-gapped; no data leaves VPC |
| LLM Deployment | StatefulSet + PVC | Model persistence across pod restarts |
| Approval Gate | Custom CRD (`PatchRequest`) | Human-in-the-loop; auditability |
| Patch Execution | Kopf (separate loop) + K8s API | Scoped RBAC; never auto-applies |
| Caching / Dedup | Redis | Avoids re-diagnosing same crash pattern |
| Alerting | Slack API (Block Kit) + SMTP | Rich notifications; actionable messages |
| Log Parsing | Python regex + token counting | Prevents LLM context overflow |
| Secrets Mgmt | Kubernetes Secrets + external-secrets (optional) | Never hardcoded |

---

## 7. Implementation Phases

```
Phase 1 — Foundation (Week 1-2)
  ✅ Set up Ollama StatefulSet in ai-infra namespace
  ✅ Pull and test DeepSeek-Coder model
  ✅ Define PatchRequest CRD schema
  ✅ Write basic Kopf controller to detect CrashLoopBackOff

Phase 2 — Brain (Week 3-4)
  ✅ Build Log Pre-Processor with regex cleaning
  ✅ Implement prompt template + JSON output enforcement
  ✅ Integrate controller → Ollama → CRD creation pipeline
  ✅ Add Redis dedup cache

Phase 3 — Human-in-the-Loop (Week 5)
  ✅ Build Patch Executor (separate SA, scoped RBAC)
  ✅ Build Notifier (Slack Block Kit + SMTP)
  ✅ End-to-end test: error → diagnosis → approval → patch applied

Phase 4 — Hardening (Week 6-7)
  ✅ Add dampening/aggregation logic (N events / T minutes)
  ✅ Add Node Taint + PriorityClass for Ollama
  ✅ NetworkPolicy to isolate ai-infra namespace
  ✅ Audit logging to ConfigMap
  ✅ Prometheus metrics (PatchRequest counts, LLM latency, MTTR)

Phase 5 — Production (Week 8)
  ✅ Multi-namespace support
  ✅ Dashboard (Grafana) for PatchRequest history + MTTR trends
  ✅ Helm chart for full deployment
```

---

## 8. Key Design Decisions & Tradeoffs

> [!IMPORTANT]
> **Human-in-the-loop is non-negotiable.** The LLM proposes; the human approves. The `PatchRequest` CRD is the firewall between AI suggestion and cluster mutation.

> [!WARNING]
> **Ollama without GPU is 15–45s per inference.** This is acceptable for SRE tooling (not user-facing latency). Use async notification — the alert fires first, the PatchRequest appears within ~1 minute. The SRE sees both together.

> [!NOTE]
> **The Log Pre-Processor is the most impactful component for LLM quality.** A 200-line Java stack trace cleaned down to 20 lines of `Exception` + `Caused by` will give a local 8B model near-GPT-4 quality results for K8s diagnosis.

> [!CAUTION]
> **Never give the agent access to Secrets or RBAC.** Even if the LLM proposes a Secret patch, the Patch Executor should only apply non-sensitive resources. Secrets management should remain a human-only operation.

---

## 9. MTTR Impact Model

```
WITHOUT this agent:
  Alert fires → SRE acknowledges → SRE reads logs → SRE Googles → SRE writes patch → Apply
  MTTR: 15–45 minutes

WITH this agent:
  Alert fires → PatchRequest already waiting → SRE reviews 5 lines → kubectl approve
  MTTR: 2–8 minutes

Estimated MTTR Reduction: 70–85%
```

---

## 10. 🖥️ 16GB CPU-Only Cluster — Feasibility Analysis

> [!IMPORTANT]
> This is the most critical constraint. Here's the honest memory budget.

### Memory Budget (16GB total)

| Component | RAM Usage | Notes |
|---|---|---|
| K8s control plane (API server, etcd, scheduler) | ~2.5 GB | Non-negotiable |
| System pods (CoreDNS, kube-proxy, metrics-server) | ~300 MB | Non-negotiable |
| **Ollama + `deepseek-coder:6.7b` (Q4_K_M quant)** | **~4.5 GB** | ✅ Best fit for this setup |
| Your actual workloads | ~4–6 GB | Depends on app |
| Kopf Controller + Notifier | ~300 MB | Lightweight Python |
| Redis (dedup cache) | ~100 MB | Optional — see below |
| **Headroom / OS** | **~1.5 GB** | Buffer |
| **Total** | **~14–15 GB** | Tight but workable |

### ✅ What WORKS on 16GB CPU-only

```
✅ deepseek-coder:6.7b with Q4_K_M quantization (~4.5GB VRAM equivalent on CPU RAM)
✅ Inference latency: 30–60 seconds per diagnosis  ← acceptable for SRE tooling
✅ Kopf controller is extremely lightweight (~150MB)
✅ The async design means SRE is NOT blocked waiting for the AI
✅ Dampening logic prevents Ollama being hit on every pod blip
```

### ❌ What DOESN'T work on 16GB CPU-only

```
❌ llama3:70b          — Needs 40GB+ RAM. Forget it.
❌ deepseek-coder:16b  — Needs ~10GB RAM. Leaves nothing for workloads.
❌ Running inference on every K8s event — Will peg CPU at 100%, starving pods.
❌ Redis as a separate pod  — Drop it. Use in-memory dict in the Kopf controller.
```

### 🎯 Recommended Model for Your Setup

```
Model: deepseek-coder:6.7b-instruct (Q4_K_M)
Size:  ~4.5GB RAM
Speed: ~30-50 tokens/sec on modern CPU (16-32 cores)
Why:   Purpose-built for code/YAML/JSON tasks. Outperforms llama3:8b
       on structured output tasks like K8s manifest generation.

Alternative: phi3:mini (3.8B, ~2.3GB) — faster but less accurate
```

### Critical Adaption for 16GB: Drop Redis, Use In-Memory Dedup

```python
# In your Kopf controller — replace Redis with this
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta

# In-memory dedup store: {log_hash: last_diagnosed_at}
_diagnosis_cache: dict[str, datetime] = {}
CACHE_TTL = timedelta(hours=1)

def should_diagnose(log_content: str) -> bool:
    """Returns True if this log pattern hasn't been diagnosed in the last hour."""
    log_hash = hashlib.sha256(log_content.encode()).hexdigest()[:16]
    last_seen = _diagnosis_cache.get(log_hash)
    if last_seen and datetime.utcnow() - last_seen < CACHE_TTL:
        return False  # Already diagnosed recently
    _diagnosis_cache[log_hash] = datetime.utcnow()
    return True
```

---

## 11. 🎯 LLM Prompt Structure

This is the exact prompt template for Ollama. Structured output is enforced strictly.

### System Prompt (sent once)

```
You are a Kubernetes SRE expert. Your only job is to diagnose pod failures.
Always respond with ONLY valid JSON. No markdown. No explanation. No code blocks.
If you are not confident, set severity to 'low' and auto_restart_safe to false.
```

### User Prompt Template

```python
PROMPT_TEMPLATE = """
Analyze these Kubernetes pod failure logs and respond with ONLY valid JSON
(no markdown, no explanation, no code fences):

=== POD CONTEXT ===
Pod: {pod_name}
Namespace: {namespace}
Error State: {error_state}
Restart Count: {restart_count}
Resource Limits: CPU={cpu_limit}, Memory={mem_limit}
Environment Variables Present: {env_vars}

=== CLEANED LOGS (last crash) ===
{cleaned_logs}

=== K8S EVENTS ===
{events}

=== RESPONSE FORMAT ===
{{
    "root_cause": "One sentence explaining exactly what went wrong",
    "severity": "low|medium|high",
    "suggested_fix": "Step-by-step fix the operator should apply",
    "auto_restart_safe": true or false,
    "config_suggestions": ["ENV_VAR=value", "..."],
    "likely_recurring": true or false,
    "estimated_impact": "What breaks if this isn't fixed"
}}
"""
```

### How Each Field Maps to the System

| Field | Maps To | Used By |
|---|---|---|
| `root_cause` | `PatchRequest.spec.rootCause` | Slack alert message |
| `severity` | `PatchRequest.spec.severity` | Alert channel routing (`#sre-critical` vs `#sre-alerts`) |
| `suggested_fix` | `PatchRequest.spec.humanNote` | Shown to SRE in `kubectl describe` |
| `auto_restart_safe` | `PatchRequest.spec.autoRestartSafe` | **Safe auto-restart gate** (see below) |
| `config_suggestions` | `PatchRequest.spec.proposedPatch` | Translated to K8s patch by Executor |
| `likely_recurring` | `PatchRequest.spec.likelyRecurring` | Adjusts dampening TTL (if true → extend cache to 4h) |
| `estimated_impact` | Slack alert footer | Helps SRE prioritize review queue |

### The `auto_restart_safe` Gate

> [!NOTE]
> This is the **only** action the system takes without human approval.
> A pod restart is reversible. A config patch is not.

```
auto_restart_safe: true
    │
    ▼
Patch Executor automatically runs:  kubectl rollout restart deployment/<name>
    │
    ▼
Logs action to audit ConfigMap + Slack notification
    │
    ▼  (PatchRequest still created for full audit trail, marked auto-applied)

auto_restart_safe: false
    │
    ▼
PatchRequest stays Pending → human must explicitly approve
```

---

## 12. ⚖️ Honest Tradeoffs Table

| Tradeoff | The Good | The Risk | Mitigation |
|---|---|---|---|
| **Local LLM (Ollama) vs Cloud API** | Zero data egress. GDPR/SOC2 compliant. No API costs. | 30–60s inference on CPU. Model quality lower than GPT-4. | Async design hides latency. Pre-processor boosts accuracy. |
| **Kopf (Python) vs Go Operator** | Fastest to build. Great K8s SDK. Easy prompt logic. | Higher memory footprint than Go. GIL limits true concurrency. | Resource limits on pod. Use `asyncio` throughout Kopf. |
| **PatchRequest CRD (Human gate)** | Safety. Auditability. Trust. No rogue AI patches. | Adds latency to fix (human must act). | `auto_restart_safe` pathway for trivial fixes. Async Slack notification. |
| **16GB RAM / CPU-only** | Feasible with right model. No GPU cost. | Inference slow (30–60s). Memory pressure if workloads spike. | Use `deepseek-coder:6.7b-q4`. Dedicate a node to Ollama with taints. |
| **In-memory dedup (no Redis)** | Saves ~100MB RAM. Simpler architecture. | Cache lost on controller pod restart. Possible duplicate diagnoses after restart. | Acceptable for SRE tooling. Worst case: one duplicate Slack alert. |
| **Dampening (3 errors / 5 min)** | Prevents LLM CPU saturation. | May delay diagnosis on first-occurrence critical failures. | Tune thresholds per severity. `OOMKilled` → trigger immediately. `Pending` → wait. |
| **DeepSeek-Coder 6.7B model** | Great at YAML/JSON. Structured output. Low RAM. | Can hallucinate K8s API versions. No internet to verify. | Strict JSON schema validation before any patch is created. |
| **No egress NetworkPolicy** | Maximum security. Logs never leave cluster. | LLM cannot look up current K8s changelogs or CVEs. | Embed K8s version in prompt context. Pre-bake common error patterns into system prompt. |

---

## 13. 🔁 Deduplication & Dampening — No Duplicate Alerts

This is the most operationally critical layer. Without it, a CrashLoop with 100 restarts will fire 100 Slack alerts and run Ollama 100 times, pegging CPU and spamming the SRE team.

### The 3 Layers You Need

```
K8s Event Fires
      │
      ▼
┌────────────────────────────────────────────┐
│  LAYER 1: Event Dampening                          │
│  Has this pod errored >= 3 times in 5 minutes?    │
│  NO  → Discard (likely self-healing noise)         │
│  YES → Continue                                    │
└────────────────────────────────────────────┘
      │
      ▼
┌────────────────────────────────────────────┐
│  LAYER 2: Log Fingerprint Dedup                    │
│  SHA-256(cleaned_logs + error_state) in cache?    │
│  YES → Same crash pattern, skip LLM call           │
│       Update existing PatchRequest "seen_count"    │
│  NO  → New pattern, call Ollama                    │
└────────────────────────────────────────────┘
      │
      ▼
┌────────────────────────────────────────────┐
│  LAYER 3: Active PatchRequest Dedup               │
│  Is there already a Pending/Approved              │
│  PatchRequest for this deployment?                │
│  YES → Increment "seen_count", re-notify if       │
│       seen_count crosses a new threshold           │
│  NO  → Create new PatchRequest + Slack alert       │
└────────────────────────────────────────────┘
```

---

### Layer 1: Event Dampening

Counters per pod in a sliding time window. Only triggers Ollama when the error is **persistent**, not a one-off.

```python
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

# {pod_uid: [(timestamp, error_state), ...]}
_event_window: dict[str, list] = defaultdict(list)

DAMPEN_COUNT = 3           # Must see N errors...
DAMPEN_WINDOW_SECS = 300   # ...within this window (5 min)

# Special case: OOMKilled is always critical, trigger immediately
IMMEDIATE_TRIGGER_STATES = {"OOMKilled"}

def should_trigger(pod_uid: str, error_state: str) -> bool:
    """Layer 1: Event dampening. Returns True only if error is persistent."""
    now = datetime.utcnow()
    
    # OOMKilled: never dampen, trigger immediately
    if error_state in IMMEDIATE_TRIGGER_STATES:
        return True

    # Add current event and prune old ones outside the window
    window = _event_window[pod_uid]
    window.append((now, error_state))
    cutoff = now - timedelta(seconds=DAMPEN_WINDOW_SECS)
    _event_window[pod_uid] = [
        (ts, state) for ts, state in window
        if ts >= cutoff and state == error_state
    ]

    return len(_event_window[pod_uid]) >= DAMPEN_COUNT
```

---

### Layer 2: Log Fingerprint Dedup

Fingerprints the **crash pattern itself**, not just the pod name. If 50 different pods crash with the exact same `NullPointerException`, only ONE diagnosis is needed.

```python
import hashlib
from datetime import datetime, timedelta

# {fingerprint: (last_diagnosed_at, patch_request_name)}
_fingerprint_cache: dict[str, tuple[datetime, str]] = {}

def make_fingerprint(cleaned_logs: str, error_state: str) -> str:
    """Create a stable hash of the crash pattern."""
    content = f"{error_state}::{cleaned_logs}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]

def get_cache_ttl(likely_recurring: bool) -> timedelta:
    """
    If LLM says this error is likely recurring, extend the TTL
    so we don't spam diagnoses for a known flapping issue.
    """
    return timedelta(hours=4) if likely_recurring else timedelta(hours=1)

def check_fingerprint_cache(
    fingerprint: str
) -> tuple[bool, str | None]:
    """
    Returns (is_duplicate, existing_patchrequest_name).
    If duplicate: skip LLM call, update existing PR's seen_count instead.
    """
    if fingerprint not in _fingerprint_cache:
        return False, None
    
    last_seen, pr_name = _fingerprint_cache[fingerprint]
    # Use a conservative 1h TTL; updated to 4h after LLM sets likely_recurring
    if datetime.utcnow() - last_seen < timedelta(hours=1):
        return True, pr_name
    
    # Cache expired: allow re-diagnosis
    del _fingerprint_cache[fingerprint]
    return False, None

def register_fingerprint(fingerprint: str, pr_name: str):
    """Register after a PatchRequest is successfully created."""
    _fingerprint_cache[fingerprint] = (datetime.utcnow(), pr_name)
```

---

### Layer 3: Active PatchRequest Dedup

Queries the K8s API itself to check if an **open PatchRequest** already exists for the affected deployment. This is the **most important** layer because it survives controller restarts (unlike the in-memory cache).

```python
import kopf
import kubernetes

async def has_open_patchrequest(
    namespace: str,
    deployment_name: str,
    error_state: str,
    api: kubernetes.client.CustomObjectsApi
) -> tuple[bool, str | None]:
    """
    Layer 3: Check if a Pending/Approved PatchRequest already
    exists for this deployment + error combination.
    Returns (exists, patchrequest_name).
    """
    try:
        prs = api.list_namespaced_custom_object(
            group="sre.yourdomain.io",
            version="v1alpha1",
            namespace=namespace,
            plural="patchrequests",
            label_selector=f"target-deployment={deployment_name}"
        )
        for pr in prs.get("items", []):
            status = pr.get("status", {}).get("approvalState", "")
            spec_error = pr.get("spec", {}).get("errorState", "")
            # Block new PR if same error type is already Pending or Approved
            if status in ("Pending", "Approved") and spec_error == error_state:
                return True, pr["metadata"]["name"]
    except Exception:
        pass  # If API call fails, allow creation (fail-open)
    return False, None

async def increment_seen_count(
    pr_name: str,
    namespace: str,
    api: kubernetes.client.CustomObjectsApi
):
    """
    Instead of creating a duplicate, just bump the seen_count
    on the existing PatchRequest so the SRE knows this is escalating.
    """
    patch = {"spec": {"seenCount": {"$inc": 1}}}  # CRD handles this
    # Also send a single 'escalation' Slack nudge if count crosses 10
    current = api.get_namespaced_custom_object(
        group="sre.yourdomain.io", version="v1alpha1",
        namespace=namespace, plural="patchrequests", name=pr_name
    )
    seen = current.get("spec", {}).get("seenCount", 0) + 1
    api.patch_namespaced_custom_object(
        group="sre.yourdomain.io", version="v1alpha1",
        namespace=namespace, plural="patchrequests", name=pr_name,
        body={"spec": {"seenCount": seen}}
    )
    if seen in (10, 25, 50):  # Escalating nudge at key thresholds
        await send_escalation_alert(pr_name, namespace, seen)
```

---

### Putting It All Together (the main Kopf handler)

```python
@kopf.on.field("pods", field="status.containerStatuses")
async def on_pod_status_change(body, name, namespace, new, **kwargs):
    pod_uid = body["metadata"]["uid"]
    error_state = detect_error_state(new)   # CrashLoopBackOff, OOMKilled, etc.
    
    if not error_state:
        return  # Pod is healthy, ignore

    # --- LAYER 1: Dampening ---
    if not should_trigger(pod_uid, error_state):
        return  # Noise, ignore

    # --- Fetch and clean logs ---
    raw_logs = fetch_previous_logs(name, namespace)
    cleaned_logs = preprocess_logs(raw_logs)
    fingerprint = make_fingerprint(cleaned_logs, error_state)

    # --- LAYER 2: Log fingerprint cache ---
    is_dup, existing_pr = check_fingerprint_cache(fingerprint)
    if is_dup:
        await increment_seen_count(existing_pr, namespace, custom_api)
        return  # Same crash pattern already diagnosed

    # --- LAYER 3: Active PatchRequest check (survives restarts) ---
    deployment_name = get_owner_deployment(body)
    has_pr, existing_pr = await has_open_patchrequest(
        namespace, deployment_name, error_state, custom_api
    )
    if has_pr:
        await increment_seen_count(existing_pr, namespace, custom_api)
        return  # PR already open for this deployment + error

    # --- All 3 layers passed: call Ollama and create PatchRequest ---
    diagnosis = await call_ollama(cleaned_logs, body, error_state)
    pr_name = await create_patch_request(namespace, deployment_name, diagnosis)
    register_fingerprint(fingerprint, pr_name)
    await send_slack_alert(pr_name, diagnosis, namespace)
```

---

### Escalation Nudges (Not Duplicates)

When the same error persists despite an open PatchRequest, the SRE needs a **reminder**, not a new alert:

```
 Restart #1   → New PatchRequest created  → Slack: "🔴 CrashLoop detected"
 Restart #3   → seenCount = 3             → (silent, counted only)
 Restart #10  → seenCount = 10            → Slack: "⚠️ Still crashing (10x). Did you review the PatchRequest?"
 Restart #25  → seenCount = 25            → Slack: "🚨 ESCALATING: 25 crashes. PatchRequest still Pending."
 Restart #50  → seenCount = 50            → Slack + Email to SRE lead
```

---

### Dedup Reset Rules

| Event | Action |
|---|---||
| PatchRequest marked `Applied` | Clear fingerprint cache entry. If error returns, it's a **new incident**. |
| PatchRequest marked `Rejected` | Clear cache. Allow re-diagnosis after 30 min cooldown. |
| Pod recovers (no crash for 15 min) | Clear dampening counters for that pod UID. |
| `likely_recurring: true` from LLM | Extend fingerprint cache TTL from 1h → 4h. |
| Controller pod restart | Layers 1 & 2 reset (in-memory). Layer 3 (K8s CRD check) survives — this is the safety net. |

---

## 14. 🧠 Incident Memory — The Self-Improving Runbook

This turns the agent from a "one-shot debugger" into a **team knowledge base**. Every resolved incident is stored. Every future similar incident gets the past resolution surfaced immediately — before the LLM even runs.

### The Core Idea

```
New Incident Detected
        │
        ▼
  Lookup IncidentRecord history
        │
  ┌───────────────┬───────────────┐
  │ MATCH FOUND    │ NO MATCH       │
  ▼                ▼                │
Slack alert:     Call Ollama         │
"Seen before!    (standard flow)     │
 Here's the                          │
past fix"                            │
  │              AND inject          │
  │              top 3 past         │
  │              incidents into ────┘
  │              Ollama prompt
  ▼
Ollama produces
better diagnosis
because it has
context from history
        │
        ▼
  After fix applied → Create IncidentRecord
  (the loop is now complete)
```

---

### The `IncidentRecord` CRD Schema

Stored in **etcd** (already running in your cluster). Zero extra dependencies.

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: incidentrecords.sre.yourdomain.io
spec:
  group: sre.yourdomain.io
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                incidentId:        { type: string }   # INC-2026-0047
                errorState:        { type: string }   # CrashLoopBackOff
                errorFingerprint:  { type: string }   # SHA-256 hash of cleaned logs
                targetDeployment:  { type: string }
                targetNamespace:   { type: string }
                rootCause:         { type: string }   # From LLM
                llmDiagnosis:      { type: object, x-kubernetes-preserve-unknown-fields: true }
                resolution:
                  type: object
                  properties:
                    patchApplied:      { type: object, x-kubernetes-preserve-unknown-fields: true }
                    approvedBy:        { type: string }   # rahul@company.com
                    resolutionNotes:   { type: string }   # Free text from SRE
                    worked:            { type: boolean }  # Did the fix actually resolve it?
                    mttd:              { type: integer }  # Seconds to detect
                    mttr:              { type: integer }  # Seconds to resolve
                    resolvedAt:        { type: string }   # ISO timestamp
                recurrenceCount:   { type: integer }  # How many times this exact pattern appeared
                tags:              { type: array, items: { type: string } }
                  # e.g. ["missing-secret", "auth-service", "oom", "production"]
  scope: Cluster   # Cluster-scoped so it's queryable across all namespaces
  names:
    plural: incidentrecords
    singular: incidentrecord
    kind: IncidentRecord
    shortNames: ["inc", "ir"]
```

---

### 3-Stage History Lookup

When a new incident fires, the controller runs three lookup stages **before** calling Ollama:

```python
async def lookup_incident_history(
    fingerprint: str,
    error_state: str,
    deployment_name: str,
    api: kubernetes.client.CustomObjectsApi
) -> list[dict]:
    """
    3-stage lookup. Returns a list of relevant past incidents,
    ordered by relevance (most relevant first).
    """
    results = []

    # ---- STAGE 1: Exact fingerprint match ----
    # Same crash pattern = same logs hash. Highest confidence.
    exact = api.list_cluster_custom_object(
        group="sre.yourdomain.io", version="v1alpha1",
        plural="incidentrecords",
        label_selector=f"fingerprint={fingerprint}"
    )
    if exact["items"]:
        # Sort by most recent, take top 1
        results.extend({
            "match_type": "exact",
            "confidence": "high",
            **item["spec"]
        } for item in sorted(
            exact["items"],
            key=lambda x: x["spec"]["resolution"]["resolvedAt"],
            reverse=True
        )[:1])

    # ---- STAGE 2: Same error type + same deployment ----
    # Different crash but same service — might share root cause.
    same_service = api.list_cluster_custom_object(
        group="sre.yourdomain.io", version="v1alpha1",
        plural="incidentrecords",
        label_selector=f"deployment={deployment_name},error-state={error_state}"
    )
    for item in sorted(
        same_service["items"],
        key=lambda x: x["spec"]["resolution"]["resolvedAt"],
        reverse=True
    )[:2]:
        results.append({
            "match_type": "same_service",
            "confidence": "medium",
            **item["spec"]
        })

    # ---- STAGE 3: Same error type, any deployment ----
    # Different service, same error class — useful pattern reference.
    same_error = api.list_cluster_custom_object(
        group="sre.yourdomain.io", version="v1alpha1",
        plural="incidentrecords",
        label_selector=f"error-state={error_state}"
    )
    for item in sorted(
        same_error["items"],
        key=lambda x: x["spec"]["resolution"]["mttr"],  # Sort by fastest resolution
    )[:2]:
        if item["spec"].get("resolution", {}).get("worked"):
            results.append({
                "match_type": "same_error_class",
                "confidence": "low",
                **item["spec"]
            })

    return results  # Up to 5 past incidents, ordered by relevance
```

---

### Memory-Augmented Prompt

The history is **injected directly into the Ollama prompt** as few-shot examples. This is the key that makes local LLMs produce near-GPT-4 quality for repeat incidents.

```python
def build_augmented_prompt(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    past_incidents: list[dict]
) -> str:
    history_block = ""
    if past_incidents:
        history_block = "=== HISTORICAL CONTEXT (past incidents for reference) ===\n"
        for i, inc in enumerate(past_incidents, 1):
            resolution = inc.get("resolution", {})
            history_block += f"""
Incident #{i} [{inc['match_type'].upper()} MATCH - {inc['confidence']} confidence]:
  Previous error:    {inc['errorState']} on {inc['targetDeployment']}
  Root cause found:  {inc['rootCause']}
  Fix that worked:   {resolution.get('resolutionNotes', 'N/A')}
  Patch applied:     {json.dumps(resolution.get('patchApplied', {}), indent=2)}
  Resolved in:       {resolution.get('mttr', '?')} seconds
  Fixed by:          {resolution.get('approvedBy', 'unknown')}
"""
        history_block += "\nUse the above as context. If this matches a past incident, say so explicitly.\n"

    return f"""
You are a Kubernetes SRE expert with access to this team's incident history.
Always respond with ONLY valid JSON (no markdown, no explanation).

{history_block}

=== CURRENT INCIDENT ===
Pod: {pod_context['pod_name']} | Namespace: {pod_context['namespace']}
Error: {pod_context['error_state']} | Restarts: {pod_context['restart_count']}
Limits: CPU={pod_context['cpu_limit']}, Memory={pod_context['mem_limit']}
Env vars present: {pod_context['env_vars']}

=== CLEANED LOGS ===
{cleaned_logs}

=== K8S EVENTS ===
{events}

=== RESPONSE FORMAT ===
{{
    "root_cause": "One sentence explaining exactly what went wrong",
    "severity": "low|medium|high",
    "suggested_fix": "Step-by-step fix",
    "auto_restart_safe": true or false,
    "config_suggestions": ["ENV_VAR=value"],
    "likely_recurring": true or false,
    "estimated_impact": "What breaks if this isn't fixed",
    "matches_past_incident": "INC-2026-0031 or null",
    "confidence_boost": "high|none"
}}
"""
```

> [!NOTE]
> The `matches_past_incident` field is new. If the LLM recognises a past incident, the Slack alert will directly reference the old resolution. The SRE can then type `kubectl describe inc INC-2026-0031` to see exactly what worked.

---

### Slack Alert — With and Without History

**No history found (standard):**
```
🔴 [HIGH] CrashLoopBackOff — auth-service (production)
────────────────────────────────────
🧠 AI Diagnosis: Missing Secret 'db-creds'
📚 No similar incidents in history

▶ kubectl get pr auth-service-fix-a3f2 -n production
```

**History match found (shows past fix immediately, no wait):**
```
🔴 [HIGH] CrashLoopBackOff — auth-service (production)
────────────────────────────────────
📚 SEEN BEFORE — INC-2026-0031 (3 weeks ago)
   Cause:     Missing Secret 'db-creds'
   Fixed by:  rahul@company.com in 8 min
   Fix used:  kubectl create secret generic db-creds --from-literal=...
────────────────────────────────────
🧠 AI Diagnosis: (confirming past pattern, high confidence)

▶ kubectl describe inc INC-2026-0031   ← see full history
▶ kubectl get pr auth-service-fix-a3f2 -n production
```

---

### Incident Lifecycle — When Records Are Created & Updated

```
1. DETECTION
   PatchRequest created → IncidentRecord created (status: Open)
   Fields: errorState, fingerprint, rootCause, llmDiagnosis, tags
   Labels: fingerprint=<hash>, deployment=<name>, error-state=<state>

2. RESOLUTION
   SRE approves PatchRequest → Patch applied
   IncidentRecord updated:
     resolution.patchApplied  = what was applied
     resolution.approvedBy    = who approved
     resolution.resolvedAt    = timestamp
     resolution.mttd/mttr     = calculated automatically
   status: Resolved

3. OUTCOME VERIFICATION (automated, 15 min after apply)
   Kopf watches the deployment for 15 min after patch
   If pod is Running → resolution.worked = true
   If still crashing → resolution.worked = false + new Slack alert

4. SRE NOTES (optional enrichment)
   SRE can add notes anytime:
   kubectl patch inc INC-2026-0047 --type=merge \
     -p '{"spec":{"resolution":{"resolutionNotes":"Was a Vault rotation issue. Check vault lease expiry."}}}'
```

---

### Auto-Generated Runbook

Every `IncidentRecord` with `resolution.worked: true` is automatically a runbook entry. A weekly CronJob can export them:

```python
# Runs every Sunday, generates RUNBOOK.md in a ConfigMap
@kopf.timer("incidentrecords", interval=604800.0)  # weekly
async def generate_runbook(**_):
    records = fetch_all_resolved_incidents()

    # Group by error type
    by_error: dict[str, list] = defaultdict(list)
    for rec in records:
        if rec["spec"]["resolution"].get("worked"):
            by_error[rec["spec"]["errorState"]].append(rec)

    runbook_md = "# Auto-Generated SRE Runbook\n"
    runbook_md += f"_Last updated: {datetime.utcnow().isoformat()}_\n\n"

    for error_state, incidents in by_error.items():
        runbook_md += f"## {error_state}\n\n"
        for inc in incidents:
            res = inc["spec"]["resolution"]
            runbook_md += f"""
### {inc['spec']['incidentId']} — {inc['spec']['targetDeployment']}
- **Root Cause**: {inc['spec']['rootCause']}
- **Fix**: {res.get('resolutionNotes', 'See patch below')}
- **Patch**: `{json.dumps(res.get('patchApplied', {}))}`
- **MTTR**: {res.get('mttr', '?')} seconds
- **Tags**: {', '.join(inc['spec'].get('tags', []))}
"""

    # Store as ConfigMap for kubectl access
    save_runbook_to_configmap(runbook_md)

    # SREs can read it with:
    # kubectl get configmap sre-runbook -n monitoring -o jsonpath='{.data.RUNBOOK\.md}'
```

---

### SRE Commands for Incident History

```bash
# List all past incidents
kubectl get inc

# List incidents for a specific deployment
kubectl get inc -l deployment=auth-service

# List all OOMKilled incidents across the cluster
kubectl get inc -l error-state=OOMKilled

# See full details of a past incident (the runbook entry)
kubectl describe inc INC-2026-0031

# Find the 5 fastest-resolved CrashLoopBackOff incidents
kubectl get inc -l error-state=CrashLoopBackOff \
  -o json | jq '.items | sort_by(.spec.resolution.mttr) | .[0:5]'

# Add SRE notes to an incident (enriches the runbook)
kubectl patch inc INC-2026-0047 --type=merge \
  -p '{"spec":{"resolution":{"resolutionNotes":"Vault lease expired. Fix: renew lease."}}}'

# Read the auto-generated runbook
kubectl get configmap sre-runbook -n monitoring \
  -o jsonpath='{.data.RUNBOOK\.md}' | less

# MTTR trend: average resolution time by month
kubectl get inc -o json | \
  jq '[.items[].spec.resolution.mttr] | add / length'
```

---

### What This Gives You Over Time

```
Month 1:  Agent learns from scratch. LLM does all the work.
Month 3:  30-40% of incidents match past history. Slack shows the fix immediately.
Month 6:  70%+ of incidents are "known patterns". MTTR drops to under 2 minutes.
Month 12: Auto-runbook has 100+ entries. New SREs onboard in hours, not weeks.
          The team's tribal knowledge is now searchable and persistent.
```

> [!TIP]
> The `recurrenceCount` field on `IncidentRecord` is your most valuable long-term metric. If `auth-service` has had `CrashLoopBackOff` 15 times, that's a signal to fix the root cause permanently (e.g., add a proper init container to wait for the secret).
