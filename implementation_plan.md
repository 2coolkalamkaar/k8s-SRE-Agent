# K8s SRE Agent — Complete Implementation Plan

## Background

A production-grade, air-gapped Kubernetes SRE agent that:
- Watches cluster events via Kopf (Python operator)
- Diagnoses failures with a local Ollama LLM (no data leaves VPC)
- Proposes human-approved patches via a `PatchRequest` CRD
- Stores all incidents in PostgreSQL with a strict State Machine
- Exposes Grafana dashboards for SRE visibility

---

## Open Questions

> [!IMPORTANT]
> **Do you need the Grafana dashboard to be accessible externally (Ingress)?**
> Or is `kubectl port-forward` acceptable for the project?

> [!IMPORTANT]
> **Slack token / SMTP credentials** — will you manage these via Kubernetes Secrets manually, or do you use an external secrets manager (e.g., Vault)?

> [!WARNING]
> **PostgreSQL on 16GB RAM** adds ~200MB. The total memory budget becomes ~15GB.
> This is still within range but leaves less buffer. Confirmed acceptable?

---

## Database Decision: Why PostgreSQL (not just etcd/CRDs)

| Requirement | CRD / etcd | PostgreSQL |
|---|---|---|
| `kubectl get inc` CLI access | ✅ | ❌ |
| Grafana native datasource | ❌ | ✅ |
| Complex queries (MTTR by month, top errors) | ❌ | ✅ |
| State transition audit log | ❌ (limited) | ✅ (full table) |
| Survives controller restarts | ✅ | ✅ (PVC-backed) |
| Fuzzy / full-text search | ❌ | ✅ (pg_trgm) |
| RAM cost | ~0 (uses etcd) | ~200MB |

**Decision: Hybrid** — Keep CRDs for `kubectl` access (SRE CLI workflow). Add PostgreSQL as the analytics + dashboard backend. The Kopf controller writes to **both** on every state transition.

---

## Architecture: State Pattern

```
                        ┌──────────────────────────────────────┐
                        │          Incident State Machine       │
                        └──────────────────────────────────────┘

  K8s Error                                              RCA provided
  Detected      LLM Diagnosis         Patch Applied      + Verified
     │          + PR Created          by Executor           │
     ▼               ▼                    ▼                 ▼
┌─────────┐    ┌──────────────┐    ┌──────────────┐  ┌──────────┐
│  OPEN   │───▶│ INVESTIGATING│───▶│   RESOLVED   │─▶│  CLOSED  │
└─────────┘    └──────────────┘    └──────────────┘  └──────────┘
                      │                   │
                      │ PR Rejected       │ Patch didn't work
                      ▼                   ▼
                   ┌──────────┐     ┌──────────────┐
                   │   OPEN   │◀────│ INVESTIGATING │
                   └──────────┘     └──────────────┘

Strict rules:
  OPEN        → INVESTIGATING only
  INVESTIGATING → RESOLVED (patch approved) | OPEN (PR rejected)
  RESOLVED    → CLOSED (RCA validated) | INVESTIGATING (patch failed)
  CLOSED      → terminal. No further transitions.
```

### Transactional RCA Validation
Before `RESOLVED → CLOSED`, the system enforces:
1. `rca_summary` must be ≥ 30 characters
2. `resolution.worked` must be `true` (verified by outcome check)
3. `approved_by` must be set (not auto-applied)
4. All fields written atomically — if any fail, transition is rejected

---

## Proposed Changes

---

### Component 1: Database Layer

#### [NEW] `db/schema.sql`
Full PostgreSQL schema with 4 tables.

#### [NEW] `db/models.py`
SQLAlchemy ORM models matching the schema.

#### [NEW] `k8s/postgres-statefulset.yaml`
PostgreSQL 15 StatefulSet with 5Gi PVC in `monitoring` namespace.

**Schema design:**

