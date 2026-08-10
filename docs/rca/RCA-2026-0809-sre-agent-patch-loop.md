# Root Cause Analysis — SRE Agent Patch Execution Loop

**Incident ID:** INC-2026-0809-COREDNS-LOOP  
**Date:** 2026-08-09  
**Severity:** Critical  
**Status:** Resolved  
**Author:** SRE Platform Team  
**System:** Autonomous SRE Agent (Kubernetes Controller)

---

## Executive Summary

During a controlled fault-injection exercise targeting the CoreDNS deployment in `kube-system`, the SRE Agent correctly diagnosed the root cause of the failure on every cycle but failed to actually remediate it. This caused an infinite loop of `CrashLoopBackOff → Detect → Diagnose → "Fix" → Restart with same broken args → Crash again`, generating 12+ PatchRequests over ~15 minutes. Three distinct bugs were identified and resolved.

---

## Timeline

| Time (UTC) | Event |
|---|---|
| `14:28:00` | WATCH_NAMESPACES updated to include `kube-system`; OUTCOME_OBSERVATION_WINDOW set to 60s |
| `14:29:38` | Fault injected: CoreDNS deployment patched to use `-conf /etc/coredns/wrongfile` |
| `14:30:24` | Controller detected `CrashLoopBackOff` on CoreDNS pods (dampening threshold crossed) |
| `14:30:32` | First PatchRequest `coredns-pr-2026-0809-5070` created (LLM correctly diagnosed wrong config path) |
| `14:31:37` | PR `5070` approved — controller "applied" patch (annotation bump only, args unchanged) |
| `14:31:58` | PR `5070` marked `Closed` (outcome checker saw old running pod still healthy) |
| `14:32:00` | Fingerprint cleared → new crashes generated fresh PRs — loop begins |
| `14:39:16` | User reported recurring CoreDNS incidents — investigation started |
| `14:40:08` | Root cause identified: deployment args still pointing to `wrongfile` |
| `14:40:30` | CoreDNS manually restored via `kubectl patch --type=json` |
| `14:41:02` | Bug fix 1 committed: patch executor rewritten to use full `proposedPatch` structure |
| `14:41:12` | Bug fix 2 committed: CRD enum updated, missing status fields added |
| `14:42:14` | Controller rebuilt and redeployed |
| `14:42:23` | Fault re-injected for end-to-end validation |
| `14:44:17` | Fixed controller applied correct patch — deployment args flipped to `Corefile` ✅ |
| `14:45:00` | Both CoreDNS pods `1/1 Running`; PR in `Applied` state with `observationStartTime` set ✅ |

---

## Bug 1 — Patch Executor Ignored LLM's Proposed Fix

### Component
`controller/main.py` — `on_patchrequest_approved()` handler (~line 599)

### Description
The patch executor read `proposed_patch.get("spec_patch", {})` to build the Kubernetes patch body. However, the LLM pipeline **never** produces a `spec_patch` key — it generates a fully-nested deployment spec fragment under `proposedPatch.spec.template.spec`. Because `spec_patch` was always `{}` (empty), the executor silently fell through to the `else` branch, which only stamped a `kubectl.kubernetes.io/restartedAt` annotation. The pod was restarted, not fixed.

### Root Cause
Mismatch between the key the executor expected (`spec_patch`) and the key the LLM actually produced (a full `spec → template → spec → containers` nested structure). No validation or warning was emitted, making the failure completely silent.

### Impact
- Every approved PatchRequest performed only a rollout restart, leaving the underlying misconfiguration intact
- CoreDNS pods restarted with the same broken `-conf /etc/coredns/wrongfile` argument on every cycle
- Outcome checker incorrectly marked PRs as `Closed` (a legacy running pod was still healthy), clearing the fingerprint and generating a new incident on the next crash

### Fix (diff)

```diff
-# Before — always empty, falls through to annotation-only restart
-spec_patch = proposed_patch.get("spec_patch", {})
-if spec_patch:
-    patch_body = {"spec": {"template": {"metadata": ..., "spec": {"containers": [spec_patch]}}}}
-else:
-    patch_body = {"spec": {"template": {"metadata": {"annotations": restart_annotation}}}}

+# After — reads full LLM-generated nested structure
+lp_tpl = proposed_patch.get("spec", {}).get("template", {})
+lp_pod_spec = lp_tpl.get("spec", {})
+if lp_pod_spec:
+    patch_body["spec"]["template"].update({k: v for k, v in lp_tpl.items() if k != "metadata"})
+elif proposed_patch.get("spec_patch"):
+    patch_body["spec"]["template"]["spec"] = {"containers": [proposed_patch["spec_patch"]]}
```

