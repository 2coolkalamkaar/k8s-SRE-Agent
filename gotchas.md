# K8s SRE Agent — Gotchas & How to Conquer Them

> Plain-English explanations of every non-obvious problem you will hit,
> and exactly how to fix each one before it wastes your time.

---

## How to Read This Document

Each gotcha follows this structure:
- 🤔 **What is it?** — Simple explanation, no jargon
- 💥 **What breaks?** — The exact failure you'll see
- 🛡️ **How we conquer it** — The precise code/YAML fix

---

## PART 1 — Day-0 Decisions
> *These must be settled before writing a single line of code.*

---

### Gotcha #1 — Kopf's Event Loop Will Jam Up Like a Single-Lane Road

**🤔 What is it?**

Think of Kopf as a single traffic cop managing all events on a one-lane road. Every time a pod crashes, Kopf sends it to Ollama for diagnosis. Each diagnosis takes 30–60 seconds on your CPU. Now imagine 20 pods crash at the same time. Kopf lines them up one after another — pod #20 only gets diagnosed **20 minutes later**. During that whole time, the "road" is blocked for everything else.

This happens because Python's `asyncio` (which Kopf uses) runs everything in a **single thread**. If you don't control how many Ollama calls run simultaneously, they stack up and everything grinds to a halt.

**💥 What breaks?**

- Pod #20 gets a Slack alert 20 minutes after it crashed — useless
- New K8s events pile up unprocessed while Kopf is waiting on Ollama
- The controller appears "frozen" with no errors in logs — hardest kind of bug to debug
- Under very high load, the Kopf Watch API connection times out and reconnects, potentially missing events

**🛡️ How we conquer it**

Use an `asyncio.Semaphore`. Think of it as a "maximum 3 cars can enter at once" sign. The 4th car waits, but the road never fully jams.

```python
# controller/main.py

import asyncio

# This is a global gate — only 3 Ollama calls can run at the same time.
# The 4th waits patiently without blocking anything else.
_ollama_semaphore = asyncio.Semaphore(3)

async def call_ollama_with_gate(prompt: str) -> dict:
    async with _ollama_semaphore:
        # Once a slot is free, this runs.
        # Other handlers continue processing other events while waiting.
        return await call_ollama(prompt)
```

Why 3? On a 16GB CPU-only cluster, 3 concurrent Ollama calls at ~1.5GB RAM each = 4.5GB — safe. You can tune this number based on your RAM headroom.

---

### Gotcha #2 — Using the Wrong PostgreSQL Driver Will Deadlock Everything

**🤔 What is it?**

There are two ways to talk to PostgreSQL from Python:

1. **The old way** (`psycopg2`): Blocks the whole program while waiting for the DB. Like a person who stops walking to read a text message.
2. **The async way** (`asyncpg`): Sends the query and continues doing other things while waiting. Like sending a text and continuing to walk.

Kopf is built entirely on `asyncio` — the async model. If you use `psycopg2` inside Kopf, every DB query freezes the entire event loop. Your controller stops processing K8s events while waiting for a PostgreSQL `SELECT`. It's a **silent deadlock** — no crash, just the controller slowly becoming less and less responsive.

**💥 What breaks?**

- Controller handles 10 events/min with `asyncpg`, but only 1–2/min with `psycopg2`
- Grafana queries are slow → Kopf event processing is slow (they share the same DB connection pool)
- Under load, the whole controller just… stops responding. No error. Very hard to diagnose.

**🛡️ How we conquer it**

Commit to the async stack from day one. Lock these exact versions in `requirements.txt`:

```
# requirements.txt — THE CORRECT STACK
kopf==1.37.2
kubernetes
asyncpg==0.29.0          # ✅ Async PostgreSQL driver
sqlalchemy[asyncio]==2.0.31  # ✅ Async SQLAlchemy wrapper
httpx==0.27.0            # ✅ Async HTTP client for Ollama (not requests!)

# DO NOT add these — they will cause silent deadlocks:
# psycopg2          ❌
# requests          ❌
```

And write your DB engine like this:

