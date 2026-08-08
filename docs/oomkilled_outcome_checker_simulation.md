# 🧪 E2E Simulation: OOMKilled Incident + Outcome Checker Verification

**Incident Type:** OOMKilled (Memory Limit Exceeded)  
**Target:** `order-service` — production, 2 replicas (healthy, nginx:alpine)  
**Observation Window:** 30 seconds (test mode)  
**Goal:** Verify the full pipeline from incident detection → LLM diagnosis → Human Approval → Outcome Checker → Closed state

---

## 📋 Pre-Flight Checklist

Run these before you start to confirm everything is green:

```bash
# 1. Controller is running
kubectl get pods -n monitoring -l app=sre-controller

# 2. order-service is healthy (2/2 replicas)
kubectl get deployment order-service -n production

# 3. No stale PatchRequests for order-service
kubectl get pr -n production -l target-deployment=order-service

# 4. Confirm outcome_checker.py is loaded in the pod
kubectl exec -n monitoring deploy/sre-controller -- ls /app/controller/outcome_checker.py

# 5. Tail logs (open this in a SEPARATE terminal — keep it running throughout)
kubectl logs -n monitoring -l app=sre-controller -f
```

**Expected:** Controller running 1/1, order-service 2/2, no existing PRs for it.

---

## 🔴 PHASE 1 — Inject the OOMKilled Incident

We patch `order-service` to have a memory limit so low (4Mi) that the nginx container instantly OOMKills. OOMKilled bypasses the 3-event dampening window and triggers immediately.

```bash
# Step 1: Set a tiny memory limit to cause OOMKilled
kubectl patch deployment order-service -n production \
  --type=merge \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"order-service","resources":{"limits":{"memory":"4Mi"},"requests":{"memory":"2Mi"}}}]}}}}'
```

```bash
# Step 2: Watch pods — you'll see OOMKilled status appear in seconds
kubectl get pods -n production -w
```

**Expected pods output (within 10-30 seconds):**
```
order-service-XXXX-YYYY   0/1   OOMKilled   1   30s
order-service-XXXX-YYYY   0/1   OOMKilled   2   45s
```

> [!NOTE]
> OOMKilled is in the `IMMEDIATE_TRIGGER_STATES` set in `dedup.py` — it skips the 3-crash dampening window and triggers the LLM pipeline on the first occurrence.

---

## 🔍 PHASE 2 — Watch the AI Agent Detect & Diagnose

In your **log-tail terminal**, look for this sequence:

```bash
# Step 3: Confirm detection logs appear
kubectl logs -n monitoring -l app=sre-controller --tail=40
```

**What to look for in logs:**

```log
[dedup-L1] OOMKilled — immediate trigger for <pod-uid>
[INC-2026-XXXX-YYYY] Open → Investigating
🔴 [CRITICAL] OOMKilled — production/order-service
   Root Cause: Container exceeded its memory limit of 4Mi...
   Suggested Fix: Increase memory limits to at least 128Mi...
   PatchRequest: kubectl get pr order-service-pr-... -n production
```

```bash
# Step 4: Verify the PatchRequest CRD was created
kubectl get pr -n production
```

**Expected:**
```
NAME                          DEPLOYMENT     ERROR       SEVERITY   STATUS    SEEN   AGE
order-service-pr-2026-...     order-service  OOMKilled   critical   Pending   1      30s
```

```bash
# Step 5: Read the full AI diagnosis
kubectl get pr -n production -l target-deployment=order-service -o yaml
```

Look for `spec.rootCause`, `spec.llmSummary`, `spec.proposedPatch`, and `spec.confidence`.

---

## ✅ PHASE 3 — SRE Human Approval (Human-in-the-Loop)

You are the SRE engineer. Review the AI's diagnosis and make the call.

```bash
# Step 6: Get the exact PR name
PR_NAME=$(kubectl get pr -n production -l target-deployment=order-service -o jsonpath='{.items[0].metadata.name}')
echo "PatchRequest: $PR_NAME"
```

```bash
# Step 7: Approve the patch (simulate SRE approval)
kubectl patch pr $PR_NAME -n production \
  --subresource=status \
  --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"rahul"}}'
```

**Expected log output immediately after approval:**
```log
[pr-name] PatchRequest approved by rahul — applying patch to production/order-service
[pr-name] Rollout restart triggered for production/order-service
```

```bash
# Step 8: Verify the PR transitioned to Applied with observationStartTime
kubectl get pr $PR_NAME -n production -o jsonpath='{.status}' | python3 -m json.tool
```

**Expected status:**
```json
{
  "approvalState": "Applied",
  "appliedAt": "2026-...",
  "observationStartTime": "2026-...",
  "approvedBy": "rahul"
}
```

> [!IMPORTANT]
> The `observationStartTime` field being set is the trigger for the outcome checker. Without it the timer will skip this PR.

---

## ⏱️ PHASE 4 — Outcome Checker Monitoring (30-second window)

The outcome checker `@kopf.timer` fires every **30 seconds**. Once the PR is in `Applied` state, it:
1. Reads `observationStartTime`
2. Checks all pods for CrashLoopBackOff / OOMKilled / Error states
3. If healthy AND 30 seconds have elapsed → marks `Closed` ✅
4. If pod crashes again within window → triggers `kubectl rollout undo` + marks `Failed` 🔴

```bash
# Step 9: Watch the outcome checker working in real-time
watch -n 5 "kubectl get pr $PR_NAME -n production -o jsonpath='{.status}' | python3 -m json.tool"
```

**Watch the controller logs simultaneously:**
```bash
kubectl logs -n monitoring -l app=sre-controller --tail=10
```