```sql
-- Core incident table
CREATE TABLE incidents (
    id              SERIAL PRIMARY KEY,
    incident_id     VARCHAR(20) UNIQUE NOT NULL,   -- INC-2026-0047
    state           VARCHAR(20) NOT NULL DEFAULT 'Open',
    error_state     VARCHAR(50) NOT NULL,           -- CrashLoopBackOff
    error_fingerprint VARCHAR(16) NOT NULL,
    target_deployment VARCHAR(100) NOT NULL,
    target_namespace  VARCHAR(100) NOT NULL,
    root_cause      TEXT,
    llm_diagnosis   JSONB,
    patch_applied   JSONB,
    approved_by     VARCHAR(100),
    resolution_notes TEXT,
    rca_summary     TEXT,                           -- Required for CLOSED
    worked          BOOLEAN,
    mttd_seconds    INTEGER,
    mttr_seconds    INTEGER,
    recurrence_count INTEGER DEFAULT 1,
    tags            TEXT[],
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    investigating_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Full audit log of every state transition
CREATE TABLE state_transitions (
    id              SERIAL PRIMARY KEY,
    incident_id     VARCHAR(20) NOT NULL REFERENCES incidents(incident_id),
    from_state      VARCHAR(20),
    to_state        VARCHAR(20) NOT NULL,
    triggered_by    VARCHAR(100),                   -- 'system' or 'rahul@company.com'
    reason          TEXT,
    metadata        JSONB,
    transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Each distinct patch proposed by the LLM
CREATE TABLE patch_requests (
    id              SERIAL PRIMARY KEY,
    pr_name         VARCHAR(100) UNIQUE NOT NULL,   -- K8s CRD name
    incident_id     VARCHAR(20) REFERENCES incidents(incident_id),
    proposed_patch  JSONB NOT NULL,
    approval_state  VARCHAR(20) DEFAULT 'Pending',
    approved_by     VARCHAR(100),
    applied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Time-series metrics for Grafana graphs
CREATE TABLE incident_metrics (
    id              SERIAL PRIMARY KEY,
    incident_id     VARCHAR(20) REFERENCES incidents(incident_id),
    metric_name     VARCHAR(50) NOT NULL,           -- 'restart_count', 'cpu_usage'
    metric_value    FLOAT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for Grafana query performance
CREATE INDEX idx_incidents_state ON incidents(state);
CREATE INDEX idx_incidents_opened_at ON incidents(opened_at);
CREATE INDEX idx_incidents_deployment ON incidents(target_deployment);
CREATE INDEX idx_incidents_error ON incidents(error_state);
CREATE INDEX idx_transitions_incident ON state_transitions(incident_id);
```

---

### Component 2: State Machine (Python)

#### [NEW] `controller/states.py`

```python
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from controller.incident import Incident

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""

class RCAValidationError(Exception):
    """Raised when RCA does not meet validation requirements before closing."""


class State(ABC):
    """Abstract base for all incident states."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def to_investigating(self, incident: "Incident", llm_diagnosis: dict) -> None: ...

    @abstractmethod
    def to_resolved(self, incident: "Incident", patch: dict, approved_by: str) -> None: ...

    @abstractmethod
    def to_closed(self, incident: "Incident", rca_summary: str) -> None: ...

    def _deny(self, target: str):
        raise InvalidTransitionError(
            f"Cannot transition from {self.name!r} → {target!r}. "
            f"See allowed transitions in the state diagram."
        )


class OpenState(State):
    name = "Open"

    def to_investigating(self, incident, llm_diagnosis):
        logger.info(f"[{incident.incident_id}] Open → Investigating")
        incident._set_state(InvestigatingState())
        incident.llm_diagnosis = llm_diagnosis
        incident.investigating_at = datetime.utcnow()

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved")

    def to_closed(self, incident, rca_summary):
        self._deny("Closed")


class InvestigatingState(State):
    name = "Investigating"

    def to_investigating(self, incident, llm_diagnosis):
        # Re-diagnosis allowed (e.g. after PR rejection)
        logger.info(f"[{incident.incident_id}] Re-diagnosing in Investigating state")
        incident.llm_diagnosis = llm_diagnosis

    def to_resolved(self, incident, patch, approved_by):
        if not patch:
            raise RCAValidationError("A patch must be provided to move to Resolved.")
        if not approved_by:
            raise RCAValidationError("approved_by must be set — auto-patches cannot move to Resolved.")
        logger.info(f"[{incident.incident_id}] Investigating → Resolved (by {approved_by})")
        incident._set_state(ResolvedState())
        incident.patch_applied = patch
        incident.approved_by = approved_by
        incident.resolved_at = datetime.utcnow()

    def to_closed(self, incident, rca_summary):
        self._deny("Closed")


class ResolvedState(State):
    name = "Resolved"

    def to_investigating(self, incident, llm_diagnosis):
        # Patch didn't work — cycle back
        logger.warning(f"[{incident.incident_id}] Resolved → Investigating (patch failed)")
        incident._set_state(InvestigatingState())
        incident.llm_diagnosis = llm_diagnosis
        incident.patch_applied = None
        incident.resolved_at = None

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved")  # Already resolved

    def to_closed(self, incident, rca_summary: str):
        # ── Transactional RCA Validation ──────────────────────────────────
        errors = []
        if not rca_summary or len(rca_summary.strip()) < 30:
            errors.append("rca_summary must be at least 30 characters.")
        if not incident.worked:
            errors.append("Cannot close: outcome not yet verified (worked=None or False).")
        if not incident.approved_by:
            errors.append("Cannot close: no approved_by set.")
        if errors:
            raise RCAValidationError(
                f"[{incident.incident_id}] RCA validation failed:\n" +
                "\n".join(f"  - {e}" for e in errors)
            )
        # ──────────────────────────────────────────────────────────────────
        logger.info(f"[{incident.incident_id}] Resolved → Closed")
        incident._set_state(ClosedState())
        incident.rca_summary = rca_summary.strip()
        incident.closed_at = datetime.utcnow()
        if incident.opened_at:
            incident.mttr_seconds = int(
                (incident.closed_at - incident.opened_at).total_seconds()
            )


class ClosedState(State):
    name = "Closed"

    def to_investigating(self, incident, llm_diagnosis):
        self._deny("Investigating")  # Terminal

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved")  # Terminal

    def to_closed(self, incident, rca_summary):
        self._deny("Closed")  # Already closed
```