```python
# db/connection.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Note: postgresql+asyncpg:// — NOT postgresql://
engine = create_async_engine(
    "postgresql+asyncpg://user:password@postgres-svc/sredb",
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# Usage in a Kopf handler:
async def save_incident(incident):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            session.add(incident)
            # ✅ This yields control back to the event loop while waiting
            await session.commit()
```

---

### Gotcha #3 — RBAC: The "Can vs. Should" Problem

**🤔 What is it?**

RBAC (Role-Based Access Control) controls what your agent is allowed to do in the cluster. The temptation is to give it wide permissions early so "everything just works." This is like giving a new employee the master key to the building on day one.

The specific tension: to diagnose "missing secret" errors, the Observer needs to *check if a Secret exists*. But if the Observer can read Secrets, and someone finds a way to inject malicious content into your Ollama prompts (prompt injection), they could potentially extract Secret values through the LLM responses.

**💥 What breaks if you get this wrong?**

- If too permissive: A prompt injection attack could read cluster secrets through the LLM
- If too restrictive: Can't diagnose "Missing Secret 'db-creds'" because the Observer can't even list Secrets
- Giving the Patch Executor broad rights means a buggy LLM patch could delete or corrupt critical resources

**🛡️ How we conquer it**

Two ServiceAccounts. Write the exact YAML before coding anything:

```yaml
# k8s/rbac.yaml

---
# SA 1: The Observer — reads everything, writes nothing (except CRDs)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: sre-observer-role
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events", "nodes", "namespaces"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["list"]          # ✅ Can LIST (knows if secret EXISTS)
                             # ❌ No "get" — cannot READ secret VALUES
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["sre.yourdomain.io"]
    resources: ["patchrequests", "incidentrecords"]
    verbs: ["get", "list", "create", "update", "patch"]

---
# SA 2: The Executor — narrow write rights only
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: sre-executor-role
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["patch"]         # ✅ Patch only — cannot delete or create
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["patch"]
  - apiGroups: ["sre.yourdomain.io"]
    resources: ["patchrequests"]
    verbs: ["get", "list", "update", "patch"]
  # ❌ No access to: Secrets, Nodes, RBAC, ClusterRoles, Namespaces
```

The key insight: `list` on Secrets tells you "does this secret exist?" — enough for diagnosis — without exposing the secret's actual value to the LLM.

---

## PART 2 — Day-1 Deployment Gotchas
> *These will break your first deploy silently.*

---

### Gotcha #4 — Ollama Takes 20 Minutes to Start and Nobody Tells You

**🤔 What is it?**

When Ollama starts for the very first time, it needs to download `deepseek-coder:6.7b` — a ~4.5GB model file. On a typical home/office internet connection (50 Mbps down), that's **12–15 minutes**. On your cluster's internal network, it depends on how you pull the image.

During this download, Ollama is running (the process is up) but not ready to respond to inference requests. Your Kopf controller sees Ollama is "alive" and immediately starts sending diagnosis requests. Every single one fails. Silently. No crash. Just empty responses.

**💥 What breaks?**

- First 15 minutes of deployment: every pod error goes undiagnosed
- Controller logs show `Connection refused` or `timeout` — looks like a network issue
- If you don't have retry logic, those incidents are lost forever

**🛡️ How we conquer it**

Two-part solution:

**Part 1 — Readiness Probe** (tells K8s "don't send traffic until model is loaded"):
```yaml
# k8s/ollama-statefulset.yaml
containers:
  - name: ollama
    image: ollama/ollama:latest
    readinessProbe:
      httpGet:
        path: /api/tags      # Returns 200 + model list when ready
        port: 11434
      initialDelaySeconds: 30   # Give it 30s before first check
      periodSeconds: 15
      failureThreshold: 40      # Allow up to 10 minutes (40 × 15s) to pull model
    livenessProbe:
      httpGet:
        path: /api/tags
        port: 11434
      initialDelaySeconds: 120
      periodSeconds: 30
```

**Part 2 — Startup init Job** (pre-pull model so StatefulSet restarts are instant):
```yaml
# k8s/ollama-model-init-job.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: ollama-pull-model
  namespace: ai-infra
spec:
  template:
    spec:
      containers:
        - name: model-puller
          image: curlimages/curl
          command:
            - /bin/sh
            - -c
            - |
              echo "Waiting for Ollama to be ready..."
              until curl -sf http://ollama-service:11434/api/tags; do sleep 5; done
              echo "Pulling model..."
              curl -X POST http://ollama-service:11434/api/pull \
                -d '{"name": "deepseek-coder:6.7b-instruct"}'
              echo "Model ready!"
      restartPolicy: OnFailure
```

