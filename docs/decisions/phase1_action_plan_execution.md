# Phase 1 Execution Log: Action-Plan Foundation

*Status: built and verified live. Corresponds to Phase 1 in the multi-domain expansion roadmap
([`multi_domain_expansion_plan.md`](./multi_domain_expansion_plan.md)).*

## What this phase was for, in plain words

Before this phase, the only thing the agent could ever do to fix something was "patch a
Deployment." That works for pod crashes, but it can't cordon a broken node or edit a cluster
resource quota — those aren't Deployment patches, they're different kinds of actions entirely.

This phase didn't add any new fixing capability yet. It built the **plumbing** that later phases
(node domain, cluster domain, app domain) will plug into — a way for the Fixer agent to say "do
these steps" instead of "apply this one patch," without breaking anything that already works.

## What was actually built

1. **`controller/actions.py` (new)** — a small plugin system. Every "kind of fix" (right now,
   just `patch_deployment`) is its own class with two functions: `dry_run()` ("would this work?")
   and `execute()` ("actually do it"). Adding a new kind of fix later (e.g. `cordon_node`) means
   writing one new class here — nothing else in the pipeline needs to change to support it.

2. **`controller/triage.py` (new)** — a sorting step that looks at where a problem signal came
   from and labels it with a "domain" (`pod`, `node`, `cluster`, or `app`). Right now every signal
   still comes from the same place (watching pods), so every incident is labeled `pod` — but the
   sorting logic is in place and ready for node/cluster/app watchers to plug into later.

3. **`controller/llm_client.py`** — the Fixer/Validator loop now builds its fix as a list of
   typed actions (currently always one: `patch_deployment`) and validates it through the new
   plugin system, instead of a hardcoded single-patch check.

4. **The `PatchRequest` CRD** (`k8s/crd-patchrequest.yaml`) — gained two new fields:
   `proposedActions` (the new action list) and `domain` (the triage label). The **old
   `proposedPatch` field is still there, completely unchanged**, so nothing downstream (the
   dashboard, the executor, RAG reuse) needed to change at all.

## Why the executor (the part that actually applies an approved fix) wasn't touched

This is a deliberate choice, not something skipped by accident. The executor's existing code —
the part that turns an approved `PatchRequest` into a real change on the cluster — already works
and is well-tested. Right now there's only one kind of action (`patch_deployment`), so routing it
through the new plugin system would add a layer of indirection with **zero new capability** and
some risk of a subtle bug in code that currently works fine. The executor gets rewired to use the
plugin system in the node-domain phase, when a second action type (`cordon_node`) actually needs
somewhere to plug in.

## How this was verified (not just "looks right")

Ran a full live incident through the updated pipeline on the real cluster, end to end:

1. Triggered a genuine OOM crash (`demo-apps/payment-gateway-oom.yaml`)
2. Confirmed the RAG cache correctly found and correctly **rejected** a stale match from an
   earlier session (its patch no longer applied to the current deployment shape) — this is the
   Validator's safety net doing its job, not a bug
3. Watched the full Analyst → Fixer → Validator pipeline run, this time going through the new
   `validate_actions()` path in `actions.py` instead of the old inline check
4. Confirmed the created `PatchRequest` had both the new fields populated correctly:
   - `domain: pod`
   - `proposedActions: [{"type": "patch_deployment", "params": {"patch": {...}}}]`
   - `proposedPatch` — byte-identical to what it would have been before this change
5. Approved the patch and confirmed the (untouched) executor still applied it correctly — pod
   went from `OOMKilled` to `Running` with the new memory limit

**Result: no regression.** The existing pod/deployment remediation flow behaves exactly as
before; the new plumbing is genuinely wired in (not just present in the code, but proven to run)
and ready for the next phase to build on.

## What's next

Per the roadmap, the next phases each add one new domain (starting with Node), which is when the
action-plugin system and the executor rewiring actually start paying off — each new domain is
"write one new action class," not "touch the core pipeline again."
