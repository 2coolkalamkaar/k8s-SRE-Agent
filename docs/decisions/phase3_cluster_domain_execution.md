# Phase 3 Execution Log: Cluster (ResourceQuota) Domain

*Status: built and verified live. Corresponds to Phase 3 in the multi-domain expansion roadmap
([`multi_domain_expansion_plan.md`](./multi_domain_expansion_plan.md)), following
[Phase 1](./phase1_action_plan_execution.md) and [Phase 2](./phase2_node_domain_execution.md).*

## What this phase was for, in plain words

Phase 2 taught the agent to notice when a whole machine (a Node) is unhealthy. This phase teaches
it to notice a different kind of non-pod problem: a **namespace running out of room** — hitting
its `ResourceQuota` limit on pod count, CPU, memory, or anything else a team has capped. This is a
much lower-stakes domain than nodes: the only available fix is raising a specific limit, which
only affects *future* scheduling decisions and never touches anything already running.

## What was actually built

1. **Quota-pressure detection** (`controller/log_preprocessor.py`) — `detect_quota_pressure()`
   compares a `ResourceQuota`'s `used` against its `hard` limits for every tracked resource
   (correctly handling Kubernetes' mixed quantity formats — `500m`, `2Gi`, plain counts — by
   converting everything to a common base unit before comparing). Returns
   `ResourceQuotaExceeded` if any resource is at/over its limit, or
   `ResourceQuotaNearLimit` as an early warning at 90%+, letting the agent get ahead of a
   problem instead of only reacting after something has already started failing to schedule.

2. **One new action** (`controller/actions.py`) — `patch_resourcequota`, which raises the
   specific limit(s) that are actually constrained. Unlike the node domain, there's no
   higher-risk second action here (no equivalent of "drain") — a quota patch is inherently low
   blast-radius, so it's built with full execute capability from the start, no dry-run-only gate
   needed.

3. **A third domain-specific Analyst/Fixer pair** (`controller/llm_client.py`) —
   `ClusterAnalystAgent` and `ClusterFixerAgent`. The Fixer is explicitly instructed to only
   touch the specific resource key(s) that are actually near/at their limit, leaving the rest of
   the quota untouched — verified this worked correctly in the live test below.

4. **A new watcher** (`controller/main.py`) — watches every `ResourceQuota` object's status
   across the watched namespaces, with the same "only react to a genuine change, not every status
   tick" filtering pattern used for nodes.

5. **CRD/RBAC additions** — `targetQuota` field (a `ResourceQuota`, unlike a Node, lives inside a
   real namespace, so — unlike node incidents — its `PatchRequest` files under that same
   namespace, no shared "cluster incidents" namespace needed), two new `errorState` enum values,
   and `resourcequotas` read/patch permissions.

## How this was verified (not just "looks right")

Unlike the Node domain (which needed synthetic conditions because `kind` nodes aren't real
hardware), this domain is fully real and organic in any cluster, `kind` included — a
`ResourceQuota` is just a regular Kubernetes object with real enforcement. No simulation needed:

1. Created a real `ResourceQuota` in the `production` namespace (`pods: "3"`, `requests.cpu: "500m"`)
2. Deployed 3 real pods, deliberately hitting the pod-count limit exactly (3/3 = 100%)
3. Watcher detected `ResourceQuotaExceeded` correctly
4. `ClusterAnalystAgent` correctly identified pod-count exhaustion as the cause
5. `ClusterFixerAgent` proposed raising only `pods` (from 3 to 10) — correctly left
   `requests.cpu` untouched even though it was also part of the same quota, exactly as instructed
6. Validator dry-ran the real quota-patch API call and approved it
7. `PatchRequest` created with `domain: cluster`, `targetQuota: bench-quota`, and a correctly
   worded `blastRadius` ("1 namespace — future scheduling only, no running workloads affected")
8. Approved it — confirmed the quota's real hard limit actually changed on the cluster
   (`pods: 3/10` afterward, checked directly via `kubectl get resourcequota`)
9. Re-ran the pod/deployment OOM demo afterward — clean PatchRequest, no regression

This was the cleanest phase so far: **no bugs found** during live testing. The pattern
established in Phase 2 (typed action, domain-specific Analyst/Fixer, server-computed action
parameters rather than trusting the LLM to echo them back correctly) held up directly, and the
RBAC lesson from Phase 2 (kopf needs `patch` on anything it watches) was applied proactively this
time instead of being rediscovered.

**Scope note:** the Node-domain code paths from Phase 2 were not independently re-tested live
this phase — only new functions were added alongside them (nothing existing was modified), so the
regression risk is low, but this is a scope decision made under time constraints, not a claim that
it was re-verified.

## What's next

App domain (Prometheus-alert-driven) is the last domain in the original roadmap, followed by
enabling `drain_node` for real execution (a separate, deliberate decision) and the audit log
(deferred to last, per an explicit earlier decision).
