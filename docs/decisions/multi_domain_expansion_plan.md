# Implementation Plan: Scaling the Pipeline to App, Cluster, and Node-Level Errors

*Status: proposed, not yet implemented.*

## The big picture first

Right now the pipeline works like this: **a pod crashes → Analyst explains why → Fixer writes a
patch → Validator dry-runs the patch → done.** Everything assumes "the broken thing is a pod, and
the fix is a patch to its Deployment."

To handle app-level, cluster-level, and node-level problems too, we need to stop assuming that.
The core idea of this plan is: **add a sorting step at the front that figures out what KIND of
problem this is, and let the rest of the pipeline branch based on that answer.** Everything else
in this plan supports that one idea.

This document goes through 6 pieces. For each: what problem we're solving, the different ways we
could solve it, which one is recommended and why, and exactly what files change. Then a
step-by-step build order.

---

## Piece 1: Teaching the system to notice more than just pod crashes

**The problem:** Right now, the only thing that can trigger the pipeline is a pod's status
changing (like `CrashLoopBackOff`). If a whole Node goes unhealthy, or the cluster runs out of
resources, or an app is just running slow (not crashing), nothing notices.

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. Watch more Kubernetes objects directly** | Add watchers for `Node` (disk full, memory pressure, not-ready), `Event` (cluster-wide warnings), `PersistentVolumeClaim` (stuck/full disks) | Simple to add (same watching mechanism already used for pods), but only catches things Kubernetes itself already knows are broken |
| **B. Listen for Prometheus alerts** | Prometheus already tracks metrics like error rate, latency, memory usage. Set up alert rules, and have the controller receive a webhook when one fires | Catches "app is degraded but not crashed" problems that Kubernetes itself can't see (e.g. slow database queries) — but needs new alerting rules to be written and maintained |
| **C. Both A and B together** | Use direct K8s watching for cluster/node problems (they're structural, Kubernetes knows about them), and Prometheus alerts for app-level problems (they're behavioral, only metrics show them) | More moving parts, but each tool is used for what it's actually good at |

**Recommendation: Option C.** Structural problems (a node dying) and behavioral problems (an app
getting slow) are fundamentally different kinds of signals, and trying to force one detection
method to catch both would be awkward. Use the right tool for each.

**What changes:**
- `controller/main.py` — add two new `@kopf.on.field(...)` watchers: one for `nodes` (watching
  `status.conditions`), one for cluster-wide `events`
- New file `controller/webhook_receiver.py` — a small HTTP endpoint that receives Prometheus
  Alertmanager webhooks and turns them into the same internal "something is wrong" signal the pod
  watcher already produces
- `k8s/observability/prometheus.yaml` — add alert rules (e.g. "error rate > 5% for 5 minutes")

---

## Piece 2: A "sorting desk" that figures out what kind of problem this is

**The problem:** Once we detect more kinds of problems, the Analyst needs to know *what it's
looking at* before it can ask the right questions. Diagnosing "why did this pod crash" is a
totally different task from "why is this node unhealthy."

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. Simple rule-based sorting** | If the signal came from a Pod → domain=`pod`. If from a Node → domain=`node`. If from Prometheus → domain=`app`. If from a cluster-wide Event → domain=`cluster` | Fast, free (no LLM call), 100% predictable — but can't handle ambiguous cases (e.g. is a slow pod an app problem or a node problem?) |
| **B. Let the LLM decide** | Send the raw signal to a small "Triage Agent" that reads it and picks the domain | Handles ambiguity well, but costs an extra LLM call on every single incident, even obvious ones |
| **C. Rules first, LLM as tie-breaker** | Use rules for the 90% of obvious cases (which domain a signal comes from is usually obvious from where it came from). Only call an LLM when the signal is genuinely ambiguous | Best of both — fast and free for clear cases, smart for unclear ones |

**Recommendation: Option C.** We already know a lot for free — a signal from the Node watcher is
obviously domain=`node`. There's no reason to spend an LLM call figuring that out. Save the LLM
for genuinely unclear situations.