#### [NEW] `controller/incident.py`

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from controller.states import State, OpenState
from db.repository import IncidentRepository


@dataclass
class Incident:
    """Domain object representing a single incident lifecycle."""
    incident_id: str
    error_state: str
    error_fingerprint: str
    target_deployment: str
    target_namespace: str

    # Populated through state transitions
    llm_diagnosis: Optional[dict] = None
    patch_applied: Optional[dict] = None
    approved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    rca_summary: Optional[str] = None
    worked: Optional[bool] = None
    tags: list = field(default_factory=list)

    opened_at: Optional[datetime] = None
    investigating_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    mttr_seconds: Optional[int] = None

    _state: State = field(default_factory=OpenState, repr=False)
    _repo: Optional[IncidentRepository] = field(default=None, repr=False)

    def __post_init__(self):
        self.opened_at = datetime.utcnow()

    def _set_state(self, new_state: State):
        """Internal. Records transition to DB before applying."""
        old_name = self._state.name
        if self._repo:
            self._repo.record_transition(
                incident_id=self.incident_id,
                from_state=old_name,
                to_state=new_state.name,
            )
        self._state = new_state
        if self._repo:
            self._repo.save(self)  # Persist after every transition

    @property
    def state(self) -> str:
        return self._state.name

    # ── Public transition methods (delegate to current state) ──────────
    def start_investigation(self, llm_diagnosis: dict):
        self._state.to_investigating(self, llm_diagnosis)

    def mark_resolved(self, patch: dict, approved_by: str):
        self._state.to_resolved(self, patch, approved_by)

    def close(self, rca_summary: str):
        self._state.to_closed(self, rca_summary)

    def reopen_investigation(self, llm_diagnosis: dict):
        """Called when a patch didn't fix the issue."""
        self._state.to_investigating(self, llm_diagnosis)
```

---

### Component 3: Grafana Dashboards

#### [NEW] `k8s/grafana-deployment.yaml`
Grafana 10.x deployed in `monitoring` namespace with PostgreSQL datasource pre-configured.

#### [NEW] `grafana/dashboards/sre-overview.json`
Main SRE operations dashboard.

#### [NEW] `grafana/dashboards/incident-detail.json`
Per-incident state timeline dashboard.

**Dashboard panels:**

```
┌─────────────────────────────────────────────────────────────────┐
│  SRE Agent Overview Dashboard                                   │
├──────────┬──────────────┬──────────────┬────────────────────────┤
│  OPEN    │ INVESTIGATING │   RESOLVED   │       CLOSED           │
│   [N]    │     [N]       │     [N]      │        [N]             │
│  (red)   │  (orange)     │  (yellow)    │      (green)           │
├──────────┴──────────────┴──────────────┴────────────────────────┤
│  INCIDENT TABLE (real-time, sortable)                           │
│  ID | Deployment | Error | State | MTTR | Opened At | Actions   │
│  INC-0047 | auth-svc | CrashLoop | Investigating | - | 5m ago   │
│  INC-0046 | pay-gw | OOMKilled | Closed | 8min | 2h ago        │
├─────────────────────────┬───────────────────────────────────────┤
│  MTTR Trend (line)      │  Error Type Distribution (pie)       │
│  Last 30 days by day    │  CrashLoop / OOM / Pending / etc.    │
├─────────────────────────┼───────────────────────────────────────┤
│  Top 5 Recurring        │  Incident Heatmap                    │
│  Deployments (bar)      │  Hour of day × Day of week           │
├─────────────────────────┴───────────────────────────────────────┤
│  State Transition Timeline (per-incident Gantt view)           │
│  INC-0047: ●Open(0s)──●Investigating(45s)──●Resolved(8m)──●    │
└─────────────────────────────────────────────────────────────────┘
```

**Key Grafana SQL queries:**

```sql
-- Panel: Incident count by state (stat panels)
SELECT state, COUNT(*) as count
FROM incidents
GROUP BY state;

