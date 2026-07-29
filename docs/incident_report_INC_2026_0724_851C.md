# 🚨 SRE Incident Report — INC-2026-0724-851C

## Incident: `payment-gateway` OOMKilled → CrashLoopBackOff
**Severity**: HIGH  
**Environment**: Kubernetes Kind Cluster — `sre-agent-cluster` (`production` namespace)  
**Status**: ✅ RESOLVED  
**MTTR**: ~14 hours (between automated detection at 11:10 UTC Jul 24 and manual remediation at 11:51 UTC Jul 26)

---

## 📋 Table of Contents
1. [Incident Timeline](#1-incident-timeline)
2. [Root Cause Analysis](#2-root-cause-analysis)
3. [What the Agent Detected](#3-what-the-agent-detected)
4. [Log Data Sent to Ollama](#4-log-data-sent-to-ollama)
5. [Ollama Processing & Response](#5-ollama-processing--response)
6. [Custom Resources Created](#6-custom-resources-created)
7. [Remediation Executed](#7-remediation-executed)
8. [Debug Command Reference — Ollama, PostgreSQL & Controller](#8-debug-command-reference)

---

## 1. Incident Timeline

| Timestamp (UTC)       | Event                                                                                          |
|-----------------------|-----------------------------------------------------------------------------------------------|
| `2026-07-24 11:02:37` | `payment-gateway` deployment patched with `memory.limit=4Mi` → new pod created               |
| `2026-07-24 11:03:04` | Pod enters `OOMKilled` then `CrashLoopBackOff` (exit code 128 / OOM)                         |
| `2026-07-24 11:10:12` | **Agent Detection**: OOMKilled bypasses dampening Layer 1 → Ollama invoked immediately        |
| `2026-07-24 11:10:12` | Logs + K8s events sent to `ollama-service.ai-infra.svc.cluster.local:11434/api/generate`     |
| `2026-07-24 ~11:13:30`| **Ollama responds** after ~196s model inference on CPU                                        |
| `2026-07-24 11:16:38` | **`PatchRequest` CRD created**: `payment-gateway-pr-2026-0724-851c` (Status: `Pending`)       |
| `2026-07-24 11:16:38` | **`IncidentRecord` CRD created**: `inc-2026-0724-851c` (State: `Investigating`)               |
| `2026-07-24 11:16:38` | Second incident `INC-2026-0724-534B` raised (CrashLoopBackOff fingerprint differs from OOM)  |
| `2026-07-24→Jul 26`   | Agent dedup Layer 3 fires on every new crash, increments `seenCount` on open `PatchRequest`  |
| `2026-07-26 11:50:37` | **SRE Remediation**: memory limit raised from `4Mi → 256Mi`                                  |
| `2026-07-26 11:51:12` | New pod `payment-gateway-57f47cb68-hpblw` becomes **1/1 Running** (0 restarts)               |

---

## 2. Root Cause Analysis

### Upstream Cause
During a routine memory tuning experiment, the `payment-gateway` Deployment was patched with an aggressively low memory limit:

```bash
# What caused the incident:
kubectl patch deployment payment-gateway -n production \
  --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"4Mi"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"4Mi"}
  ]'
```

### Why It Failed
The `payment-gateway` container is a `python:3.11-alpine` process that on startup:
1. Imports Python runtime (~30MB baseline)
2. Executes `bytearray(128 * 1024 * 1024)` — a 128MB memory allocation for simulating payment batch processing

With a 4Mi hard limit, the Linux OOM killer immediately sent `SIGKILL` (exit code 137 → exit code 128 in containerd).

### The Crash Loop
Once OOMKilled, Kubernetes attempted a restart via `kubelet`. Each restart hit the same 128MB allocation within milliseconds and was killed again. The kubelet backoff escalated:
```
5s → 10s → 20s → 40s → 80s → 160s → 300s (max)
```
Over 55+ restart attempts across 2 days.

---

## 3. What the Agent Detected

### The Kopf Watch Handler Triggered
```text
@kopf.on.field("pods", field="status.containerStatuses")
Handler: on_pod_status_change
```

The `sre-controller` monitors `status.containerStatuses` across all pods in `production`. When the pod status changed, the handler received this container state:
```json
{
  "name": "payment-gateway",
  "state": {
    "waiting": {
      "reason": "CrashLoopBackOff",
      "message": "back-off 5m0s restarting failed container=payment-gateway"
    }
  },
  "lastState": {
    "terminated": {
      "exitCode": 128,
      "reason": "OOMKilled",
      "finishedAt": "2026-07-24T11:03:04Z",
      "containerID": "containerd://1338a5918204c148939f7d2e328ad23128b9e03e9eff7830c2c7eb4494e85d67"
    }
  },
  "restartCount": 4
}
```

### Error State Classification
The `log_preprocessor.detect_error_state()` function matched:
```python
# OOMKilled matched because lastState.terminated.reason == "OOMKilled"
# This bypasses Layer 1 dampening entirely:
# "OOMKilled events bypass dampening and trigger immediately"
error_state = "OOMKilled"
```

### 3-Layer Deduplication
1. **Layer 1 (Dampening)** — BYPASSED: OOMKilled is in the `bypass_list`, so no event count threshold required.
2. **Layer 2 (Fingerprint Cache)** — PASSED: No existing SHA-256 fingerprint for `c87e47a7155b6cca` in in-memory cache.
3. **Layer 3 (K8s API Check)** — PASSED: No active `PatchRequest` with label `target-deployment=payment-gateway` exists.
→ **All 3 layers passed. Ollama invocation triggered.**

---

## 4. Log Data Sent to Ollama

### What the Controller Fetched
The controller fetched both current and previous container logs plus K8s events:
```bash
kubectl logs payment-gateway-dc9888659-ng62m -n production --previous
kubectl logs payment-gateway-dc9888659-ng62m -n production
kubectl get events -n production \
  --field-selector involvedObject.name=payment-gateway-dc9888659-ng62m
```

### Raw Container Logs (Before Preprocessing)
```text
[INFO] Payment Gateway starting up...
[INFO] Allocating memory buffer for payment processing batch...
Killed
```

### Kubernetes Events Collected
```
REASON   MESSAGE
Pulled   Container image "python:3.11-alpine" already present on machine
Created  Created container payment-gateway
Started  Started container payment-gateway
OOMKilling  Memory cgroup out of memory: Killed process
BackOff  Back-off restarting failed container payment-gateway
```

### After `log_preprocessor.preprocess_logs()` Cleaning
The preprocessor:
- Stripped timestamps and ANSI codes
- Removed noisy health check HTTP 200 lines
- Preserved exit-code lines, FATAL/ERROR messages, and OOM stack traces
- Applied 4,000-character budget cap

**Cleaned log excerpt sent to Ollama:**
```text
[INFO] Payment Gateway starting up...
[INFO] Allocating memory buffer for payment processing batch...
Killed

K8s Events:
OOMKilling: Memory cgroup out of memory: Killed process
BackOff: Back-off restarting failed container payment-gateway
```

**Fingerprint generated** (SHA-256 of cleaned log + error_state):
```
c87e47a7155b6cca
```

### Full Prompt Sent to Ollama
```json
{
  "model": "deepseek-coder:6.7b-instruct",
  "stream": false,
  "prompt": "You are a Kubernetes SRE expert...\n\n## Context\nNamespace: production\nDeployment: payment-gateway\nError State: OOMKilled\nPod: payment-gateway-dc9888659-ng62m\nContainer Exit Code: 128\n\n## Pod Logs (Cleaned)\n[INFO] Payment Gateway starting up...\n[INFO] Allocating memory buffer for payment processing batch...\nKilled\n\n## K8s Events\nOOMKilling: Memory cgroup out of memory: Killed process\nBackOff: Back-off restarting failed container payment-gateway\n\nAnalyse the above and return ONLY valid JSON..."
}
```

---

## 5. Ollama Processing & Response

### Inference Metadata
| Property          | Value                                      |
|-------------------|--------------------------------------------|
| **Model**         | `deepseek-coder:6.7b-instruct`             |
| **Node**          | `sre-agent-cluster-worker2` (Tainted AI infra) |
| **Endpoint**      | `http://ollama-service.ai-infra.svc.cluster.local:11434/api/generate` |
| **Semaphore**     | Acquired slot 1/3 (concurrency gate)       |
| **Request Time**  | `2026-07-24T11:10:12Z`                     |
| **Response Time** | `~2026-07-24T11:13:30Z`                    |
| **Inference Duration** | **196.7 seconds** (CPU-only cold inference) |
| **HTTP Status**   | `200 OK`                                   |

### Raw Ollama JSON Response
```json
{
  "root_cause": "The pod 'payment-gateway-dc9888659-ng62m' in the production namespace has been killed due to Out of Memory (OOM) error.",
  "suggested_fix": "Increase the memory limit for this pod. Currently, it is set at 128Mi which might not be enough for your application.",
  "severity": "high",
  "error_state": "OOMKilled",
  "estimated_impact": "This pod will fail to start if memory limit is not increased.",
  "confidence_boost": "high",
  "likely_recurring": false,
  "auto_restart_safe": false,
  "config_suggestions": ["MEMORY_LIMIT=512Mi"],
  "matches_past_incident": null
}
```

### Agent State Transition
```text
[INC-2026-0724-851C] Open → Investigating
```
This transition was logged by `controller/states.py` when Ollama returned a valid diagnosis.

---

## 6. Custom Resources Created

### PatchRequest CRD (`payment-gateway-pr-2026-0724-851c`)
```yaml
apiVersion: sre.yourdomain.io/v1alpha1
kind: PatchRequest
metadata:
  name: payment-gateway-pr-2026-0724-851c
  namespace: production
  labels:
    incident-id: INC-2026-0724-851C
    target-deployment: payment-gateway
  creationTimestamp: "2026-07-24T11:16:38Z"
spec:
  incidentId: INC-2026-0724-851C
  errorState: OOMKilled
  severity: high
  confidence: high
  targetDeployment: payment-gateway
  targetNamespace: production
  rootCause: "The pod 'payment-gateway-dc9888659-ng62m' has been killed due to OOM."
  llmSummary: "Increase the memory limit for this pod. Currently set at 128Mi."
  humanNote: "This pod will fail to start if memory limit is not increased."
  likelyRecurring: false
  autoRestartSafe: false
  seenCount: 1
  llmDiagnosis:
    root_cause: "OOM kill due to insufficient memory limit"
    severity: high
    confidence_boost: high
    config_suggestions:
      - MEMORY_LIMIT=512Mi
    auto_restart_safe: false
```

### IncidentRecord CRD (`inc-2026-0724-851c`)
```yaml
apiVersion: sre.yourdomain.io/v1alpha1
kind: IncidentRecord
metadata:
  name: inc-2026-0724-851c
  labels:
    deployment: payment-gateway
    error-state: OOMKilled
    fingerprint: c87e47a7155b6cca
  creationTimestamp: "2026-07-24T11:16:38Z"
spec:
  incidentId: INC-2026-0724-851C
  errorFingerprint: c87e47a7155b6cca
  errorState: OOMKilled
  targetDeployment: payment-gateway
  targetNamespace: production
  recurrenceCount: 1
  state: Investigating
  rootCause: "OOM kill — memory limit 4Mi insufficient for 128MB application workload"
```

### Dedup Kicks In: `seenCount` Tracking
On every subsequent crash (55+ over 2 days), Layer 3 detected the active open PatchRequest:
```text
[dedup-L3] production/payment-gateway: active PR exists (payment-gateway-pr-2026-0724-851c) — incrementing seenCount
```
This prevented Ollama from being called on every restart, saving significant CPU load.

---

## 7. Remediation Executed

### SRE Manual Approval & Fix
```bash
# Step 1: SRE reviews PatchRequest diagnosis
kubectl get pr payment-gateway-pr-2026-0724-851c -n production -o yaml

# Step 2: SRE manually applies the fix (increase memory limits)
kubectl patch deployment payment-gateway -n production \
  --type='json' -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"256Mi"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"128Mi"}
  ]'
# OUTPUT: deployment.apps/payment-gateway patched

# Step 3: Verify new pod is healthy
kubectl get pods -n production -l app=payment-gateway
```

### Verification Output
```
NAME                              READY   STATUS    RESTARTS   AGE
payment-gateway-57f47cb68-hpblw   1/1     Running   0          22s
```

**Incident Resolved at `2026-07-26T11:51:12Z`** — 0 restarts, clean startup.

---

## 8. Debug Command Reference

### Controller Debugging

```bash
# View live controller logs
kubectl logs -n monitoring deployment/sre-controller -f

# View last 100 lines
kubectl logs -n monitoring deployment/sre-controller --tail=100

# Filter for specific incident
kubectl logs -n monitoring deployment/sre-controller | grep "INC-2026-0724-851C"

# Check controller pod health
kubectl get pod -n monitoring -l app=sre-controller
kubectl describe pod -n monitoring -l app=sre-controller

# Check liveness probe status
kubectl get pod -n monitoring sre-controller-<pod-id> -o jsonpath='{.status.containerStatuses[0].ready}'

# Check all PatchRequests and IncidentRecords
kubectl get pr -A
kubectl get inc -A

# Inspect a specific PatchRequest
kubectl get pr payment-gateway-pr-2026-0724-851c -n production -o yaml

# Inspect IncidentRecord
kubectl get inc inc-2026-0724-851c -o yaml
```

---

### Ollama Pod Debugging

```bash
# Check Ollama pod status
kubectl get pod -n ai-infra -l app=ollama

# View Ollama startup and model loading logs
kubectl logs -n ai-infra ollama-0 -f

# View last 50 Ollama logs
kubectl logs -n ai-infra ollama-0 --tail=50

# Check Ollama resource usage (CPU/Memory on ai-infra node)
kubectl top pod -n ai-infra ollama-0

# Exec into Ollama pod and check loaded models
kubectl exec -it -n ai-infra ollama-0 -- ollama list

# Test Ollama is responding inside the cluster
kubectl exec -it -n ai-infra ollama-0 -- curl -s \
  http://localhost:11434/api/tags | python3 -m json.tool

# Manually send a test request to Ollama from inside the cluster
kubectl exec -it -n monitoring deployment/sre-controller -- \
  curl -s -X POST \
  http://ollama-service.ai-infra.svc.cluster.local:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-coder:6.7b-instruct","prompt":"Say OK","stream":false}' \
  | python3 -m json.tool

# Check Ollama service and endpoint
kubectl get svc -n ai-infra ollama-service
kubectl get endpoints -n ai-infra ollama-service

# Check OOM on Ollama pod (if it was killed by the node OOM killer)
kubectl describe pod -n ai-infra ollama-0 | grep -A5 "OOMKilled\|Limits\|Requests"

# View Ollama node resource stats
kubectl describe node sre-agent-cluster-worker2 | grep -A15 "Allocated resources"
```

---

### PostgreSQL Pod Debugging

```bash
# Check PostgreSQL pod status
kubectl get pod -n monitoring -l app=postgres

# View PostgreSQL startup logs
kubectl logs -n monitoring postgres-0 --tail=50

# Live tail PostgreSQL logs
kubectl logs -n monitoring postgres-0 -f

# Exec into PostgreSQL pod
kubectl exec -it -n monitoring postgres-0 -- psql -U sre_user -d sre_agent

# Inside psql — check tables
\dt

# List all incident records in database
SELECT incident_id, error_state, deployment, state, created_at 
FROM incidents 
ORDER BY created_at DESC 
LIMIT 10;

# Check incident count by error type
SELECT error_state, COUNT(*) FROM incidents GROUP BY error_state;

# Check PostgreSQL resource usage
kubectl top pod -n monitoring postgres-0

# Check PVC (persistent volume) for PostgreSQL
kubectl get pvc -n monitoring

# Check disk usage inside PostgreSQL pod
kubectl exec -it -n monitoring postgres-0 -- df -h /var/lib/postgresql/data

# Describe PostgreSQL pod (check restarts, OOMKilled, etc.)
kubectl describe pod -n monitoring postgres-0
```

---

### Production Namespace Debugging

```bash
# Check all pod statuses in production
kubectl get pods -n production -o wide

# Get restart counts across all pods
kubectl get pods -n production \
  -o custom-columns='NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount,STATUS:.status.phase'

# Check pod events for a specific pod
kubectl get events -n production \
  --field-selector involvedObject.name=<pod-name> \
  --sort-by=.lastTimestamp

# View previous container logs (before last crash)
kubectl logs <pod-name> -n production --previous

# Describe pod for full status and events
kubectl describe pod <pod-name> -n production

# Top-level resource usage for production namespace
kubectl top pods -n production

# Check deployment rollout history
kubectl rollout history deployment/payment-gateway -n production

# Rollback to previous deployment revision
kubectl rollout undo deployment/payment-gateway -n production
```
