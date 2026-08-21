# Phase 1 RCA: Issues Found (and One Avoided On Purpose)

*Companion to [`phase1_action_plan_execution.md`](./phase1_action_plan_execution.md).*

Being straightforward about this: this build went cleanly. No bugs surfaced during live testing
— the pipeline worked correctly on the first end-to-end run. That's different from earlier work
in this project (the RAG feature-build and the benchmark rewrite both surfaced real bugs), and
it's worth saying plainly rather than manufacturing findings that don't exist.

There's one real gotcha worth documenting, because it's the kind of thing that silently breaks
things with no error message if you don't know to look for it — and one thing worth flagging as
a design tradeoff, not a bug.

---

## Gotcha: Kubernetes silently deletes fields you forget to declare in a CRD schema

**What would have happened if this wasn't caught upfront:** the plan was to add two new fields to
the `PatchRequest` object — `domain` and `proposedActions`. The natural (wrong) assumption is
"just start writing extra data into the object and it'll be there." That's not how Kubernetes
Custom Resource Definitions (CRDs) work by default.

**Why:** a CRD's schema is "structural" — meaning Kubernetes enforces `additionalProperties:
false` implicitly unless a field is explicitly listed (or the whole object is marked
`x-kubernetes-preserve-unknown-fields: true`). If code writes a field the schema doesn't know
about, the Kubernetes API server doesn't error — it just **silently strips the field out** before
saving. The write looks like it succeeded. Reading the object back afterward, the field is just
... gone. No error, no warning, nothing in the logs. This is a very easy way to spend an hour
debugging "why isn't my new field showing up" with zero clues pointing at the real cause.

**How this was avoided:** rather than finding this out the hard way, the two new fields
(`proposedActions`, `domain`) were added explicitly to `k8s/crd-patchrequest.yaml`'s schema
*before* writing any code that uses them. Verified directly afterward by reading the field back
off a real, live `PatchRequest` object post-incident — confirmed both fields were actually
present and correctly populated, not silently dropped.

**Where this matters for later phases:** any new field added to a CRD going forward (e.g. a
future `blastRadius` field for node/cluster incidents) needs to go through the same step —
declare it in the schema first, then write code that populates it. Skipping this step doesn't
cause a crash; it causes a much harder to notice "the data I wrote just isn't there."

---

## Design tradeoff, not a bug: the executor was deliberately left untouched

This isn't something that broke — it's a decision worth recording so it doesn't look like an
oversight later. The part of the code that actually applies an approved fix to the cluster (the
"executor") still uses its original, hardcoded single-patch logic, even though the new
action-plugin system (`controller/actions.py`) is fully built and capable of running it instead.

**Why not switch it over immediately:** there's currently only one kind of action
(`patch_deployment`). Routing already-working, already-tested code through a new layer of
indirection, when that new layer doesn't unlock any new capability yet, is pure risk with no
benefit — and this project has already seen (in the RAG and benchmark work) how an untested
refactor of working code can introduce a real bug. The safer call is to make that switch when a
second action type (`cordon_node`, in the node-domain phase) actually needs the plugin system to
work — at that point the switch is justified by real new functionality, and it gets tested against
that new functionality directly rather than tested in a vacuum.

---

## Bottom line

Zero regressions found in live testing. One real gotcha (CRD schema pruning) was designed around
proactively rather than debugged reactively. One deliberate scope decision (executor left as-is
for now) is documented here so it reads as a choice, not something forgotten.