**Look for one of these outcome logs (within ~60 seconds of approval):**

**SUCCESS path:**
```log
[outcome] ✅ order-service-pr-...: deployment healthy for 32s — marking CLOSED
```

**ROLLBACK path (if pod crashes again):**
```log
[outcome] 🔴 order-service-pr-...: pod crashed again after patch — triggering rollback!
```

---

## 🎯 PHASE 5 — Verify Final State

```bash
# Step 10: Check the final PR status
kubectl get pr $PR_NAME -n production -o yaml
```

### ✅ Success Path — Expected final status:
```yaml
status:
  approvalState: Closed
  approvedBy: rahul
  appliedAt: "2026-08-08T..."
  observationStartTime: "2026-08-08T..."
  workedOutcome: true
  mttrSeconds: 45   # Time from observationStart to resolution
```

### 🔴 Rollback Path — Expected final status:
```yaml
status:
  approvalState: Failed
  workedOutcome: false
```

```bash
# Step 11: Check Prometheus metrics for this incident
kubectl port-forward -n monitoring svc/sre-controller 9090:9090 &
sleep 2
curl -s http://localhost:9090/metrics | grep -E "sre_agent_mttr|sre_agent_patch_outcomes"
```

**Expected metrics:**
```
sre_agent_patch_outcomes_total{outcome="success"} 1
sre_agent_mttr_seconds_sum 45.0
```

---

## 🔄 PHASE 6 — Test the Rollback Path (Optional)

To force the rollback path, re-break the deployment immediately after approval before the 30s window closes:

```bash
# After approving, QUICKLY patch it back to tiny memory to force another OOMKill
kubectl patch deployment order-service -n production \
  --type=merge \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"order-service","resources":{"limits":{"memory":"4Mi"}}}]}}}}'
```

**Expected:** The outcome checker detects the pod crash and rolls back:
```log
[outcome] 🔴 order-service-pr-...: pod crashed again after patch — triggering rollback! Reason: Pod order-service-XXXX is in OOMKilled
```

---

## 🧹 PHASE 7 — Cleanup After Test

Restore the order-service to its healthy state:

```bash
# Restore normal memory limits
kubectl patch deployment order-service -n production \
  --type=merge \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"order-service","resources":{"limits":{"cpu":"100m","memory":"128Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}}]}}}}'

# Verify it comes back healthy (should show 2/2)
kubectl get pods -n production -l app=order-service -w

# Clean up the test PatchRequest
kubectl delete pr $PR_NAME -n production
```

---

## 📊 Grafana Dashboard Checks

After the simulation, check these dashboards at `http://localhost:3000`:

| Dashboard | What to look for |
|---|---|
| **SRE Overview** | `sre_agent_incidents_total` — counter incremented |
| **SRE Overview** | `sre_agent_patch_outcomes_total{outcome="success"}` — shows 1 |
| **SRE Overview** | `sre_agent_mttr_seconds` — histogram with time to resolve |
| **Tempo Traces** | Search by service `sre-controller` — full LLM call span visible |

```bash
# Port-forward Grafana if not already running
kubectl port-forward -n observability svc/grafana 3000:3000
```

---

## 🗂️ Quick Command Reference Card

| Step | Command |
|---|---|
| **Inject OOMKill** | `kubectl patch deployment order-service -n production --type=merge -p '{"spec":{"template":{"spec":{"containers":[{"name":"order-service","resources":{"limits":{"memory":"4Mi"}}}]}}}}'` |
| **Watch pods** | `kubectl get pods -n production -w` |
| **Watch controller logs** | `kubectl logs -n monitoring -l app=sre-controller -f` |
| **Get PR name** | `PR_NAME=$(kubectl get pr -n production -l target-deployment=order-service -o jsonpath='{.items[0].metadata.name}')` |
| **Approve PR** | `kubectl patch pr $PR_NAME -n production --subresource=status --type=merge -p '{"status":{"approvalState":"Approved","approvedBy":"rahul"}}'` |
| **Watch PR status** | `watch -n 5 "kubectl get pr $PR_NAME -n production -o jsonpath='{.status}' \| python3 -m json.tool"` |
| **Check metrics** | `curl -s http://localhost:9090/metrics \| grep sre_agent` |
| **Cleanup** | `kubectl patch deployment order-service -n production --type=merge -p '{"spec":{"template":{"spec":{"containers":[{"name":"order-service","resources":{"limits":{"cpu":"100m","memory":"128Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}}]}}}}'` |

---

## 🔑 What We're Validating (Full Checklist)

| # | Component | What to check | Expected |
|---|---|---|---|
| 1 | **Dedup L1** | OOMKilled skips dampening | Triggers immediately, no 3-crash wait |
| 2 | **LLM Diagnosis** | AI identifies memory limit as root cause | `rootCause` mentions memory limit |
| 3 | **PatchRequest CRD** | Created with correct fields | `status.approvalState: Pending` |
| 4 | **Human Approval** | SRE can approve via kubectl | `status.approvalState: Approved` |
| 5 | **Patch Applied** | Controller runs rollout restart | `status.approvalState: Applied` + `observationStartTime` set |
| 6 | **Outcome Checker** | Timer polls every 30s | Logs appear showing health checks |
| 7 | **Closed state** | Success path closes the PR | `workedOutcome: true`, `mttrSeconds` populated |
| 8 | **MTTR metric** | Prometheus records it | `sre_agent_mttr_seconds` histogram updated |
| 9 | **Rollback** | Failure path triggers undo | `kubectl rollout undo` fires, `workedOutcome: false` |
