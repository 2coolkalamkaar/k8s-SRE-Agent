# Phase 5 Execution Log: Hash-Chained Audit Log

*Status: built and verified live, including a real tamper-detection demonstration.
Corresponds to Piece 7 / the final phase in the multi-domain expansion roadmap
([`multi_domain_expansion_plan.md`](./multi_domain_expansion_plan.md)), built last per an
explicit earlier decision (Scenario B — see that conversation's reasoning: building it against
the final, multi-domain action shape rather than the single-patch shape Phase 1 started with).*

## What this phase was for, in plain words

Checked the codebase for this at the very start of this whole effort (see the "Piece 7" section
of the roadmap doc): there was no audit trail. Every "configmap" reference in the code was
unrelated. What existed — `IncidentRecord` CRDs, the `incidents` table, a free-text `approved_by`
field — was all ordinary, editable data. Nothing was signed, nothing was append-only, nothing
would notice if a row got quietly changed after the fact.

This phase adds a real one: every human approval, and every automatic action the agent takes on
its own (a rollback, an auto-close), gets one row in a **hash-chained** log. Each row's hash is
computed from its own contents plus the row before it's hash — so editing any past row, even with
full database access, breaks the hash of every row after it. A function
(`verify_chain()`) walks the whole log and reports exactly where a break happened, if any.

## What was actually built

1. **`audit_log` table** (`db/schema.sql`, `k8s/postgres-statefulset.yaml`) — `entry_hash`,
   `prev_hash`, `incident_id`, `action_type`, `actor`, `reason`, `payload`, `recorded_at`. Never
   `UPDATE`d or `DELETE`d by the application, only ever `INSERT`ed into.

2. **`controller/audit.py`** — `record()` appends one entry (reading the current last row's hash
   and writing the new one inside a single locked transaction, so two concurrent writes can't
   both read the same `prev_hash` and silently fork the chain into two valid-looking branches).
   `verify_chain()` walks the whole table front to back and confirms every row's hash still
   matches what recomputing it from that row's own stored contents produces.

3. **Three wiring points**, all feeding the same log:
   - `controller/main.py`'s `on_patchrequest_approved` — logs `action_type="approval"`,
     `actor=<the human's email>`, **once, before branching by domain** — so this single insertion
     point covers pod, node, cluster, and app approvals uniformly, with no per-domain code needed.
   - `controller/outcome_checker.py`'s rollback path — logs `action_type="rollback"`,
     `actor="system"`.
   - `controller/outcome_checker.py`'s close path — logs `action_type="auto_close"`,
     `actor="system"`.

   The `actor` field is what makes the log tell a real story: "system" entries are the agent
   acting on its own (a fix failed and got rolled back with no human involved in that decision);
   named-human entries are the one thing every domain requires before anything is ever applied.

## How this was verified (not just "looks right")

1. **A real bug, found and fixed via live testing** — the very first approval failed with
   `invalid input for query argument $8: ... expected a datetime.date or datetime.datetime
   instance, got 'str'`. Same class of mistake made once already earlier in this project's history
   (passing an ISO string where asyncpg needs a real `datetime` object).
2. **A second, subtler issue caught while fixing the first** — simply switching to a real
   `datetime` object wasn't enough. Postgres returns timezone-*aware* datetimes for
   `TIMESTAMPTZ` columns, but the original code was building a timezone-*naive* one for hashing.
   `.isoformat()` produces a different string for the same instant depending on whether `tzinfo`
   is set (`...T18:03:41.812` vs `...T18:03:41.812+00:00`) — meaning `verify_chain()` would have
   reported every single legitimate entry as "broken," a false alarm on a security feature, which
   is about as bad an outcome as a real miss. Fixed by using one explicit `strftime` format
   (`_canonical_ts()`) at both write time and read-back time, independent of `tzinfo`.
3. **Verified all three action types actually record correctly** — approved a real fix (logged
   `approval` by the approving human), let it close successfully (logged `auto_close` by
   `system`), then separately approved a fix on a demo that's designed to always fail (logged
   `rollback` by `system`) — reading the resulting log back tells the real story of both
   incidents without needing to cross-reference anything else.
4. **The actual security property — tamper detection — was demonstrated, not assumed.** Ran
   `verify_chain()`: reported the chain intact. Directly edited one row's `actor` field in
   Postgres (simulating someone with full database access trying to quietly rewrite history).
   Ran `verify_chain()` again: **correctly identified the exact row that was altered**, even
   though the row after it was never touched — because that row's hash still referenced the
   *original* `prev_hash`, and the tampered row's own recomputed hash no longer matched what was
   stored. Restored the original value and re-verified clean, confirming the hash function is
   genuinely deterministic and reproducible, not just "happened to catch this one edit."

## Scope decisions made explicitly, not silently

- **Dashboard UI (the "Verify Integrity" button) was not built this phase.** The dashboard
  (`dashboard/app.py`) currently has zero Postgres connectivity — it only reads Kubernetes objects
  directly. Adding a database connection, a new endpoint, and a frontend section is real
  additional scope, and the security-critical part (the log actually being tamper-evident, proven
  above) mattered more to get right than a UI wrapper around it. For now, `verify_chain()` is
  callable directly (as demonstrated above) — a CLI/script interface, not a button. Worth a
  follow-up if a visual "Verify Integrity" moment is wanted for a demo.
- **All four domains were not individually re-tested against this phase's change.** The audit
  hook in `on_patchrequest_approved` sits at one insertion point *before* the code branches by
  domain — every domain's approval flows through the identical `audit.record()` call. Verified
  directly for the pod domain; the other three inherit the same code path rather than each having
  their own copy to independently break.