**What changes:**
- New file `controller/triage.py` — a function `classify_domain(signal) -> domain, resource_ref`
  that does the rule-based sorting, with a fallback LLM call for ambiguous cases
- `controller/main.py` — every detection path (pod, node, cluster, app) now calls
  `triage.classify_domain()` before deciding what to do next

---

## Piece 3: Teaching the Analyst to ask domain-appropriate questions

**The problem:** The Analyst's current prompt is literally "diagnose the pod failure" — it
doesn't know how to think about a node problem or a cluster-wide quota problem.

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. One prompt, extra context** | Keep one Analyst, but tell it the domain and swap in different background info (pod logs vs. node conditions vs. metrics) | Simple, one prompt to maintain — but the prompt gets long and generic trying to cover 4 domains reasonably |
| **B. Separate prompt per domain** | Write 4 focused prompts — one that's great at reading pod logs, one that's great at reading node health signals, etc. — and pick the right one based on the triage result | Each prompt can be sharp and specific — but 4 prompts to maintain instead of 1, and they can drift out of sync over time |

**Recommendation: Option B**, but built as templates sharing a common base
(severity/impact/JSON-output rules shared, domain-specific "what to look for" section swapped in)
— gets sharpness without full duplication.

**What changes:**
- `controller/llm_client.py` — `AnalystAgent.analyze()` gains a `domain` parameter and picks the
  matching prompt template
- New prompt templates for node/cluster/app domains alongside the existing pod one

---

## Piece 4: Letting the Fixer propose more than just "patch a Deployment"

**The problem:** This is the biggest structural change. The Fixer's whole output today assumes
"the fix is a JSON patch to a Deployment spec." But:
- Fixing a full disk on a node means cordoning it and evicting pods, not patching anything
- Fixing a cluster resource quota means editing a `ResourceQuota` object, a totally different shape
- Fixing an app-level slowness issue might mean scaling up replicas — a different kind of change again

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. Keep "patch" as the only concept, force everything into it** | Try to represent a node cordon as some kind of "patch" | Doesn't really work — cordoning isn't a patch, it's an imperative action. Would require ugly workarounds |
| **B. Add a generic "action plan" format** | Fixer outputs a list of typed steps, e.g. `[{"type": "patch_deployment", ...}]` or `[{"type": "cordon_node", "node": "worker-2"}]` or `[{"type": "scale_deployment", "replicas": 5}]`. Each action type has its own executor function | More upfront design work (need to define each action type), but scales cleanly — adding a new fixable problem later just means adding one new action type |

**Recommendation: Option B — it's the one change that actually unlocks everything else in this
plan.** Without it, nothing beyond "patch a Deployment" is possible no matter what else is built.

**What changes:**
- `controller/llm_client.py` — `FixerAgent.propose_fix()` now returns `{"actions": [...]}`
  instead of `{"patch": {...}}`; prompt changes to describe the available action types for the
  current domain
- CRD schema (`k8s/crd-patchrequest.yaml`) — `proposedPatch` field becomes `proposedActions` (a
  list), keeping the old field name working for a transition period so nothing breaks immediately
- New file `controller/actions.py` — defines each action type and what it means
  (`patch_deployment`, `cordon_node`, `drain_node`, `patch_resourcequota`, `scale_deployment`,
  `restart_daemonset`)

---

## Piece 5: Making the Validator and Executor understand the new action types

**The problem:** The Validator only knows how to dry-run a Deployment patch. The Executor (the
part that actually applies approved fixes) only knows how to apply a Deployment patch too. Both
need to grow to match Piece 4.

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. One big if/else for each action type** | `ValidatorAgent.validate()` and the executor both get a chain of `if action.type == "patch_deployment": ... elif action.type == "cordon_node": ...` | Quick to write, gets messy fast as action types grow |
| **B. Each action type is its own small class/function with a standard shape** | Every action type implements the same two functions: `dry_run(action) -> (ok, error)` and `execute(action) -> result`. The Validator/Executor just look up the right one and call it | Slightly more setup, but adding action #7 later doesn't require touching the Validator or Executor code at all — you just add the new file |