-- Panel: MTTR trend over 30 days
SELECT
  DATE_TRUNC('day', closed_at) as day,
  AVG(mttr_seconds) / 60 as avg_mttr_minutes,
  MIN(mttr_seconds) / 60 as min_mttr_minutes,
  MAX(mttr_seconds) / 60 as max_mttr_minutes
FROM incidents
WHERE state = 'Closed' AND closed_at >= NOW() - INTERVAL '30 days'
GROUP BY DATE_TRUNC('day', closed_at)
ORDER BY day;

-- Panel: Top recurring deployments
SELECT target_deployment, SUM(recurrence_count) as total_incidents
FROM incidents
GROUP BY target_deployment
ORDER BY total_incidents DESC
LIMIT 10;

-- Panel: Error type distribution
SELECT error_state, COUNT(*) as count
FROM incidents
WHERE opened_at >= NOW() - INTERVAL '7 days'
GROUP BY error_state
ORDER BY count DESC;

-- Panel: Incident heatmap (hour × day of week)
SELECT
  EXTRACT(DOW FROM opened_at) as dow,
  EXTRACT(HOUR FROM opened_at) as hour,
  COUNT(*) as incident_count
FROM incidents
GROUP BY dow, hour;

-- Panel: State transition timeline (per incident)
SELECT incident_id, from_state, to_state, transitioned_at,
       EXTRACT(EPOCH FROM (transitioned_at - LAG(transitioned_at)
         OVER (PARTITION BY incident_id ORDER BY transitioned_at))) as duration_seconds
FROM state_transitions
WHERE incident_id = '${incident_id}'
ORDER BY transitioned_at;
```

---

### Component 4: Kopf Controller (Updated)

#### [MODIFY] `controller/main.py`
Integrate state machine + dual writes (CRD + PostgreSQL).

#### [NEW] `controller/log_preprocessor.py`
Log cleaning, fingerprinting, dedup logic.

#### [NEW] `controller/ollama_client.py`
Async Ollama HTTP client with the memory-augmented prompt.

#### [NEW] `controller/notifier.py`
Slack Block Kit + SMTP alerting with history-aware messages.

#### [NEW] `controller/outcome_checker.py`
15-minute post-patch verification loop.

---

### Component 5: Kubernetes Manifests

#### [NEW] `k8s/namespace.yaml`
`monitoring` and `ai-infra` namespaces.

#### [NEW] `k8s/rbac.yaml`
Two ServiceAccounts: `sre-observer` (read) and `sre-executor` (write).

#### [NEW] `k8s/crds/patch-request-crd.yaml`
`PatchRequest` CRD definition.

#### [NEW] `k8s/crds/incident-record-crd.yaml`
`IncidentRecord` CRD definition (K8s-native view; PostgreSQL is source of truth for analytics).

#### [NEW] `k8s/ollama-statefulset.yaml`
Ollama + 60Gi PVC + node taint toleration.

#### [NEW] `k8s/network-policy.yaml`
Block all egress from `ai-infra` namespace.

#### [NEW] `k8s/priority-classes.yaml`
`production-critical` > `sre-monitoring` > `ai-low-priority`

---

### Component 6: Project Structure

```
k8s-sre-agent/
│
├── controller/                  # Kopf operator
│   ├── main.py                  # Entry point, Kopf handlers
│   ├── incident.py              # Incident domain object
│   ├── states.py                # State Pattern classes
│   ├── log_preprocessor.py      # Log cleaning + fingerprinting
│   ├── ollama_client.py         # Async Ollama HTTP client
│   ├── notifier.py              # Slack + Email alerting
│   ├── outcome_checker.py       # Post-patch verification
│   └── dedup.py                 # 3-layer dedup logic
│
├── db/
│   ├── schema.sql               # PostgreSQL schema
│   ├── models.py                # SQLAlchemy ORM models
│   ├── repository.py            # IncidentRepository (save, query)
│   └── migrations/              # Alembic migration files
│
├── grafana/
│   ├── dashboards/
│   │   ├── sre-overview.json    # Main operations dashboard
│   │   └── incident-detail.json # Per-incident timeline
│   └── datasources/
│       └── postgres.yaml        # Auto-provisioned datasource
│
├── k8s/
│   ├── namespace.yaml
│   ├── rbac.yaml
│   ├── network-policy.yaml
│   ├── priority-classes.yaml
│   ├── crds/
│   │   ├── patch-request-crd.yaml
│   │   └── incident-record-crd.yaml
│   ├── ollama-statefulset.yaml
│   ├── postgres-statefulset.yaml
│   ├── grafana-deployment.yaml
│   └── controller-deployment.yaml
│
├── helm/                        # Phase 5: Package everything
│   └── k8s-sre-agent/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│
├── tests/
│   ├── test_states.py           # Unit tests for state machine
│   ├── test_dedup.py            # Unit tests for dedup logic
│   └── test_preprocessor.py     # Unit tests for log cleaning
│
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Verification Plan