After this Job completes, the model is stored on the PVC. Future Ollama pod restarts skip the download entirely.

---

### Gotcha #5 — Ollama Will Give You Broken JSON (Guaranteed)

**🤔 What is it?**

You ask the LLM to return valid JSON. It tries. But local 6.7B models are not perfectly reliable at structured output. They'll sometimes:

- Wrap the JSON in markdown code fences: ` ```json { ... } ``` `
- Add trailing commas: `{"key": "value",}` — invalid JSON
- Write comments inside JSON: `// this is the cause` — invalid JSON
- Cut off mid-response if the output is too long: `{"root_cause": "The pod crash` (truncated)

Python's `json.loads()` crashes on all of these. Without a safety net, one bad LLM response kills your Kopf handler, and the incident goes completely unprocessed.

**💥 What breaks?**

- Kopf handler raises an unhandled exception → K8s marks the handler as failed
- Kopf retries the handler up to 5 times, calling Ollama 5 times for the same event
- All 5 responses might be broken → incident never gets a PatchRequest
- In your Slack, silence. No alert. Pod still crashing.

**🛡️ How we conquer it**

A bullet-proof response parser with multiple fallback layers:

```python
# controller/ollama_client.py
import json
import re
import logging

logger = logging.getLogger(__name__)

def parse_llm_response(raw_text: str, incident_id: str) -> dict:
    """
    Parse Ollama response with multiple fallback strategies.
    Never raises — always returns a dict (empty on total failure).
    """
    if not raw_text or not raw_text.strip():
        logger.warning(f"[{incident_id}] Empty response from Ollama")
        return {}

    # Strategy 1: Strip markdown code fences (most common issue)
    # Handles: ```json { ... } ``` and ``` { ... } ```
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()

    # Strategy 2: Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 3: Find the first { ... } block (handles leading/trailing text)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 4: Remove trailing commas (common LLM mistake)
    # Changes {"key": "val",} to {"key": "val"}
    no_trailing = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(no_trailing)
    except json.JSONDecodeError:
        pass

    # Strategy 5: Total failure — return a safe default
    # This creates a low-confidence PatchRequest so a human can investigate
    logger.error(
        f"[{incident_id}] Could not parse LLM response after all strategies.\n"
        f"Raw response (first 500 chars):\n{raw_text[:500]}"
    )
    return {
        "root_cause": "LLM returned unparseable response. Manual investigation required.",
        "severity": "high",
        "suggested_fix": "Check Ollama logs and raw incident logs manually.",
        "auto_restart_safe": False,
        "config_suggestions": [],
        "likely_recurring": False,
        "estimated_impact": "Unknown — automated diagnosis failed.",
    }
```

The last fallback is important: instead of losing the incident entirely, it creates a `PatchRequest` flagging it for human review. Nothing falls through the cracks.

---

### Gotcha #6 — etcd Has a 1.5MB Size Limit Per Object

**🤔 What is it?**

Every Kubernetes resource (Pods, Deployments, your CRDs) is stored in etcd — the cluster's "database." etcd has a hard limit: **each object can be at most 1.5MB**.

Your `IncidentRecord` CRD wants to store:
- The full LLM response (a few KB)
- The cleaned log excerpt (can be 50–200 KB)
- The proposed patch YAML (a few KB)
- All the metadata

A single Java app crash with a full stack trace, cleaned and stored, can easily hit 100–200 KB. With 5–10 fields of similar size, you'll bump into the 1.5MB limit after a few months of incidents.

When you hit this limit, the K8s API server rejects the write with an opaque error. The incident data is silently lost.

**💥 What breaks?**

- `kubectl apply` on a large IncidentRecord fails with `Request entity too large`
- No error in your application logs — the K8s API just returns a 413 error
- The incident exists in PostgreSQL but the CRD creation fails → system is in a split state

**🛡️ How we conquer it**

**Rule**: CRDs are your index card. PostgreSQL is your filing cabinet.

