# Phase 2 RCA: Issues Found and Fixed

*Companion to [`phase2_node_domain_execution.md`](./phase2_node_domain_execution.md).*

Unlike Phase 1 (which went clean), this phase surfaced three real issues during live testing —
two straightforward bugs, and one genuine pre-existing architectural gap that's worth
understanding on its own. All three were found by actually running the code against a live
cluster, not by reading it.

---

## Issue 1: Forgot that kopf needs write access to anything it watches

**What happened:** the very first deploy crashed on startup with:

```
APIForbiddenError: nodes "sre-agent-cluster-worker2" is forbidden: User
"system:serviceaccount:monitoring:sre-observer-sa" cannot patch resource "nodes"
```

**Why:** the RBAC rule for reading Nodes was written as read-only (`get, list, watch`) — which
seemed correct, since the controller only ever *reads* node health data, it never changes a
node's real state through this permission. But `kopf` (the framework this whole controller is
built on) writes its own internal bookkeeping annotations onto every single object it watches,
regardless of what the handler code does with it. The existing `pods` permission rule already had
a comment explaining exactly this ("patch needed by Kopf to write status annotations") — it just
didn't occur to apply the same reasoning to the brand-new `nodes` rule until the error made it
obvious.

**Fix:** added `patch` to the Node RBAC rule, with a comment explaining why (so the next person
adding a new watched resource type doesn't hit the same surprise).

---

## Issue 2: A pre-existing gap — the "separate least-privilege executor" was never actually wired up

**What happened:** approving a fix and trying to dry-run a pod eviction failed with a permission
error, even after fixing Issue 1. Digging into *why* revealed something bigger than a missing
permission line.

**The actual finding:** this project's RBAC file has always defined two separate identities —
`sre-observer-sa` (read-only, plus writing `PatchRequest`/`IncidentRecord` objects) and
`sre-executor-sa` (the only one meant to be allowed to actually apply a fix). The comments
describe this as a deliberate security boundary, and the README advertises it as one too. But
checking `controller-deployment.yaml` shows the entire controller — every single handler,
including the part that's supposed to be the restricted "executor" — runs under **one** identity,
`sre-observer-sa`. `sre-executor-sa` is defined in `rbac.yaml` but is never attached to any
running pod anywhere in the project. It's dead configuration that looks like a real security
boundary but has never actually been enforced at runtime.

**This wasn't introduced by this phase's work** — it's been true since the project's very first
commit. It only surfaced now because this phase was the first time a new permission
(`pods/eviction: create`) was added to the *executor* role without realizing that role isn't
actually in use.

**Fix applied for now:** granted the node/eviction permissions the *actually-running* identity
(`sre-observer-sa`) needs, with a clearly written comment in `rbac.yaml` explaining the situation
so it doesn't look like an accident to the next reader.

**Decision: not pursuing this.** The initial instinct was to flag this as a follow-up worth a
dedicated pass (separating "the part that reads and plans" from "the part that's allowed to act"
into two real identities). On reflection, that's solving for the wrong threat model here. The
actual safety boundary in this system isn't which internal identity a piece of code runs as — it's
that **every real change requires a human to explicitly approve a `PatchRequest` first.** Nothing
the agent proposes ever touches the cluster without that approval step. A stricter RBAC split
between "observer" and "executor" would only matter if the diagnosis code itself got compromised
or buggy enough to bypass its own approval logic and call the Kubernetes API directly — a much
narrower and less likely problem than what the human-approval gate already covers. Splitting the
identities would add real engineering effort for a safety property the system's actual design
doesn't depend on. Leaving `sre-executor-sa` as unused, documented dead config is fine as-is; the
`rbac.yaml` comment on the observer role's node/eviction permissions stays, so this isn't
mistaken for an oversight later.

---

## Issue 3: A real bug in the code written this phase — wrong argument type

**What happened:** after fixing the permission issue, the dry-run eviction call still failed, now
with a different error:

```
dryRun: Unsupported value: ["['All']"]: supported values: "All"
```

**Why:** `DrainNodeAction.dry_run()` called the Kubernetes eviction API with
`dry_run=["All"]` — a list containing the string `"All"`. Every other dry-run call in this
codebase (the Deployment patch validator, for example) passes `dry_run="All"` as a plain string.
The eviction endpoint's client method expects the same plain-string shape, not a list — passing a
list caused it to get stringified into the literal text `['All']` when building the request, which
the API server correctly rejected as not a recognized value.

**Fix:** changed `dry_run=["All"]` to `dry_run="All"`, matching the pattern already used
everywhere else in the codebase. One-line fix once the actual cause was visible in the error
message.

---

## Bottom line

Three real issues, each caught by actually running the code against a live cluster rather than
assuming it would work:
- One RBAC oversight (fixed, and documented so it doesn't repeat for the next watched resource)
- One pre-existing architectural gap discovered as a side effect (worked around pragmatically,
  documented clearly as a follow-up rather than silently patched over)
- One genuine coding bug (fixed, one line)

After all three fixes, the full node-domain flow was re-verified end-to-end and confirmed
correct, including confirming the `drain_node` safety gate actually holds — no pods were evicted
even after a human approved the fix.