**Recommendation: Option B** — this is a standard "plugin" pattern and it's worth the small extra
setup, especially since node and cluster actions are coming next, and probably more later.

**What changes:**
- `controller/llm_client.py` — `ValidatorAgent.validate()` loops over the action list, calling
  each action's own `dry_run()`
- `controller/main.py`'s `on_patchrequest_approved` handler — loops over the action list, calling
  each action's own `execute()`
- `controller/actions.py` — each action type defined here (from Piece 4) implements both functions

---

## Piece 6: Making sure risky domains get more human scrutiny, not less

**The problem:** Today, every incident gets the same approval process regardless of how much
damage a bad patch could do. A wrong env-var patch affects one deployment. A wrong node drain can
take down everything scheduled on that node. Treating these the same is dangerous.

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. Leave approval the same for everything** | No change | Simplest, but risky — a confidently-wrong node action gets the same one-click approval as a low-risk env var fix |
| **B. Blast-radius-aware approval rules** | Pod/app-domain fixes: current one-click approval flow stays. Node/cluster-domain fixes: require a stated `blast_radius` estimate (e.g. "affects 12 other pods on this node") shown prominently, and never allow the RAG cache to auto-skip validation for these — always require the full agent pipeline, never a cached shortcut | A bit more code (blast-radius estimation logic), but matches how a real SRE team would want this to work — you don't want a "confidently wrong" node drain sailing through the same way a config typo fix does |

**Recommendation: Option B.**

**What changes:**
- `controller/incident.py` / CRD schema — add a `blastRadius` field (a short human-readable
  estimate) populated by the Analyst for node/cluster domains
- `controller/db.py`'s `find_similar_incident()` — skip the RAG cache entirely when
  `domain != "pod"` (always go through the full pipeline for higher-risk domains, at least
  initially, until confidence is built up in the pattern)
- Dashboard — surface `blastRadius` prominently for node/cluster incidents so a human approver
  sees it immediately

---

## Piece 7: A signed, tamper-evident audit log

**The problem:** As the agent starts touching higher-blast-radius things (nodes, cluster
resources — see Piece 6), "who approved this and why" needs to be trustworthy, not just a
free-text field. Checked the codebase for this directly: **there is no audit log today.** Every
"configmap" reference in the code is unrelated (RBAC permissions, the whitelist of resource kinds
a patch is allowed to touch) — not an audit trail. What actually exists is:
- `IncidentRecord` CRDs in Kubernetes (etcd) — regular mutable objects, editable/deletable by
  anyone with `kubectl` access, no tamper-evidence
- The `incidents` / `state_transitions` tables in Postgres — same story, plain mutable rows
- `approved_by` on a `PatchRequest` — just a free-text string field; nothing verifies the identity
  behind it, and nothing stops it from being edited after the fact

This is a real gap, especially once node/cluster-domain actions (higher blast radius) are in play.

**Possible solutions:**

| Option | How it works | Tradeoff |
|---|---|---|
| **A. Store audit entries in a ConfigMap** | Write each approval/action as an entry in a Kubernetes ConfigMap | Doesn't actually solve the problem — ConfigMaps are still just mutable, editable objects with a 1MB size cap, and have no built-in append-only or tamper-evidence mechanism. Not recommended. |
| **B. Append-only table in Postgres** | A new `audit_log` table that is only ever inserted into, never updated/deleted (enforced by a DB trigger or revoked `UPDATE`/`DELETE` grants) | Simple, reuses infrastructure already in place — but "append-only by convention/permissions" can still be bypassed by a superuser or a direct DB admin action |
| **C. Hash-chained append-only log** | Same as B, but each new entry also stores the hash of the previous entry (like a mini blockchain / git-commit style chain). Any edit to an old entry breaks the hash chain for everything after it, making tampering detectable even by a DB admin | Slightly more implementation work (hashing logic), but this is what makes it genuinely **tamper-evident** rather than just "hopefully nobody touches it" — and it's a compelling, concrete thing to demo (show breaking the chain live) |