```python
# controller/incident_writer.py

# What goes in the CRD (lightweight — stays under 50KB guaranteed)
CRD_PAYLOAD = {
    "spec": {
        "incidentId":       incident.incident_id,
        "errorState":       incident.error_state,
        "errorFingerprint": incident.fingerprint,
        "targetDeployment": incident.deployment,
        "targetNamespace":  incident.namespace,
        "rootCause":        llm_response.get("root_cause", ""),  # 1 sentence only
        "severity":         llm_response.get("severity", ""),
        "state":            "Investigating",
        "seenCount":        1,
        # ✅ Short summary only — NOT the full diagnosis
        "llmSummary":       llm_response.get("suggested_fix", "")[:500],
    }
}

# What goes in PostgreSQL (full detail — no size limit)
DB_PAYLOAD = {
    "incident_id":       incident.incident_id,
    "llm_diagnosis":     llm_response,            # ✅ Full JSON
    "log_excerpt":       cleaned_logs,             # ✅ Full cleaned logs
    "patch_applied":     patch,                    # ✅ Full patch YAML
    "raw_events":        events_text,              # ✅ Full K8s events
}
```

**Never put these in a CRD:**
- Raw or cleaned log text
- Full LLM JSON response
- Full patch YAML (use a summary/description instead)

---

### Gotcha #7 — Controller Restart Misses All Events During Downtime

**🤔 What is it?**

Kopf uses the Kubernetes Watch API — a live stream of cluster events. Think of it like a live sports stream. If your internet cuts out for 5 minutes, you miss those 5 minutes of the game. They're gone. No replay.

When your Kopf controller restarts (for an upgrade, an OOM kill, or even a regular pod reschedule), it reconnects to the Watch API and resumes from "now." Any pod that crashed during the downtime was never seen. No diagnosis. No alert.

**💥 What breaks?**

- Pod crashes at 2:00 AM while controller is restarting for an upgrade
- Controller comes back online at 2:05 AM — pod is now in `CrashLoopBackOff`
- Watch API only tells Kopf about *future* events — it missed the 2:00 AM crash
- No Slack alert. No PatchRequest. SRE finds out at 9 AM when users complain.

**🛡️ How we conquer it**

Add a startup scan — when the controller comes online, it actively looks for existing problems:

```python
# controller/main.py

@kopf.on.startup()
async def catch_up_scan(logger, **kwargs):
    """
    Runs once when the controller starts.
    Scans every pod in the cluster for existing error states
    that may have been missed during downtime.
    """
    logger.info("🔍 Running startup catch-up scan for missed events...")
    
    v1 = kubernetes.client.CoreV1Api()
    # List all pods across all namespaces
    all_pods = v1.list_pod_for_all_namespaces(watch=False)
    
    missed_count = 0
    for pod in all_pods.items:
        error_state = detect_error_state(pod.status)
        
        if not error_state:
            continue  # Pod is healthy, skip
        
        # Check if we already have an open PatchRequest for this pod
        # (Don't re-diagnose something we caught before the restart)
        already_handled = await has_open_patchrequest(
            pod.metadata.namespace,
            get_owner_deployment(pod),
            error_state,
        )
        
        if not already_handled:
            logger.warning(
                f"⚠️  Missed event during downtime: "
                f"{pod.metadata.name} is {error_state}. Re-queuing diagnosis."
            )
            # Re-trigger the normal diagnosis pipeline
            await diagnose_pod(pod, error_state)
            missed_count += 1
    
    logger.info(f"✅ Catch-up scan complete. Found {missed_count} missed incidents.")
```

This runs in under 5 seconds for a cluster with <500 pods. It's the insurance policy for every restart.

---

## PART 3 — Operational Risks
> *These won't break your demo but will break production.*

---

### Gotcha #8 — Ollama Can Kill Your Production Pods (The Death Spiral)

**🤔 What is it?**

Imagine your hospital's doctor is using so much electricity that the patient's life support machine starts failing. That's this gotcha.

Ollama running on CPU uses **100% of available cores** during inference. If Ollama shares a Kubernetes node with your production pods (auth-service, payment-gateway), and Ollama hogs all the CPU during a diagnosis, those production pods starve of CPU. They fail their liveness probes. Kubernetes kills them. They crash. Now Kopf detects more crashes → calls Ollama → more CPU hogging → more crashes. A death spiral.

