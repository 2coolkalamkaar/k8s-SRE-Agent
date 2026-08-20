# RAG Semantic Cache — Reusing Past Fixes Instead of Re-Diagnosing

## What this is

Before this feature, every crash — even one the agent had already solved before —
triggered a fresh call to all 3 LLM agents (Analyst → Fixer → Validator). The only
duplicate-detection was an exact SHA-256 hash of the cleaned logs (`dedup.py` Layer 2),
which only catches byte-identical repeats. A slightly different pod name, timestamp,
or log ordering produces a different hash, so the same underlying bug gets re-diagnosed
from scratch every time.

This feature adds a semantic memory layer: past incidents are embedded into vectors and
stored in Postgres. When a new crash comes in, the controller checks whether it *means*
almost the same thing as something it already fixed successfully — if so, it reuses that
patch instead of calling the AI agents again.

Everything runs **locally, inside the cluster** — no external API, no per-call cost:
- **Embeddings**: [`fastembed`](https://github.com/qdrant/fastembed) running
  `BAAI/bge-small-en-v1.5` (384-dim), baked into the controller image at build time.
  CPU-only, no GPU, no network access needed at runtime.
- **Vector store**: [`pgvector`](https://github.com/pgvector/pgvector) on the existing
  Postgres deployment (`k8s/postgres-statefulset.yaml`) — no new database, no managed
  vector-DB service.

## How it fits into the pipeline

```
Pod fails
  → dampening (Layer 1) → exact-hash cache (Layer 2) → active-PR check (Layer 3)
  → [NEW] embed cleaned logs
  → [NEW] RAG lookup: same error_state + worked=true + cosine similarity ≥ threshold?
       ├─ hit  → reuse past root_cause + patch → Validator dry-run only → PatchRequest
       └─ miss → Analyst → Fixer → Validator (full pipeline, as before)
  → PatchRequest CRD created, labeled source=rag_cache or source=ai_pipeline
  → (later) outcome checker marks worked=true/false → next incident can learn from it
```

Key safety rules (all in `controller/db.py::find_similar_incident` and
`controller/main.py::_run_diagnosis_pipeline`):

- Only matches within the **same `error_state`** (never compares an OOMKilled log against
  a CrashLoopBackOff one, even if the text looks similar).
- Only considers incidents where the past fix **actually worked**
  (`worked = true`, confirmed by the outcome checker after the observation window).
- Even on a RAG hit, the reused patch is still **dry-run validated**
  (`ValidatorAgent.validate`) against the live cluster before a PatchRequest is created —
  cheap, no LLM call, but catches a patch that no longer applies cleanly.
- Every PatchRequest is labeled `source: rag_cache` or `source: ai_pipeline`, and RAG
  hits carry `matches_past_incident: <incident_id>` — fully auditable.

Default similarity threshold is **0.75** (`RAG_SIMILARITY_THRESHOLD` env var). This isn't
a magic number — see [Notes / gotchas](#notes--gotchas) below for how it was tuned.

## Files touched

| File | What changed |
|---|---|
| `controller/db.py` | New — Postgres connection pool, incident persistence, `find_similar_incident()` |
| `controller/embeddings.py` | New — local `fastembed` model wrapper |
| `db/schema.sql`, `k8s/postgres-statefulset.yaml` | `pgvector` extension + `embedding vector(384)` column; image switched to `pgvector/pgvector:pg15` |
| `k8s/controller-deployment.yaml` | Postgres connection env vars, `RAG_SIMILARITY_THRESHOLD`, bumped memory limits (256Mi→768Mi for the embedding model) |
| `Dockerfile` | Pre-downloads the embedding model at build time (no runtime network dependency) |
| `controller/main.py` | Wires embedding + RAG lookup into the diagnosis pipeline; persists the applied patch after approval |
| `controller/outcome_checker.py` | Marks `worked=true/false` in Postgres when an incident closes or rolls back |

---

## Try it yourself

Prerequisites: cluster is up and `bootstrap.sh` has already been run once (controller,
dashboard, observability stack all deployed). These commands assume you're in the repo
root with `kubectl` pointed at the `sre-agent-cluster` context.

### 1. Deploy Postgres with pgvector

```bash
kubectl apply -f k8s/postgres-statefulset.yaml
kubectl rollout status statefulset/postgres -n monitoring --timeout=120s
```

Verify the extension and schema:

```bash
kubectl exec -n monitoring statefulset/postgres -- \
  psql -U sreagent -d sredb -c "\dx"
# should list: vector | 0.8.6 | public | vector data type and ivfflat and hnsw access methods

kubectl exec -n monitoring statefulset/postgres -- \
  psql -U sreagent -d sredb -c "\d incidents" | grep embedding
# embedding | vector(384) |
```

### 2. Build and deploy the controller (with RAG baked in)

```bash
docker build -t sre-controller:latest .
kind load docker-image sre-controller:latest --name sre-agent-cluster
kubectl apply -f k8s/controller-deployment.yaml
kubectl rollout restart deployment/sre-controller -n monitoring
kubectl rollout status deployment/sre-controller -n monitoring --timeout=120s
```

Confirm startup wired everything up:

```bash
kubectl logs -n monitoring deploy/sre-controller --tail=20
```

You should see, in order:
```
✅ Postgres pool initialised (postgres-svc.monitoring.svc.cluster.local)
✅ Embedding model loaded (BAAI/bge-small-en-v1.5, dim=384)
```

### 3. First incident — no memory yet, full AI pipeline runs

```bash
kubectl apply -f demo-apps/payment-gateway-oom.yaml
kubectl logs -n monitoring deploy/sre-controller -f | grep -E "INC-|Analyst|Fixer|Validator|PatchRequest CRD"
```

You'll see the full `Starting Multi-Agent Remediation Pipeline` → Analyst RCA → Fixer →
Validator → `PatchRequest CRD created` sequence (takes ~15-20s, real Vertex AI calls).

Approve it so the outcome checker can mark it as a successful fix (needed before RAG has
anything to reuse):

```bash
PR_NAME=$(kubectl get pr -n production -l target-deployment=payment-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl patch pr $PR_NAME -n production \
  --subresource=status --type=merge \
  -p '{"status":{"approvalState":"Approved","approvedBy":"you@example.com"}}'
```

Wait ~90s (the default `OUTCOME_OBSERVATION_WINDOW`) and confirm it closed successfully:

```bash
kubectl exec -n monitoring statefulset/postgres -- psql -U sreagent -d sredb -c \
  "SELECT incident_id, state, worked, patch_applied IS NOT NULL AS has_patch FROM incidents;"
```

You should see `state=Closed`, `worked=t`, `has_patch=t`.

### 4. Second incident — same crash, this time RAG should catch it

```bash
kubectl delete -f demo-apps/payment-gateway-oom.yaml
kubectl delete pr -n production --all
sleep 3
kubectl apply -f demo-apps/payment-gateway-oom.yaml
kubectl logs -n monitoring deploy/sre-controller -f | grep -E "INC-|RAG|Starting Multi-Agent|PatchRequest CRD"
```

**Expected**: instead of `Starting Multi-Agent Remediation Pipeline`, you should see:

```
🧠 RAG match: INC-2026-XXXX-XXXX (similarity=1.000) — reusing its patch instead of calling the AI agents
PatchRequest CRD created: payment-gateway-pr-...
```

No Analyst/Fixer/Validator LLM calls — the `PatchRequest` appears in well under a second.

Confirm the label:

```bash
PR_NAME=$(kubectl get pr -n production -l target-deployment=payment-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl get pr $PR_NAME -n production -o jsonpath='{.metadata.labels}'
# {"incident-id":"...","source":"rag_cache","target-deployment":"payment-gateway"}
```

### 5. Negative test — a different, never-successful error should NOT match

```bash
kubectl delete -f demo-apps/payment-gateway-oom.yaml
kubectl delete pr -n production --all
sleep 3
kubectl apply -f demo-apps/shipping-service-failure.yaml
kubectl logs -n monitoring deploy/sre-controller -f | grep -E "INC-|RAG|Starting Multi-Agent|PatchRequest CRD"
```

`shipping-service` fails with `CrashLoopBackOff` (a different error type, and one that
this demo app can never actually resolve — see `demo-apps/shipping-service-failure.yaml`,
its container unconditionally `sys.exit(1)`s). You should see the full AI pipeline run
again — **no RAG match line** — because:
- the error type doesn't match anything in the `worked=true` pool, and
- even within `CrashLoopBackOff`, nothing has ever actually been fixed successfully.

```bash
PR_NAME=$(kubectl get pr -n production -l target-deployment=shipping-service -o jsonpath='{.items[0].metadata.name}')
kubectl get pr $PR_NAME -n production -o jsonpath='{.metadata.labels}'
# {"incident-id":"...","source":"ai_pipeline","target-deployment":"shipping-service"}
```

---

## What we actually saw (this session's run)

Real results from testing this against a live cluster:

| Step | Result |
|---|---|
| Embedding model load time | ~2.2s at controller startup |
| Similarity: two hand-written near-duplicate sentences | 0.87 |
| Similarity: two *real* independent OOM incident runs (same demo, different pods) | 0.79 |
| Similarity: a third run against the stored embedding | **1.000** (near-identical log capture) |
| RAG hit latency (embed + pgvector query + Validator dry-run) | < 1s, vs. ~15-20s for the full 3-agent pipeline |
| False-positive check (shipping-service vs. OOM history) | No match — correctly fell through to full AI pipeline |

## Notes / gotchas

- **The threshold started at 0.90 and had to be lowered to 0.75.** A synthetic test with
  two hand-written near-duplicate sentences scored 0.87, which set an unrealistic
  expectation. Two genuinely-identical real crash incidents (same demo app, same root
  cause) only scored 0.79 — real crash logs carry more incidental noise (pod name
  suffixes, event ordering, timestamps) than clean prose does. 0.75 is still safe because
  it only ever ranks candidates *within* a pool that's already hard-filtered to the same
  `error_state` and `worked = true`.
- **`patch_applied` has to be written back to Postgres after approval**, not just at
  incident-creation time — the patch doesn't exist yet when the incident row is first
  inserted (that happens before human approval). This is done in
  `on_patchrequest_approved` via `db.save_applied_patch()`. Skipping this step silently
  makes RAG never find anything to reuse, with no error anywhere — worth checking first
  if RAG isn't matching things you expect it to.
- **Controller memory limit had to increase** (256Mi → 768Mi) — the ONNX runtime powering
  `fastembed` needs more headroom than the operator's original tiny limit allowed;
  without this the pod gets silently OOMKilled and restarted.
- RAG only ever *skips the LLM calls*, never the Kubernetes-level safety check — the
  Validator's dry-run still runs on every reused patch before a PatchRequest is created.
