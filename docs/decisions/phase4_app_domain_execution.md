# Phase 4 Execution Log: App Domain

*Status: built and verified live. Corresponds to Phase 4 in the multi-domain expansion roadmap
([`multi_domain_expansion_plan.md`](./multi_domain_expansion_plan.md)), the last of the three new
domains following [Phase 1](./phase1_action_plan_execution.md),
[Phase 2](./phase2_node_domain_execution.md), and
[Phase 3](./phase3_cluster_domain_execution.md).*

## What this phase was for, in plain words

Every domain so far is detected by watching a Kubernetes object change — a pod's status, a
node's conditions, a quota's usage. This phase covers something structurally different: an app
that's **behaving badly without crashing**. A slow endpoint, an elevated error rate — nothing a
Kubernetes object's status field will ever show, because from Kubernetes' point of view, the pod
is perfectly healthy. The only place this shows up is in metrics, which means detection for this
domain can't watch anything — it has to **listen** for Prometheus to tell it something's wrong.

## What was actually built

1. **A webhook receiver** (`controller/webhook_receiver.py`) — a small HTTP server running
   inside the same process as the rest of the controller, listening on port 8090 for the standard
   Prometheus Alertmanager webhook format. When Alertmanager POSTs a firing alert, this turns it
   into the same internal signal every other domain already produces, then hands it off — this is
   the only domain where "detection" means running a server instead of watching an object.

2. **A domain-specific Analyst/Fixer pair** (`controller/llm_client.py`) — `AppAnalystAgent` and
   `AppFixerAgent`. There are no crash logs to reason from here, only the alert's own labels and
   description — verified live that the Analyst can still produce a sensible root cause purely
   from an alert description (see below).

3. **Two new actions** (`controller/actions.py`) — `rollout_restart` (restart all pods, no
   downtime) and `scale_deployment` (change replica count). The Fixer is deliberately restricted
   to only these two blunt, well-understood moves rather than anything requiring it to guess at
   application code — and, importantly, it's explicitly allowed to propose **neither** if the
   root cause doesn't look fixable by either one (verified this actually happens, not just an
   unused escape hatch — see below).

4. **Error-state normalization** (`controller/main.py`) — Alertmanager alert names are
   arbitrary/user-defined (unlike node conditions or quota resource keys, which come from a fixed
   Kubernetes vocabulary), so they're mapped to a small set of canonical buckets
   (`HighErrorRate`, `HighLatency`, `AppDegraded`) rather than passed through verbatim — keeps the
   CRD's `errorState` field meaningful as a fixed enum across every domain instead of opening it
   to arbitrary text.

## A payoff from the earlier phases' design

This is the one domain that gets **automatic health-observation and rollback for free**. Node and
cluster fixes had to skip the existing `outcome_checker` loop (no Deployment to health-check), but
an app-domain fix targets a real Deployment in a real namespace — exactly what that loop was
built for. So unlike the node/cluster executor paths, `_execute_app_actions` sets
`observationStartTime` and lets the untouched, already-proven `outcome_checker.py` take over from
there, with zero new observation code. Verified this actually closes the loop, not just plumbed
correctly (see below).

## How this was verified (not just "looks right")

No simulation needed here either — Alertmanager's webhook format is well-defined and stable, so
sending a hand-built payload in that exact shape exercises the receiver identically to a real
Alertmanager instance:

1. Deployed a real, healthy 2-replica Deployment (`nginx`, no actual problem) — this app domain's
   entire point is that Kubernetes sees nothing wrong, so a genuinely healthy pod is the correct
   test target, not a crashing one
2. **First alert** — a downstream-dependency-style error alert. Result: Analyst correctly
   diagnosed an external dependency issue; Fixer correctly recognized neither restart nor scale
   would fix a downstream service being down, and returned no actions — an annotation-only
   `PatchRequest` was created instead of a guess. This is the guardrail working as designed, not
   a limitation.
3. **Second alert** — a memory-leak-style latency alert with a description implying steady memory
   growth and GC pressure. Result: Analyst correctly identified the memory-leak pattern from the
   description text alone (no logs available); Fixer proposed `rollout_restart`, matching exactly
   the scenario its prompt names as the right call for; Validator dry-ran and approved it
4. Approved the fix — confirmed a **real** rolling restart happened (new pod names/ages, checked
   directly)
5. Confirmed `observationStartTime` was set and the existing `outcome_checker` timer picked it up
   automatically — watched it run its 30s health-check ticks and correctly mark the incident
   `CLOSED` after the observation window, with **no new code written for this step**
6. Re-ran the pod/deployment OOM demo afterward to confirm zero regression

**No new bugs found this phase** — the patterns established in Phases 2 and 3 (typed actions,
domain-specific agents, server-computed action parameters, proactive RBAC/kopf-patch awareness)
held up directly, and this domain had less new surface area than the other two (no new RBAC
needed — it only ever touches Deployments, already covered).

## What's next

All four domains from the original roadmap (pod, node, cluster, app) are now built. Remaining:
enabling `drain_node` for real execution (a separate, deliberate decision, not part of this
phase), and the audit log (deferred to last per an explicit earlier decision — this is now the
last item on the original plan).