**💥 What breaks?**

- Ollama starts a 45-second inference at peak traffic time
- payment-gateway pod gets 0 CPU for 45 seconds → liveness probe times out → pod killed
- This creates a new `CrashLoopBackOff` event → Kopf calls Ollama again to diagnose it
- You've now created more incidents by trying to fix incidents

**🛡️ How we conquer it**

Two weapons:

**Weapon 1 — CPU Limits on Ollama** (for single-node clusters like minikube):
```yaml
# k8s/ollama-statefulset.yaml
containers:
  - name: ollama
    resources:
      requests:
        cpu: "2"
        memory: "5Gi"
      limits:
        cpu: "4"        # ✅ Ollama can NEVER use more than 4 cores
        memory: "5500Mi"
        # This leaves your remaining cores for production pods
```

**Weapon 2 — Node Taint + PriorityClass** (for multi-node clusters):
```bash
# Dedicate one node entirely to Ollama (run once, before deploying)
kubectl taint nodes <your-ai-node> ai-specialist=true:NoSchedule
```

```yaml
# k8s/ollama-statefulset.yaml
spec:
  template:
    spec:
      tolerations:
        - key: "ai-specialist"
          operator: "Equal"
          value: "true"
          effect: "NoSchedule"
      # Ollama gets lowest priority — if the node is starved,
      # the OS will deprioritise Ollama's processes first
      priorityClassName: ai-low-priority
```

```yaml
# k8s/priority-classes.yaml
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: production-critical
value: 1000000       # Highest

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: sre-monitoring
value: 500000

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: ai-low-priority
value: 100           # Lowest — Ollama always yields to everything else
```

---

### Gotcha #9 — CRD Schema Changes Are a Nightmare After Deployment

**🤔 What is it?**

Once you deploy a CRD to Kubernetes and objects are created against it, changing the schema is painful. Kubernetes stores all existing objects as-is. If you add a required field, all existing objects become invalid. If you rename a field, all your code breaks.

This is like changing the columns of a database table that already has 10,000 rows — except harder because there's no `ALTER TABLE` for CRDs.

**💥 What breaks?**

- You add `estimatedImpact` field to `PatchRequest` in week 3
- All `PatchRequest` objects created in weeks 1–2 don't have that field
- Your Python code does `pr["spec"]["estimatedImpact"]` → `KeyError` on old objects
- The schema validation rejects the new field if you didn't plan for it

**🛡️ How we conquer it**

**Step 1**: Lock down your final CRD field list before writing any code. Use this process: go through every piece of data the system needs and add it to the schema upfront, even if you don't use it in week 1.

**Step 2**: Use `x-kubernetes-preserve-unknown-fields: true` on any nested object where you know the schema will evolve:

```yaml
# k8s/crds/patch-request-crd.yaml
schema:
  openAPIV3Schema:
    type: object
    properties:
      spec:
        type: object
        properties:
          # Stable fields — strict schema
          incidentId:     { type: string }
          errorState:     { type: string }
          severity:       { type: string, enum: [low, medium, high] }
          approvalState:  { type: string, enum: [Pending, Approved, Rejected, Applied] }
          
          # Flexible fields — no schema enforcement, free to evolve
          llmDiagnosis:
            type: object
            x-kubernetes-preserve-unknown-fields: true  # ✅ Add fields freely
          proposedPatch:
            type: object
            x-kubernetes-preserve-unknown-fields: true  # ✅ Patch format can change
```

**Step 3**: Always use `.get()` in Python when reading CRD fields — never direct key access:

```python
# ❌ Bad — crashes on old objects missing the field
impact = pr["spec"]["estimatedImpact"]

# ✅ Good — returns None gracefully on old objects
impact = pr.get("spec", {}).get("estimatedImpact", "Not assessed")
```

---

### Gotcha #10 — Grafana Needs PostgreSQL Credentials and You'll Hardcode Them

**🤔 What is it?**

This sounds boring but causes real problems. Grafana needs a username and password to connect to PostgreSQL. The path of least resistance is to put them directly in your YAML files. This is dangerous because:

