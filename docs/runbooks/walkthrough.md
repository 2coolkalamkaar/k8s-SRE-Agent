# K8s AI SRE Agent — Kopf Operator Walkthrough

## What Was Built

A production-grade, air-gapped Kubernetes AI SRE Agent — a Kopf-based Python operator that autonomously detects pod failures, runs LLM diagnosis via in-cluster Ollama, and creates human-in-the-loop `PatchRequest` CRDs for SRE approval.

---

## Project File Map

```
/home/rahul/K8s/
├── controller/
│   ├── __init__.py
│   ├── main.py            ← Kopf handlers (startup, pod watch, patch executor)
│   ├── states.py          ← State Pattern: Open→Investigating→Resolved→Closed
│   ├── incident.py        ← Incident domain object with serialisation
│   ├── log_preprocessor.py← Log cleaning, fingerprinting, error state detection
│   ├── dedup.py           ← 3-layer dedup (dampening, cache, K8s API check)
│   └── ollama_client.py   ← Async Ollama client with 5-layer JSON parser
├── tests/
│   ├── conftest.py
│   ├── test_states.py     ← 19 state machine tests (all passing)
│   └── test_preprocessor.py← 13 preprocessor tests (all passing)
├── k8s/
│   ├── crd-patchrequest.yaml    ← PatchRequest CRD (short name: pr)
│   ├── crd-incidentrecord.yaml  ← IncidentRecord CRD (short name: inc)
│   ├── rbac.yaml                ← Two SAs: observer (read) + executor (patch)
│   └── controller-deployment.yaml
├── Dockerfile
└── requirements.txt
```

---

## Architecture

```
K8s Event (pod status change)
         │
         ▼
 ┌──────────────────────────────────────────────────────┐
 │  Kopf Handler: on_pod_status_change                  │
 │                                                      │
 │  Layer 1: Event Dampening                            │
 │     ├── OOMKilled → trigger immediately              │
 │     └── Others → require 3 events in 5 min          │
 │                                                      │
 │  Fetch pod logs + K8s events                         │
 │  Preprocess logs (strip noise, fingerprint)          │
 │                                                      │
 │  Layer 2: Fingerprint Cache (1h TTL)                 │
 │     └── Duplicate? → increment seenCount on PR       │
 │                                                      │
 │  Layer 3: Active PatchRequest Check (K8s API)        │
 │     └── PR exists? → increment seenCount             │
 │                                                      │
 │  ✓ All 3 layers passed → call Ollama                 │
 │       deepseek-coder:6.7b-instruct                   │
 │       Memory-augmented prompt (top-3 past incidents) │
 │       asyncio.Semaphore(3) — max concurrent calls    │
 │                                                      │
 │  Parse JSON (5-layer fallback, never raises)         │
 │  Create PatchRequest CRD (status: Pending)           │
 │  Create IncidentRecord CRD                           │
 │  Register fingerprint in cache                       │
 └──────────────────────────────────────────────────────┘
         │
         ▼
 SRE approves via: kubectl patch pr <name> -n production
         │           --type=merge -p '{"status":{"approvalState":"Approved","approvedBy":"rahul"}}'
         ▼
 ┌──────────────────────────────────────────────────────┐
 │  Kopf Handler: on_patchrequest_approved              │
 │                                                      │
 │  Validate patch kind whitelist                       │
 │  (Deployment | StatefulSet | ConfigMap only)         │
 │                                                      │
 │  Apply proposedPatch to Deployment                   │
 │  OR do rollout restart (if autoRestartSafe=true)     │
 │                                                      │
 │  Update PR status → Applied                          │
 └──────────────────────────────────────────────────────┘
```

---

## State Machine

```
Open ──────────────────► Investigating
                             │      ▲
                             │      │ patch failed
                    approved │      │ (reopen)
                             ▼      │
                           Resolved ┘
                             │
                  rca valid  │  (worked=True + 30-char RCA)
                             ▼
                           Closed  ← terminal
```

Every transition is validated. Invalid transitions raise `InvalidTransitionError`. RCA validation is transactional — all conditions must pass atomically.

---

## Test Results

```
32 passed, 0 warnings in 0.07s
```

| Suite | Tests | Coverage |
|---|---|---|
| `test_states.py` | 19 | All 4 states, all valid + invalid transitions, MTTR, serialization |
| `test_preprocessor.py` | 13 | Log cleaning, dedup, fingerprinting, all 5 error state types |

---

## Live Cluster Status

```bash
kubectl get nodes
# NAME                              STATUS   ROLES
# sre-agent-cluster-control-plane   Ready    control-plane
# sre-agent-cluster-worker          Ready    (App pods)
# sre-agent-cluster-worker2         Ready    (Ollama)

kubectl get pods -n monitoring
# NAME                              READY   STATUS    
# sre-controller-cc78bf75b-fwq29    1/1     Running   ← Our operator
# postgres-0                        1/1     Running

kubectl get crd | grep sre
# incidentrecords.sre.yourdomain.io
# patchrequests.sre.yourdomain.io
```

---

## How to Trigger a Live E2E Test

```bash
# 1. Force a crash by deleting the auth-service secret (if not already missing)
kubectl delete secret auth-service-secret -n production --ignore-not-found

# 2. Restart auth-service to trigger CrashLoopBackOff
kubectl rollout restart deployment/auth-service -n production

# 3. Watch the controller detect and diagnose (~3 events in 5 min window)
kubectl logs -n monitoring deployment/sre-controller -f

# 4. Check for created PatchRequest
kubectl get pr -n production -w

# 5. Approve the patch (human-in-the-loop step)
kubectl patch pr <name> -n production --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"rahul@company.com"}}'

# 6. Watch the executor apply the patch
kubectl get pr -n production -w
# status should transition to: Applied
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| `asyncio.Semaphore(3)` on Ollama | Prevents CPU starvation on 16GB host |
| 5-layer JSON parser | Ollama occasionally produces malformed JSON; never crashes the pipeline |
| Layer 3 dedup uses K8s API | Survives controller pod restarts (in-memory layers 1 & 2 reset) |
| Two separate ServiceAccounts | `observer-sa` has no write access to Deployments; `executor-sa` has no access to Secrets |
| `PYTHONPATH=/app` in Dockerfile | Kopf loads main.py as a script, not via `-m`, so the package root must be explicit |
| State machine with `InvalidTransitionError` | Prevents auto-close without RCA, prevents re-resolving without SRE approval |

---

## Next Steps

1. **Outcome checker** — 15-minute background task to set `incident.worked = True/False` and trigger `close()` or `reopen_investigation()`
2. **PostgreSQL persistence** — Replace in-memory fingerprint cache with `asyncpg` writes to `incidents` table
3. **Slack notifier** — Replace logger stub with real Slack webhook for `PatchRequest` alerts
4. **Memory retrieval** — Query past `IncidentRecord` CRDs to inject few-shot context into Ollama prompts
5. **Host resource monitor** — Prometheus scrape of node CPU/RAM with alert thresholds
