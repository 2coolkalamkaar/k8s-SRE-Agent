# Phase 2 Execution Log: Node Domain

*Status: built and verified live on a real cluster. Corresponds to Phase 2 in the multi-domain
expansion roadmap ([`multi_domain_expansion_plan.md`](./multi_domain_expansion_plan.md)), building
on the foundation from [Phase 1](./phase1_action_plan_execution.md).*

## What this phase was for, in plain words

Phase 1 built the plumbing (a pluggable "action plan" system) but only ever ran one kind of
action: patching a Deployment. This phase adds the agent's first genuinely new capability —
noticing when a whole cluster **Node** (not a pod) is unhealthy, and responding to it. A node
problem (running low on disk, out of memory, not responding) isn't something you fix by patching
a Deployment — you either stop new work from landing there (`cordon`) or move existing work off it
(`drain`).

**Following an explicit instruction going into this phase: `drain_node` is built to only ever
dry-run — it never actually evicts a pod for real, even after a human approves it.** This is a
deliberate safety choice, not a limitation that slipped in — draining is the single riskiest
action in the whole system (it can disrupt many running workloads at once), so it stays
observe-only until it's been proven reliable and someone deliberately turns it on.

## What was actually built

1. **Node condition detection** (`controller/log_preprocessor.py`) — a `detect_node_condition()`
   function that reads a Node's health conditions (`NotReady`, `DiskPressure`, `MemoryPressure`,
   `PIDPressure`, `NetworkUnavailable`) and returns a plain label, mirroring how pod crash
   detection already works.

2. **Two new actions** (`controller/actions.py`):
   - `cordon_node` — stops new pods being scheduled on a bad node. Low risk, fully able to
     execute for real once approved.
   - `drain_node` — moves existing pods off a node. Its `dry_run()` is fully real (it really
     calls Kubernetes' Eviction API in dry-run mode, so it gives an honest answer about whether a
     drain would actually work). Its `execute()` is hard-coded to refuse, on purpose.

3. **A separate Analyst/Fixer pair for node problems** (`controller/llm_client.py`) —
   `NodeAnalystAgent` and `NodeFixerAgent`. These don't read pod logs; they look at node capacity,
   conditions, and how many pods (and which namespaces) are actually running on the affected
   node — that "how many pods would this affect" number is calculated in code (not asked of the
   LLM) and shown to the approver as `blastRadius` before they click approve.

4. **A new watcher** (`controller/main.py`) — watches every Node's health conditions
   cluster-wide. Filters out the kubelet's routine ~40-second heartbeat updates (which touch the
   same field but don't mean anything changed) so it only reacts to genuine condition changes.

5. **CRD additions** (`k8s/crd-patchrequest.yaml`) — `targetNode` and `blastRadius` fields, plus
   node problem types added to the existing error-state list.

6. **RBAC** (`k8s/rbac.yaml`) — read access to Nodes, and the specific permissions needed to
   cordon a node and dry-run an eviction (see the RCA doc below for why this required more
   investigation than expected).

## Where node incidents "live"

A Node isn't inside any namespace, so its `PatchRequest`/`IncidentRecord` objects are filed under
the `monitoring` namespace (configurable via `CLUSTER_INCIDENT_NAMESPACE`) rather than wherever
the affected pods happen to run.

## A scope decision carried over from Phase 1's pattern

Just like Phase 1 deliberately left the pod/deployment executor untouched until a second action
type genuinely needed the plugin system, this phase deliberately **does not hook node incidents
into the automatic health-observation loop** (`outcome_checker.py`). A cordoned node doesn't
"recover" the way a restarted pod does — there's nothing to poll for "is it healthy now." Node
`PatchRequest`s stay in the `Applied` state until a human closes them, which matches how an SRE
would actually work a hardware issue (often ends in a ticket to replace the machine, not an
automatic all-clear).

## How this was verified (not just "looks right")

This is where it mattered most that `kind` "nodes" are just Docker containers, not real hardware —
there's no way to make one genuinely run low on disk. Instead, the exact condition a real disk
problem would report was written directly onto a live Node object
(`kubectl patch node ... --subresource=status`), which exercises the *identical* code path a real
event would trigger — the controller can't tell the difference between a synthetic and a real
condition change, only whether the JSON says `DiskPressure: True`.

Full live run, three attempts (two surfaced real bugs — see the RCA doc):

1. Simulated `DiskPressure=True` on a real cluster node
2. Watcher correctly detected it and filtered out the heartbeat noise
3. `NodeAnalystAgent` correctly diagnosed disk pressure as the root cause
4. `NodeFixerAgent` proposed `drain_node`, with the affected pod list computed server-side (not
   trusted from the LLM's output)
5. Validator dry-ran the real eviction API call and approved it
6. `PatchRequest` created with `domain: node`, `targetNode` set, and
   `blastRadius: "4 pod(s) across 2 namespace(s)"` correctly populated
7. Approved the fix — confirmed the executor genuinely refused to drain for real
   (`drain_node execution is disabled (dry-run only) — no pods were evicted`), the PatchRequest
   correctly moved to `Failed` rather than being silently stuck or falsely marked successful, and
   **zero pods were actually evicted** (checked directly — all 4 pods still `Running` on the node
   afterward)
8. Re-ran the existing pod/deployment OOM demo afterward to confirm no regression — clean
   PatchRequest created, same as before this phase's changes

## What's next

Cluster domain (Piece 1/6 in the roadmap) and App domain (Prometheus-driven) remain. Enabling
`drain_node` for real execution is intentionally left as a separate, deliberate future decision —
not part of this phase.