### Automated Tests
```bash
# State machine unit tests
pytest tests/test_states.py -v

# Test invalid transitions are rejected
pytest tests/test_states.py::test_open_cannot_close -v

# Test RCA validation is transactional
pytest tests/test_states.py::test_rca_validation -v

# Test dedup layers
pytest tests/test_dedup.py -v

# Integration test: end-to-end incident flow
pytest tests/test_integration.py -v
```

### Manual Verification
1. Deploy to local `kind` or `minikube` cluster
2. Deliberately crash a pod: `kubectl delete pod <name>` in a loop
3. Verify: Slack alert fires, `PatchRequest` created, `IncidentRecord` created
4. Verify: Grafana dashboard shows incident in "Open" → "Investigating" state
5. Approve the patch: `kubectl patch pr ...`
6. Verify: Dashboard moves to "Resolved", MTTR recorded
7. Add RCA: `kubectl patch inc ...` → verify moves to "Closed"
8. Crash same pod again → verify "SEEN BEFORE" in Slack alert

---

## Phased Build Plan (9 Weeks)

```
Week 1-2: Foundation
  [ ] PostgreSQL StatefulSet + schema.sql applied
  [ ] SQLAlchemy models + IncidentRepository
  [ ] Alembic migrations setup
  [ ] CRD definitions applied to cluster
  [ ] RBAC + NetworkPolicy manifests

Week 3-4: State Machine + Controller Core
  [ ] states.py (all 4 states + transitions)
  [ ] incident.py (domain object)
  [ ] main.py Kopf handlers (Open → Investigating flow)
  [ ] log_preprocessor.py (cleaning + fingerprinting)
  [ ] dedup.py (3-layer dedup)
  [ ] Unit tests: test_states.py, test_dedup.py

Week 5: Ollama Integration
  [ ] Ollama StatefulSet deployed
  [ ] deepseek-coder:6.7b model pulled and persisted
  [ ] ollama_client.py (async, memory-augmented prompt)
  [ ] End-to-end: crash pod → LLM diagnosis → PatchRequest created

Week 6: Notifier + Patch Executor
  [ ] notifier.py (Slack Block Kit + SMTP)
  [ ] Slack history-aware alert format
  [ ] outcome_checker.py (15-min post-patch loop)
  [ ] Patch Executor (separate Kopf loop, restricted SA)
  [ ] Full flow: Open → Investigating → Resolved → Closed

Week 7: Grafana Dashboards
  [ ] Grafana deployment + PostgreSQL datasource provisioning
  [ ] sre-overview.json dashboard (4 stat panels + table + charts)
  [ ] incident-detail.json dashboard (state transition timeline)
  [ ] All SQL queries validated against real incident data

Week 8: Hardening
  [ ] OOMKilled immediate-trigger (no dampening)
  [ ] likely_recurring → 4h cache TTL
  [ ] Escalation nudges (10x, 25x, 50x seen_count)
  [ ] Auto-runbook ConfigMap weekly CronJob
  [ ] Node taint + PriorityClass applied
  [ ] End-to-end integration tests

Week 9: Packaging
  [ ] Helm chart (k8s-sre-agent)
  [ ] values.yaml with all tuneable params
  [ ] README with quickstart
  [ ] One-command deploy: helm install sre-agent ./helm/k8s-sre-agent
```

---

## Memory Budget (Final — 16GB RAM)

| Component | RAM |
|---|---|
| K8s control plane | ~2.5 GB |
| System pods | ~300 MB |
| Ollama (deepseek-coder:6.7b Q4) | ~4.5 GB |
| PostgreSQL 15 | ~200 MB |
| Grafana | ~200 MB |
| Kopf Controller | ~200 MB |
| Patch Executor | ~100 MB |
| Notifier | ~100 MB |
| Your workloads | ~4–5 GB |
| **Buffer** | **~1.9 GB** |
| **Total** | **~14.1 GB** ✅ |