---

## Bug 2 — CRD Schema Rejected `Closed` and `Failed` Approval States

### Component
`k8s/crd-patchrequest.yaml` — `status.approvalState` field validation

### Description
The `PatchRequest` CRD's OpenAPI v3 schema defined `approvalState` with the enum `[Pending, Approved, Applied, Rejected]`. The `outcome_checker` transitions a successfully remediated incident to `Closed` and a rolled-back incident to `Failed`. Both states were missing, causing the Kubernetes API server to reject every status update from the outcome checker with HTTP 422 Unprocessable Entity.

Additionally, the status object lacked explicit field definitions for `observationStartTime`, `mttrSeconds`, and `workedOutcome`, and was missing `x-kubernetes-preserve-unknown-fields: true`, causing writes to those fields to also be rejected.

### Fix (diff)

```diff
 status:
   type: object
+  x-kubernetes-preserve-unknown-fields: true
   properties:
     approvalState:
       type: string
-      enum: [Pending, Approved, Applied, Rejected]
+      enum: [Pending, Approved, Applied, Rejected, Closed, Failed]
+    observationStartTime:
+      type: string
+    mttrSeconds:
+      type: integer
+    workedOutcome:
+      type: boolean
```

---

## Bug 3 — MTTR Calculation Skipped When `observationStartTime` Was Lost

### Component
`controller/outcome_checker.py` — `outcome_checker_timer()` (~line 163)

### Description
The outcome checker reads `status.observationStartTime` to compute elapsed time since the patch was applied. The patch executor writes both `appliedAt` and `observationStartTime` in a single status patch call. However, a subsequent Kopf internal status update could overwrite the status object via `application/merge-patch+json`, leaving `observationStartTime` absent while `appliedAt` survived.

When the timer fired on a PR missing `observationStartTime`, it logged a warning and returned early — MTTR was never computed and the incident was never closed.

### Fix (diff)

```diff
 if not observation_start:
-    logger.warning("[outcome] %s: Applied but no observationStartTime — skipping", name)
-    return
+    observation_start = body.get("status", {}).get("appliedAt")
+    if observation_start:
+        logger.info("[outcome] %s: using appliedAt as fallback, re-stamping observationStartTime", name)
+        await _fix_api.patch_namespaced_custom_object_status(
+            ..., body={"status": {"observationStartTime": observation_start}}
+        )
+    else:
+        logger.warning("[outcome] %s: no observationStartTime or appliedAt — skipping", name)
+        return
```

---

## Contributing Factors

1. **No end-to-end integration test for the patch executor** — unit tests mocked `proposed_patch` using the legacy `spec_patch` key, masking the schema mismatch between LLM output and executor input.

2. **Silent failure mode** — the executor did not log a warning when `spec_patch` was empty and it fell through to annotation-only restart. No alert, no metric, nothing.

3. **Outcome checker declared victory too early** — the health check passed because of a *pre-existing* healthy pod from the old ReplicaSet, not a newly fixed pod, causing premature `Closed` transitions while bad pods continued crashing.

4. **CRD schema not updated alongside controller code** — the `Closed` and `Failed` states and their associated status fields were implemented in the controller but never reflected in the CRD schema.

---

## Action Items

| # | Action | Priority | Status |
|---|--------|----------|--------|
| 1 | Add integration test: inject PR with real LLM-shaped `proposedPatch`, approve, verify deployment spec changes on cluster | P0 | Open |
| 2 | Emit `WARN` log + increment `patch_apply_noop_total` metric when executor falls through to annotation-only restart | P1 | Open |
| 3 | Scope outcome health check to pods from the **current ReplicaSet only** (filter by `pod-template-hash` label) | P1 | Open |
| 4 | Add post-close cooldown: suppress new PRs for a deployment for 5 min after a `Closed` transition to prevent fingerprint-cleared re-flooding | P2 | Open |
| 5 | Add CI schema validation step: assert CRD enum contains all `approvalState` values referenced in controller Python code | P1 | Open |

---

## Lessons Learned

- **Autonomous remediation requires full-loop tests.** Unit tests that mock individual components cannot catch interface mismatches between the LLM output schema and the executor's expected input structure.
- **Silent no-ops are more dangerous than loud failures.** A remediation step that silently degrades to a restart instead of applying a fix appears to succeed while leaving the system broken.
- **Health checks must be scoped to the change being validated.** Checking "is any pod in this deployment healthy" is insufficient — the check must confirm the *newly rolled-out pods* from the patched ReplicaSet are healthy.

---

*Document generated: 2026-08-09 | SRE Agent Platform v1.0*