**Recommendation: Option C.** Option A doesn't actually deliver on "tamper-evident" at all, and
Option B only prevents *accidental* edits, not deliberate ones. The hash-chaining in Option C is
what makes this a genuine security feature rather than "a log we promise not to edit."

**What changes:**
- New table `audit_log` in `db/schema.sql` / `k8s/postgres-statefulset.yaml`'s init script:
  columns for `entry_hash`, `prev_hash`, `incident_id`, `action_type`, `actor` (who approved),
  `reason`, `timestamp`, and the full action payload
- New file `controller/audit.py` — `record(entry) -> None` computes `entry_hash` from the entry's
  contents + `prev_hash`, appends the row; `verify_chain() -> (ok, broken_at)` walks the whole
  table and confirms every hash still matches, for demoing/checking integrity
- `controller/main.py` — call `audit.record(...)` at every point where a human approves a fix
  (`on_patchrequest_approved`) and every point where the agent auto-applies/rolls back a fix
  (`outcome_checker.py`)
- Dashboard — a simple "Audit Log" view showing the chain, with a "Verify Integrity" button that
  calls `verify_chain()` live (strong demo moment: shows a green checkmark, then if you
  manually edit a row in Postgres and click it again, it turns red and shows exactly where the
  chain broke)

---

## Step-by-step build order

Build this in the order below — each step is independently useful and testable before moving to
the next, so we're never stuck with half-finished work:

1. **Triage layer** (Piece 2) — build `classify_domain()`, wire it into the existing
   pod-detection path only for now. Nothing user-visible changes yet, but this is the foundation
   everything else needs.
2. **Action-plan contract** (Piece 4) — change the Fixer's output shape, update the
   Validator/Executor to the plugin pattern (Piece 5), but initially only implement one action
   type: `patch_deployment` (the thing already done today). This step is a pure refactor —
   behavior for existing pod/deployment incidents shouldn't change at all. Test thoroughly here
   before moving on, since everything downstream depends on this being solid.
3. **Domain-aware Analyst prompts** (Piece 3) — still only for the `pod` domain for now, just
   proving the domain-based prompt-switching mechanism works.
4. **Widen detection to Nodes** (Piece 1, option A half) — add the Node watcher, wire it through
   triage → Analyst (node prompt) → Fixer. Implement the `cordon_node` and `drain_node` action
   types.
5. **Blast-radius-aware approval** (Piece 6) — add this now that node-domain incidents exist,
   before letting node fixes run unsupervised.
6. **Widen detection to cluster-wide Events** — same pattern as step 4, add
   `patch_resourcequota` and similar action types as needed.
7. **App-level detection via Prometheus** (Piece 1, option B) — this is the most different from
   what exists today (webhook receiver, alert rules), so it's saved for last after the
   domain/action-plan machinery has been proven out on the more direct K8s-native domains.

---

## Summary: everything that changes

| File | What changes |
|---|---|
| `controller/main.py` | New watchers (Node, cluster Events), calls triage before dispatching, executor loops over actions instead of one patch |
| `controller/triage.py` *(new)* | Domain classification (rule-based + LLM fallback) |
| `controller/actions.py` *(new)* | Each action type's dry-run + execute logic |
| `controller/webhook_receiver.py` *(new)* | Receives Prometheus alerts for app-domain detection |
| `controller/llm_client.py` | Analyst gets domain-specific prompts; Fixer outputs an action list instead of one patch; Validator loops over actions |
| `controller/incident.py` | Adds `domain` and `blastRadius` fields |
| `controller/db.py` | RAG lookup skips cache for non-pod domains |
| `k8s/crd-patchrequest.yaml` | `proposedPatch` → `proposedActions` (list) |
| `k8s/observability/prometheus.yaml` | New alert rules for app-level detection |
| Dashboard | Shows domain + blast radius on each incident |

This is a genuinely large body of work — step 2 alone (the action-plan refactor) touches the core
of every agent. Treat steps 1-3 as one deliverable (foundation, no new user-facing capability yet,
but nothing should break), then each domain (node, cluster, app) as its own separate deliverable
after that.
