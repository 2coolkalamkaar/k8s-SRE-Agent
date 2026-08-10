# Kubernetes Controllers — Deep Dive
## K8s AI SRE Agent · Complete Controller Reference

---

## Table of Contents

1. [What Is a Kubernetes Controller?](#1-what-is-a-kubernetes-controller)
2. [The Controller Pattern — Reconciliation Loop](#2-the-controller-pattern--reconciliation-loop)
3. [Why We Use Controllers (Instead of Polling / Cron)](#3-why-we-use-controllers-instead-of-polling--cron)
4. [Kopf — The Python Operator Framework](#4-kopf--the-python-operator-framework)
5. [Controller Architecture Overview](#5-controller-architecture-overview)
6. [Handler 1 — `catch_up_scan` (Startup Controller)](#6-handler-1--catch_up_scan-startup-controller)
7. [Handler 2 — `on_pod_status_change` (Main Watch Handler)](#7-handler-2--on_pod_status_change-main-watch-handler)
8. [Handler 3 — `on_patchrequest_approved` (PatchRequest Executor)](#8-handler-3--on_patchrequest_approved-patchrequest-executor)
9. [Sub-System: 3-Layer Dedup Pipeline (`dedup.py`)](#9-sub-system-3-layer-dedup-pipeline-deduppy)
10. [Sub-System: Log Preprocessor (`log_preprocessor.py`)](#10-sub-system-log-preprocessor-log_preprocessorpy)
11. [Sub-System: Multi-Provider LLM Client (`llm_client.py`)](#11-sub-system-multi-provider-llm-client-llm_clientpy)
12. [Sub-System: Incident State Machine (`incident.py`)](#12-sub-system-incident-state-machine-incidentpy)
13. [Custom Resource Definitions (CRDs)](#13-custom-resource-definitions-crds)
14. [RBAC — What the Controller Is Allowed to Do](#14-rbac--what-the-controller-is-allowed-to-do)
15. [End-to-End Flow Diagram](#15-end-to-end-flow-diagram)

---

## 1. What Is a Kubernetes Controller?

A **Kubernetes Controller** is a process that continuously watches the state of objects in the cluster (via the Kubernetes API) and takes action to make the **actual state** match the **desired state**.

> **Core principle**: Every controller is a *desired-state machine*. It does not blindly run tasks — it watches *what is*, compares it to *what should be*, and reconciles the difference.

### The Classic Example — ReplicaSet Controller (Built In)

```
Desired state:  replicas: 3   ← declared in YAML
Actual state:   replicas: 2   ← pod crashed
Action taken:   create 1 new pod  ← controller reconciles
```

This is exactly what `kube-controller-manager` does for built-in resources. **Our SRE controller extends this pattern to incidents** — it watches for error states in pods, diagnoses them with an LLM, and proposes patches via CRDs.

### Types of Controllers

| Type | Example | Who Implements? |
|------|---------|-----------------|
| Built-in controllers | ReplicaSet, DaemonSet, Deployment, Job | `kube-controller-manager` |
| Custom controllers (Operators) | SRE Agent, Prometheus Operator, cert-manager | You / the community |
| Admission controllers | OPA Gatekeeper, Kyverno | Kubernetes admission webhooks |

**Our SRE Agent is a custom controller (Operator)** implemented in Python using the Kopf framework.

---

## 2. The Controller Pattern — Reconciliation Loop

Every controller runs an infinite loop called the **reconciliation loop** (or *control loop*):

```
┌─────────────────────────────────────────────────────┐
│                  RECONCILIATION LOOP                │
│                                                     │
│   ┌──────────┐   Watch API   ┌──────────────────┐  │
│   │          │◄──────────────│   K8s API Server  │  │
│   │  EVENT   │               │  (etcd-backed)    │  │
│   │ RECEIVED │               └──────────────────┘  │
│   └────┬─────┘                                     │
│        │                                           │
│        ▼                                           │
│   ┌──────────┐                                     │
│   │ OBSERVE  │  Read current state of the object   │
│   └────┬─────┘                                     │
│        │                                           │
│        ▼                                           │
│   ┌──────────┐                                     │
│   │  DIFF    │  Compare actual vs desired          │
│   └────┬─────┘                                     │
│        │                                           │
│        ▼                                           │
│   ┌──────────┐                                     │
│   │   ACT    │  Create/update/delete resources     │
│   └──────────┘                                     │
└─────────────────────────────────────────────────────┘
```

**Key properties**:
- **Level-triggered, not edge-triggered**: If the controller misses an event, the next event catches up. Our startup scan (`catch_up_scan`) handles the downtime case explicitly.
- **Idempotent**: Running the same reconciliation twice should be safe. Our dedup pipeline ensures this.
- **Non-blocking**: Handlers are `async` so many pods can be processed concurrently.

---

## 3. Why We Use Controllers (Instead of Polling / Cron)

| Approach | Problem |
|----------|---------|
| **Cron / `sleep` loop** | Fixed polling interval — slow to react (30s cron = 30s average latency). Misses events that come and go between polls. |
| **Push via webhook** | Requires configuring admission webhooks per resource type. Doesn't work for pod state changes. |
| **Watch API directly** | Raw Watch API requires reconnection logic, bookmark handling, resource version tracking — complex to implement correctly. |
| **Kopf (what we use)** | Wraps the Watch API with automatic reconnect, retry, error handling, and Python decorator syntax. Sub-second event latency. |

**The Watch API** (used by Kopf under the hood) keeps an HTTP streaming connection open to the API server. When any Pod's `.status.containerStatuses` field changes, the API server pushes the diff down the stream immediately. No polling needed.

---

## 4. Kopf — The Python Operator Framework

[Kopf](https://kopf.readthedocs.io/) (**K**ubernetes **Op**erator **F**ramework) is the Python library that powers our controller.

### What Kopf Does For Us

| Feature | What It Handles |
|---------|----------------|
| **Watch loop** | Opens a streaming Watch to the K8s API for each resource type we register |
| **Reconnection** | Automatically reconnects if the Watch stream drops (network blip, API server restart) |
| **Resource version tracking** | Maintains `resourceVersion` bookmarks so events aren't replayed or missed across reconnects |
| **Handler dispatching** | Calls our decorated Python functions when matching events arrive |
| **Error handling** | Retries handlers with exponential back-off on transient errors |
| **Startup/cleanup hooks** | `@kopf.on.startup()` and `@kopf.on.cleanup()` lifecycle hooks |
| **Status patching** | Helpers to patch `.status` sub-resources on CRDs |

### How Kopf Handlers Are Declared

```python
# Watch any K8s resource field change
@kopf.on.field("pods", field="status.containerStatuses")
async def on_pod_status_change(body, name, namespace, new, logger, **kwargs):
    ...

# Startup hook (runs once on boot)
@kopf.on.startup()
async def catch_up_scan(logger, **kwargs):
    ...

# Watch our custom CRD field
@kopf.on.field("patchrequests", group="sre.yourdomain.io", field="status.approvalState")
async def on_patchrequest_approved(body, name, namespace, new, old, logger, **kwargs):
    ...
```

Kopf injects keyword arguments automatically: `body` (full object dict), `name`, `namespace`, `new` (new field value), `old` (old field value), `logger`, etc.

---

## 5. Controller Architecture Overview

```
                       K8s API Server (etcd)
                            │
              Watch: pods   │   Watch: patchrequests
                            │
                    main.py (Kopf Operator)
                            │
          ┌─────────────────┼──────────────────────┐
          │                 │                       │
    catch_up_scan    on_pod_status_change    on_patchrequest_approved
    (on startup)     (main event handler)    (approval executor)
          │                 │
          │        3-Layer Dedup (dedup.py)
          │        L1: Dampening
          │        L2: Fingerprint Cache
          │        L3: Active PR Check (K8s API)
          │                 │
          │        log_preprocessor.py
          │        Clean logs + SHA-256 fingerprint
          │                 │
          └────────► llm_client.py
                     Vertex AI → Gemini API → Ollama (fallback)
                             │
                     incident.py (State Machine)
                     Open → Investigating → Resolved → Closed
                             │
                     Create CRDs in K8s:
                     - PatchRequest (namespaced)
                     - IncidentRecord (cluster-scoped)
```

---

## 6. Handler 1 — `catch_up_scan` (Startup Controller)

**File**: [`main.py` L246–L296](file:///home/rahul/K8s/controller/main.py#L246-L296)

**Decorator**: `@kopf.on.startup()`

### What It Does

Runs **once** every time the controller pod starts up. It solves a critical reliability gap: if the controller was down (restarted, OOMKilled, rolled out) while pods were crashing, those pods would never trigger `on_pod_status_change` because the Watch stream was offline.

### Algorithm

```python
@kopf.on.startup()
async def catch_up_scan(logger, **kwargs):
    for namespace in WATCH_NAMESPACES:
        pods = await v1.list_namespaced_pod(namespace=namespace)
        for pod in pods.items:
            error_state = detect_error_state(pod.container_statuses)
            if not error_state:
                continue  # Pod is healthy — skip
            deployment = _get_owner_deployment(pod)
            has_pr = await has_open_patchrequest(namespace, deployment, error_state)
            if not has_pr:
                # Missed during downtime — trigger diagnosis now
                asyncio.create_task(_run_diagnosis_pipeline(...))
```

### Why This Matters

| Scenario | Without Catch-Up | With Catch-Up |
|----------|-----------------|---------------|
| Controller restarted at 3 AM | Pods crashing during downtime never get diagnosed | Diagnosed immediately on boot |
| Rolling deploy of controller | 30–60s gap in coverage | Gap covered on startup |
| Node eviction of controller pod | Same as restart | Same coverage guarantee |

### Design Decisions

- Uses `asyncio.create_task()` so the scan doesn't block Kopf's startup sequence — handlers register immediately.
- Does **not** re-diagnose if a `PatchRequest` already exists (Layer 3 check) — prevents double-diagnosis on every restart.
- The Layer 3 check is K8s-API-backed (survives memory resets unlike Layer 1/2).

---

## 7. Handler 2 — `on_pod_status_change` (Main Watch Handler)

**File**: [`main.py` L301–L340](file:///home/rahul/K8s/controller/main.py#L301-L340)

**Decorator**: `@kopf.on.field("pods", field="status.containerStatuses")`

### What It Does

This is the **heart of the controller**. It fires every time any pod's `containerStatuses` array changes in the watched namespaces.

### Event Trigger Conditions

The Watch fires when:
- A container starts → `state.running` appears
- A container crashes → `state.waiting.reason = CrashLoopBackOff` appears
- A restart happens → `restartCount` increments
- OOMKill → `lastState.terminated.reason = OOMKilled` appears

### Handler Flow

```
K8s sends event (pod containerStatuses changed)
    │
    ├── Check namespace in WATCH_NAMESPACES? No → return early
    │
    ├── detect_error_state(new) → error_state?
    │       No error → clear_dampening(pod_uid) + return
    │       (Pod recovered — reset its counter)
    │
    ├── _get_owner_deployment(body) → deployment_name?
    │       None → log "can't determine deployment" + return
    │
    └── should_trigger(pod_uid, error_state)?  [Layer 1 dedup]
            Not yet → log "not persistent enough" + return
            YES → asyncio.create_task(_run_diagnosis_pipeline(...))
```

### Key Design: `new` vs `body`

Kopf passes two versions of the object:
- `body` → the **full** current pod spec+status
- `new` → **only** the changed field value (`containerStatuses` list)

The handler uses `new` for error detection (fast path) and `body` for full context (owner references, spec, etc.).

### Why `asyncio.create_task()`?

The diagnosis pipeline calls Ollama/Vertex AI (takes 2–300 seconds). Using `create_task()` returns from the handler immediately so Kopf can process the next event. Without this, **one slow Ollama call would block all other pod events**.

---

## 8. Handler 3 — `on_patchrequest_approved` (PatchRequest Executor)

**File**: [`main.py` L456–L551](file:///home/rahul/K8s/controller/main.py#L456-L551)

**Decorator**: `@kopf.on.field("patchrequests", group="sre.yourdomain.io", field="status.approvalState")`

### What It Does

Watches for human SRE operators (or automated systems) approving a `PatchRequest` CRD. When `approvalState` transitions to `"Approved"`, this handler:

1. Reads `spec.proposedPatch` from the CRD
2. Validates the patch kind is in the allowed whitelist
3. Applies the patch to the target Deployment via `AppsV1Api`
4. Updates the `PatchRequest.status.approvalState` to `"Applied"`

### Approval Flow

```
SRE runs:
  kubectl patch pr crash-demo-pr-... \
    --type merge \
    -p '{"status":{"approvalState":"Approved","approvedBy":"sre-rahul"}}'

  → Kopf detects field change: "Pending" → "Approved"
  → on_patchrequest_approved fires
  → Validates patch (Deployment/StatefulSet/ConfigMap only)
  → apps_api.patch_namespaced_deployment(...)
  → Updates PatchRequest status → "Applied"
```

### Security Controls

```python
# Only these resource types can be patched — hard whitelist
ALLOWED_KINDS = {"Deployment", "StatefulSet", "ConfigMap"}

if patch_kind not in ALLOWED_KINDS:
    await set_status("Rejected")
    return
```

The controller's `ServiceAccount` (`sre-executor-sa`) only has RBAC rights to patch Deployments, StatefulSets, and ConfigMaps. Even if the code validation were bypassed, the API server would reject any attempt to patch Secrets or RBAC resources.

### Patch Strategies

| `proposedPatch` contents | What happens |
|--------------------------|-------------|
| `spec_patch` dict present | Applies targeted container spec patch (env vars, resources, image) |
| No `spec_patch` | Falls back to rollout restart annotation (safe "try restarting" action) |

---

## 9. Sub-System: 3-Layer Dedup Pipeline (`dedup.py`)

**File**: [`dedup.py`](file:///home/rahul/K8s/controller/dedup.py)

This is the **noise filter** that prevents Ollama/Vertex AI from being called thousands of times for the same crash.

### Why Dedup Is Essential

A single pod in `CrashLoopBackOff` generates a Watch event **every few seconds** (every restart). Without dedup, one crashed pod = hundreds of Ollama requests per hour = GPU/CPU saturation + Slack spam.

---

### Layer 1: Event Dampening

**Purpose**: Only trigger diagnosis after a crash is **persistent** (not a one-off blip).

**Implementation**: In-memory time-windowed counter per pod UID.

```python
DAMPEN_COUNT = 3          # must see 3+ events...
DAMPEN_WINDOW_SECS = 300  # ...within 5 minutes

async def should_trigger(pod_uid: str, error_state: str) -> bool:
    # OOMKilled always triggers immediately (memory pressure = critical)
    if error_state in IMMEDIATE_TRIGGER_STATES:
        return True

    # Append current event to window
    _event_window[pod_uid].append((now, error_state))

    # Prune entries older than 5 min OR with different error_state
    _event_window[pod_uid] = [
        (ts, st) for ts, st in window
        if ts >= cutoff and st == error_state
    ]

    return len(_event_window[pod_uid]) >= DAMPEN_COUNT
```

**Limitation**: In-memory only — resets on controller restart. Layer 3 is the safety net.

---

### Layer 2: Log Fingerprint Cache

**Purpose**: Don't call Ollama twice for the **same crash pattern** (same error + same stack trace).

**Implementation**: SHA-256 hash of cleaned logs + error_state → 16-hex-char key.

```python
def make_fingerprint(cleaned_logs: str, error_state: str) -> str:
    content = f"{error_state}::{cleaned_logs}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]
```

**TTL Policy**:
- Default: `1 hour`
- If LLM says `likely_recurring=True`: `4 hours` — known flapping issues don't re-diagnose for 4h

**Limitation**: In-memory only. Layer 3 is the K8s-persistent backup.

---

### Layer 3: Active PatchRequest Check (K8s API)

**Purpose**: Don't create a second `PatchRequest` if one is already `Pending` or `Approved` for this deployment. Survives controller restarts.

**Implementation**: Query the K8s API for existing CRDs.

```python
async def has_open_patchrequest(namespace, deployment_name, error_state, custom_api):
    prs = await custom_api.list_namespaced_custom_object(
        plural="patchrequests",
        label_selector=f"target-deployment={deployment_name}",
    )
    for pr in prs["items"]:
        if pr["status"]["approvalState"] in ("Pending", "Approved"):
            if pr["spec"]["errorState"] == error_state:
                return True, pr["metadata"]["name"]
    return False, None
```

**Fail-open**: If the K8s API call fails (network issue), returns `(False, None)` — allows diagnosis to proceed. Better to diagnose twice than miss an incident.

---

### `increment_seen_count` — Escalation Mechanism

When a duplicate is detected (Layer 2 or 3), instead of silently dropping the event, we **increment `seenCount`** on the existing `PatchRequest`:

```python
if seen in (10, 25, 50):
    logger.warning("[ESCALATION] %s has been seen %d times and is still unresolved!", pr_name, seen)
    # TODO: Fire PagerDuty / escalate Slack alert
```

An unresolved `CrashLoopBackOff` that keeps crashing 50 times will trigger a high-severity escalation, even though Ollama was only called once.

---

### State Normalization Bug (Found & Fixed During Live Incident)

**Bug**: The kubelet alternates between two K8s states during a crash cycle:
1. `state.waiting.reason = "CrashLoopBackOff"` (after backoff timer fires)
2. `lastState.terminated.exitCode = 1` (immediately after crash)

Before the fix, `detect_error_state()` returned `"CrashLoopBackOff"` for state 1 and `"ContainerCrashed"` for state 2. The Layer 1 dampening window pruned entries with `st != error_state`, so the counter **reset to 1 on every alternation** and never reached 3.

**Fix** (in `log_preprocessor.py` L139–L143):
```python
# Before (bug)
if terminated.get("exitCode", 0) not in (0, None):
    return "ContainerCrashed"

# After (fixed) — normalise to same string as the waiting phase
if terminated.get("exitCode", 0) not in (0, None):
    return "CrashLoopBackOff"
```

Result: Counter now accumulates properly across the full crash cycle: `1/3 → 2/3 → 3/3 ✅`.

---

## 10. Sub-System: Log Preprocessor (`log_preprocessor.py`)

**File**: [`log_preprocessor.py`](file:///home/rahul/K8s/controller/log_preprocessor.py)

### What It Does

Raw pod logs are noisy (hundreds of lines of health checks, timestamps, startup chatter). Sending raw logs to an LLM wastes tokens and degrades diagnosis quality. The preprocessor extracts only the **actionable signal**.

### Processing Pipeline

```
Raw logs (200 lines, 8000+ chars)
    │
    ├── 1. Strip timestamps  (regex: ISO-8601, RFC-3339)
    │       "2024-01-15T10:23:45Z [INFO] ..."  →  "..."
    │
    ├── 2. Drop noise lines  (health checks, metrics, startup banners)
    │       "GET /healthz" → dropped
    │       "Listening on :8080" → dropped
    │
    ├── 3. Deduplicate consecutive identical lines
    │       "connection refused" × 40 → kept once
    │
    ├── 4. Sort: signal lines first, context lines second
    │       Exception/Error/Fatal lines → bubble to top
    │       Other lines → fill remaining budget
    │
    └── 5. Truncate to MAX_LINES=100, MAX_CHARS=4000
            Result: ~50 lines, ~1000 tokens
```

### Regex Patterns

```python
# Lines we KEEP (actionable signal)
_KEEP_PATTERN = re.compile(
    r"(exception|error|fatal|critical|caused by|traceback|oomkilled|"
    r"killed|sigkill|panic|segfault|connection refused|timeout|"
    r"secret.*not found|image.*not found|failed|exit code [^0])",
    re.IGNORECASE,
)

# Lines we DROP (noise)
_NOISE_PATTERN = re.compile(
    r"(health.?check|liveness|readiness|GET /|POST /|prometheus|"
    r"metrics|starting up|listening on|server config|level=INFO)",
    re.IGNORECASE,
)
```

### `detect_error_state` — State Mapping Table

Maps raw `containerStatuses` (complex K8s nested structure) to a canonical string:

| K8s Condition | Canonical String |
|---------------|-----------------|
| `state.waiting.reason = CrashLoopBackOff` | `"CrashLoopBackOff"` |
| `state.waiting.reason = Error` | `"CrashLoopBackOff"` |
| `lastState.terminated.exitCode != 0` | `"CrashLoopBackOff"` ← normalized |
| `lastState.terminated.reason = OOMKilled` | `"OOMKilled"` |
| `state.waiting.reason = ImagePullBackOff` | `"ImagePullBackOff"` |
| `state.waiting.reason = ErrImagePull` | `"ImagePullBackOff"` |
| `state.waiting.reason = CreateContainerConfigError` | `"CreateContainerConfigError"` |
| All containers healthy | `None` |

---

## 11. Sub-System: Multi-Provider LLM Client (`llm_client.py`)

**File**: [`llm_client.py`](file:///home/rahul/K8s/controller/llm_client.py)

### Provider Priority Chain

```
LLM_PROVIDER env var (default: "auto")
    │
    ├── "auto"
    │       ├── gcloud token available? → Vertex AI (GCP $300 credits, ~3.9s)
    │       ├── GEMINI_API_KEY set?     → Gemini API key (~2s)
    │       └── else                    → Ollama local (~2–5min on CPU)
    │
    ├── "vertex"  → Vertex AI, fall back to Gemini on failure
    ├── "gemini"  → Gemini API, fall back to Ollama on failure
    └── "ollama"  → Ollama only
```

### Provider 1: GCP Vertex AI

```python
# Auth: GCP Application Default Credentials
token = subprocess.check_output(
    ["gcloud", "auth", "application-default", "print-access-token"]
)

url = (
    f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{LOCATION}/publishers/google/models/{VERTEX_MODEL}:generateContent"
)
```

- **Model**: `gemini-2.5-flash` (configurable via `VERTEX_MODEL` env)
- **Latency**: ~3.9 seconds
- **Cost**: GCP project credits (uses $300 free trial)

### Provider 2: Gemini API Key

```python
url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
```

- **Model**: `gemini-2.0-flash` (configurable via `GEMINI_MODEL` env)
- **Auth**: API key from `.env` file or `GEMINI_API_KEY` environment variable
- **Response format**: `application/json` (native structured output — fewer parsing errors)

### Provider 3: Local Ollama (Fallback)

```python
OLLAMA_URL = "http://ollama-service.ai-infra.svc.cluster.local:11434"
OLLAMA_MODEL = "deepseek-coder:6.7b-instruct"
_ollama_semaphore = asyncio.Semaphore(3)  # max 3 concurrent calls

async with _ollama_semaphore:
    resp = await httpx.post(f"{OLLAMA_URL}/api/generate", json=payload)
```

- **Air-gap safe**: Works without internet access
- **Semaphore**: Caps concurrent Ollama calls at 3 to prevent CPU saturation
- **Latency**: 2–5 minutes on CPU (deepseek-coder 6.7B quantized)

### 5-Layer JSON Parser

LLMs sometimes return malformed JSON (markdown fences, trailing commas, JS comments). The parser tries 5 strategies before giving up:

```
Strategy 1: Strip ``` markdown fences → try JSON parse
Strategy 2: Direct JSON parse on cleaned text
Strategy 3: Regex extract first {...} block → try JSON parse
Strategy 4: Remove trailing commas → try JSON parse
Strategy 5: Remove JS-style // and /* */ comments → try JSON parse
FAIL: Return safe default dict (NEVER crashes the controller)
```

### Prompt Design

The prompt contains 6 sections sent to the LLM:
1. **System role**: "You are a K8s SRE expert. Respond ONLY with valid JSON."
2. **Historical context**: Past incidents for the same deployment (memory-augmented RAG)
3. **Current incident**: Pod name, namespace, error state, restart count, resource limits, env vars
4. **Cleaned logs**: Up to 100 lines / 4000 chars from `log_preprocessor.py`
5. **K8s events**: Last 20 events from `v1.list_namespaced_event()`
6. **Response schema**: Strict JSON format with all required fields

---

## 12. Sub-System: Incident State Machine (`incident.py`)

**File**: [`incident.py`](file:///home/rahul/K8s/controller/incident.py)

### State Machine

```
          ┌─────────┐
          │  Open   │  (created at detection)
          └────┬────┘
               │ start_investigation(llm_diagnosis)
               ▼
       ┌───────────────┐
       │ Investigating │  (LLM diagnosed, PatchRequest created)
       └───────┬───────┘
               │ mark_resolved(patch, approved_by)
               ▼
        ┌────────────┐
        │  Resolved  │  (patch applied)
        └─────┬──────┘
              ├── close(rca_summary) ──────────► ┌────────┐
              │                                   │ Closed │
              └── reopen_investigation() ────────► └────────┘
                  (patch didn't work)     ┌───────────────┐
                                          │ Investigating │
                                          └───────────────┘
```

### Why A State Machine?

Without explicit states:
- A "resolved" incident could accidentally be re-diagnosed
- An "Investigating" patch could be applied twice
- MTTR (Mean Time to Resolve) can't be computed accurately

The `Incident` dataclass enforces transitions through `State` objects (GoF State pattern). Direct mutation of `_state` is only possible through `State` subclasses.

### Timestamps Tracked

| Field | When Set |
|-------|----------|
| `opened_at` | Object creation (`__post_init__`) |
| `investigating_at` | `start_investigation()` called |
| `resolved_at` | `mark_resolved()` called |
| `closed_at` | `close()` called |
| `mttr_seconds` | Computed as `resolved_at - opened_at` |
| `mttd_seconds` | Detection-to-diagnosis latency |

---

## 13. Custom Resource Definitions (CRDs)

### `PatchRequest` CRD

**File**: [`k8s/crd-patchrequest.yaml`](file:///home/rahul/K8s/k8s/crd-patchrequest.yaml)

A `PatchRequest` is the primary human-approval interface. It represents a single AI-generated diagnosis + proposed fix waiting for SRE approval.

**Key fields**:
```yaml
spec:
  incidentId: INC-2026-0726-7256
  targetDeployment: crash-demo
  targetNamespace: production
  errorState: CrashLoopBackOff
  rootCause: "Pod fails because /app/missing.py does not exist in the image"
  severity: high
  confidence: medium
  seenCount: 3             # Incremented each time same crash is detected
  llmSummary: "Add missing file or fix entrypoint"
  autoRestartSafe: false
  likelyRecurring: true
  proposedPatch:
    kind: Deployment
    spec_patch:
      name: app
      image: myapp:v2.1    # The LLM's proposed fix
status:
  approvalState: Pending   # Pending → Approved → Applied
  approvedBy: sre-rahul
  appliedAt: "2026-07-26T12:15:00"
```

**Lifecycle**:
```
Controller creates PR → approvalState: Pending
SRE patches status   → approvalState: Approved
Controller detects   → on_patchrequest_approved fires → patch applied
Controller patches   → approvalState: Applied
```

### `IncidentRecord` CRD

**File**: [`k8s/crd-incidentrecord.yaml`](file:///home/rahul/K8s/k8s/crd-incidentrecord.yaml)

A lightweight, cluster-scoped CRD used for quick CLI access and timeline tracking.

```bash
kubectl get inc -A                          # List all incidents
kubectl describe inc inc-2026-0726-7256     # Full incident details
```

---

## 14. RBAC — What the Controller Is Allowed to Do

**File**: [`k8s/rbac.yaml`](file:///home/rahul/K8s/k8s/rbac.yaml)

### Controller ServiceAccount Permissions

| Resource | Verbs | Reason |
|----------|-------|--------|
| `pods` | `get`, `list`, `watch` | Watch for pod status changes |
| `pods/log` | `get` | Fetch crash logs |
| `events` | `get`, `list` | Fetch pod events for LLM context |
| `deployments` | `get`, `list`, `patch` | Apply LLM-proposed patches |
| `patchrequests` | `*` (full CRUD) | Create, read, update PatchRequest CRDs |
| `incidentrecords` | `*` (full CRUD) | Create, read IncidentRecord CRDs |

### What It Is Explicitly **NOT** Allowed To Do

- Patch `Secrets` — cannot exfiltrate credentials
- Patch `Roles` or `ClusterRoles` — cannot escalate its own permissions
- Delete `Pods` — no disruptive actions without human approval
- Access namespaces outside `WATCH_NAMESPACES` — scoped by NetworkPolicy

---

## 15. End-to-End Flow Diagram

```
T+0s    Pod crashes (FileNotFoundError, exit code 1)

T+1s    Kubelet sets: state.waiting.reason = "CrashLoopBackOff"

T+1s    K8s API server detects .status.containerStatuses changed
        → Kopf Watch stream receives event
        → fires on_pod_status_change()

T+1s    detect_error_state() → "CrashLoopBackOff"
        [Layer 1] should_trigger() → count=1/3 → NOT YET

T+3s    Pod crashes again → counter: 2/3 → NOT YET

T+5s    Pod crashes again → counter: 3/3 → ✅ THRESHOLD CROSSED
        asyncio.create_task(_run_diagnosis_pipeline())

T+5s    _fetch_pod_logs()   → 200 lines of crash output
        _fetch_pod_events() → 20 K8s events for this pod
        preprocess_logs()   → 200 lines → ~40 signal lines
        make_fingerprint()  → "a3f2bc91d4e7..."

T+5s    [Layer 2] check_fingerprint_cache() → NOT in cache → pass
        [Layer 3] has_open_patchrequest()   → no existing PR → pass

T+5s    incident_id = "INC-2026-0726-7256"
        call_llm() → tries Vertex AI (gcloud token present)

T+9s    Vertex AI responds in 3.9s with JSON diagnosis:
          root_cause: "FileNotFoundError: /app/missing.py"
          severity: high
          suggested_fix: "Fix the ENTRYPOINT or add the missing file"
          auto_restart_safe: false
          likely_recurring: true

T+9s    _create_patch_request_crd()  → PatchRequest created in K8s
        _create_incident_record_crd() → IncidentRecord created
        register_fingerprint()        → 4h TTL (likely_recurring=true)
        Log: "🔴 HIGH CrashLoopBackOff — production/crash-demo"

T+??    SRE reviews:
          kubectl get pr -n production
          kubectl describe pr crash-demo-pr-...

T+??    SRE approves:
          kubectl patch pr crash-demo-pr-... \
            --type merge \
            -p '{"status":{"approvalState":"Approved","approvedBy":"sre-rahul"}}'

T+??    on_patchrequest_approved fires
        Validates patch kind ✅ (Deployment is in whitelist)
        apps_api.patch_namespaced_deployment(...) ← patch applied
        Updates PR status → "Applied"

T+??    New pod starts with fixed image/config
        Pod is healthy → clear_dampening(pod_uid)

DONE    Incident closed, MTTR calculated and persisted
```

---

## Summary Table — All Controllers & Sub-Systems

| Component | Trigger / Role | File |
|-----------|----------------|------|
| `catch_up_scan` | Startup: scan for missed incidents | `main.py` |
| `on_pod_status_change` | Pod `containerStatuses` field change → 3-layer dedup → LLM | `main.py` |
| `on_patchrequest_approved` | PatchRequest `approvalState` → "Approved" → apply patch | `main.py` |
| `should_trigger` | L1 dedup: time-windowed event counter per pod | `dedup.py` |
| `check_fingerprint_cache` | L2 dedup: SHA-256 log hash → in-memory cache | `dedup.py` |
| `has_open_patchrequest` | L3 dedup: K8s API check for existing active PR | `dedup.py` |
| `increment_seen_count` | Bump seenCount + escalation at milestones 10/25/50 | `dedup.py` |
| `preprocess_logs` | Strip noise, extract error signal, truncate to token budget | `log_preprocessor.py` |
| `detect_error_state` | Map containerStatuses → canonical error string | `log_preprocessor.py` |
| `make_fingerprint` | SHA-256(error_state + cleaned_logs)[:16] | `log_preprocessor.py` |
| `call_llm` | Multi-provider: Vertex AI → Gemini API → Ollama | `llm_client.py` |
| `parse_llm_response` | 5-strategy JSON parser (never crashes) | `llm_client.py` |
| `Incident` | State machine: Open → Investigating → Resolved → Closed | `incident.py` |

---

*Document generated: 2026-07-26 | K8s AI SRE Agent v1.0*
