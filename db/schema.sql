-- K8s SRE Agent PostgreSQL Database Schema

-- Enables the `vector` type used below for RAG similarity search.
-- Requires the pgvector extension to be present in the Postgres image
-- (see k8s/postgres-statefulset.yaml — pgvector/pgvector:pg15).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    id                SERIAL PRIMARY KEY,
    incident_id       VARCHAR(20) UNIQUE NOT NULL,    -- INC-2026-0047
    state             VARCHAR(20) NOT NULL DEFAULT 'Open',
    error_state       VARCHAR(50) NOT NULL,            -- CrashLoopBackOff, OOMKilled, etc.
    error_fingerprint VARCHAR(64) NOT NULL,
    target_deployment VARCHAR(100) NOT NULL,
    target_namespace  VARCHAR(100) NOT NULL,
    root_cause        TEXT,
    llm_diagnosis     JSONB,
    patch_applied     JSONB,
    approved_by       VARCHAR(100),
    resolution_notes  TEXT,
    rca_summary       TEXT,                            -- Required for CLOSED state
    worked            BOOLEAN,
    mttd_seconds      INTEGER,
    mttr_seconds      INTEGER,
    recurrence_count  INTEGER DEFAULT 1,
    tags              TEXT[],
    embedding         vector(384),                     -- RAG: semantic embedding of cleaned crash logs
    opened_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    investigating_at  TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Full audit log of every state transition
CREATE TABLE IF NOT EXISTS state_transitions (
    id              SERIAL PRIMARY KEY,
    incident_id     VARCHAR(20) NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
    from_state      VARCHAR(20),
    to_state        VARCHAR(20) NOT NULL,
    triggered_by    VARCHAR(100),                    -- 'system' or 'rahul@company.com'
    reason          TEXT,
    metadata        JSONB,
    transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Each distinct patch proposed by the LLM
CREATE TABLE IF NOT EXISTS patch_requests (
    id              SERIAL PRIMARY KEY,
    pr_name         VARCHAR(100) UNIQUE NOT NULL,    -- K8s CRD name
    incident_id     VARCHAR(20) REFERENCES incidents(incident_id) ON DELETE CASCADE,
    proposed_patch  JSONB NOT NULL,
    approval_state  VARCHAR(20) DEFAULT 'Pending',
    approved_by     VARCHAR(100),
    applied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Time-series metrics for Grafana graphs
CREATE TABLE IF NOT EXISTS incident_metrics (
    id              SERIAL PRIMARY KEY,
    incident_id     VARCHAR(20) REFERENCES incidents(incident_id) ON DELETE CASCADE,
    metric_name     VARCHAR(50) NOT NULL,            -- 'restart_count', 'cpu_usage'
    metric_value    FLOAT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hash-chained, append-only audit log. Every human approval and every
-- automatic action (rollback, auto-close) the agent takes gets one row
-- here. entry_hash is computed from the row's own contents PLUS the
-- previous row's entry_hash — tampering with any past row breaks the hash
-- of every row after it, which is what makes this tamper-EVIDENT rather
-- than just "a table nobody is supposed to edit." See controller/audit.py.
-- This table is intentionally never UPDATEd or DELETEd from by the
-- application — only ever INSERTed into.
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    entry_hash      VARCHAR(64) UNIQUE NOT NULL,
    prev_hash       VARCHAR(64) NOT NULL,
    incident_id     VARCHAR(20),
    action_type     VARCHAR(50) NOT NULL,            -- 'approval', 'rollback', 'auto_close'
    actor           VARCHAR(100) NOT NULL,           -- human email, or 'system' for automatic actions
    reason          TEXT,
    payload         JSONB,
    recorded_at     TIMESTAMPTZ NOT NULL
);

-- Indexes for query & dashboard performance
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state);
CREATE INDEX IF NOT EXISTS idx_incidents_opened_at ON incidents(opened_at);
CREATE INDEX IF NOT EXISTS idx_incidents_deployment ON incidents(target_deployment);
CREATE INDEX IF NOT EXISTS idx_incidents_error ON incidents(error_state);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(error_fingerprint);
CREATE INDEX IF NOT EXISTS idx_audit_log_incident ON audit_log(incident_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_recorded_at ON audit_log(recorded_at);
CREATE INDEX IF NOT EXISTS idx_transitions_incident ON state_transitions(incident_id);