- YAML files get committed to Git → credentials leak to your repo
- Anyone with `kubectl get configmap` access can read them
- If you ever rotate the PostgreSQL password, you have to update every file that has it hardcoded

**💥 What breaks?**

- In a team setting, the Grafana datasource config in Git has the real DB password → security incident
- Password rotation requires redeploying multiple components instead of changing one Secret
- A `kubectl get configmap -o yaml` from a compromised pod reads the DB password

**🛡️ How we conquer it**

Always use Kubernetes Secrets + environment variable injection:

```bash
# Step 1: Create the secret once (never committed to Git)
kubectl create secret generic postgres-credentials \
  --from-literal=username=sreagent \
  --from-literal=password='your-strong-password-here' \
  -n monitoring
```

```yaml
# k8s/grafana-deployment.yaml
containers:
  - name: grafana
    image: grafana/grafana:10.4.0
    env:
      - name: GF_DATABASE_USER
        valueFrom:
          secretKeyRef:
            name: postgres-credentials
            key: username
      - name: GF_DATABASE_PASSWORD    # ✅ Never in plain text
        valueFrom:
          secretKeyRef:
            name: postgres-credentials
            key: password
```

```yaml
# grafana/datasources/postgres.yaml (auto-provisioned, safe to commit)
datasources:
  - name: PostgreSQL
    type: postgres
    url: postgres-svc.monitoring.svc.cluster.local:5432
    user: ${GF_DATABASE_USER}       # ✅ Read from env var, not hardcoded
    secureJsonData:
      password: ${GF_DATABASE_PASSWORD}
    jsonData:
      database: sredb
      sslmode: disable
```

---

## The Master Checklist

Print this out. Check off each item before starting the build.

```
PRE-BUILD (Week 0)
  □ RBAC ClusterRole YAML written and reviewed by a second person
  □ requirements.txt pinned with asyncpg + sqlalchemy[asyncio] + httpx
  □ CRD field list finalised — no new required fields after this point
  □ Node strategy decided: dedicated AI node (multi-node) OR CPU limits (single node)
  □ PostgreSQL credentials strategy: K8s Secret created, not in any YAML file

BEFORE FIRST DEPLOY (Week 2)
  □ asyncio.Semaphore(3) added to Ollama call wrapper
  □ Ollama readiness probe configured (path: /api/tags)
  □ Ollama init Job to pre-pull model written
  □ parse_llm_response() with all 5 fallback strategies written and unit-tested
  □ etcd size limit rule documented: summaries in CRD, full data in PostgreSQL

BEFORE PRODUCTION (Week 8)
  □ Startup catch-up scan (@kopf.on.startup) tested by manually restarting controller
  □ Death spiral test: crash 10 pods, verify Ollama CPU limit holds
  □ PriorityClass applied and verified with kubectl describe pod
  □ All CRD fields use .get() with defaults — no direct key access
  □ Grafana datasource uses env var injection — password not in any committed file
```

---

## Quick Reference: Gotcha → Fix Summary

| # | Gotcha | Fix in One Line |
|---|---|---|
| 1 | Kopf event loop jams | `asyncio.Semaphore(3)` around all Ollama calls |
| 2 | Wrong PostgreSQL driver deadlocks | Use `asyncpg` + `sqlalchemy[asyncio]`, never `psycopg2` |
| 3 | RBAC too wide or too narrow | Two ServiceAccounts; Observer `list` Secrets, never `get` |
| 4 | Ollama takes 20 min to start | Readiness probe on `/api/tags` + init Job to pre-pull model |
| 5 | Ollama returns broken JSON | 5-layer parse function that never throws, always returns a default |
| 6 | CRD hits etcd 1.5MB limit | Summaries in CRD, full data in PostgreSQL |
| 7 | Controller restart misses events | `@kopf.on.startup` catch-up scan of all existing pods |
| 8 | Ollama kills production pods | CPU limits `4` cores OR dedicated node taint |
| 9 | CRD schema breaks after deploy | Lock schema upfront; use `x-kubernetes-preserve-unknown-fields` |
| 10 | DB credentials hardcoded | K8s Secret + env var injection — never in YAML or Git |
