"""
db.py — PostgreSQL persistence for incidents.

Owns a single asyncpg connection pool for the controller process.
This is the write path for incident history (and, in a later phase,
the RAG read/write path once pgvector embeddings are added).
"""

from __future__ import annotations
import logging
import os
from typing import Optional

from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _dsn() -> str:
    host = os.getenv("PGHOST", "postgres-svc.monitoring.svc.cluster.local")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "sreagent")
    password = os.getenv("PGPASSWORD", "")
    database = os.getenv("PGDATABASE", "sredb")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def init_db_pool() -> None:
    """Create the connection pool. Safe to call once at controller startup."""
    global _pool
    if _pool is not None:
        return
    try:
        _pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=5)
        logger.info("✅ Postgres pool initialised (%s)", os.getenv("PGHOST", "postgres-svc"))
    except Exception as exc:
        logger.error("Failed to initialise Postgres pool: %s", exc)
        _pool = None


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> Optional[asyncpg.Pool]:
    if _pool is None:
        logger.warning("Postgres pool not initialised — skipping DB write")
    return _pool


async def save_incident(incident, embedding: list[float] | None = None) -> None:
    """Upsert an incident row from an Incident domain object (incident.to_dict())."""
    pool = _get_pool()
    if pool is None:
        return
    data = incident.to_dict()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, state, error_state, error_fingerprint,
                    target_deployment, target_namespace, root_cause,
                    llm_diagnosis, patch_applied, approved_by,
                    resolution_notes, rca_summary, worked, tags,
                    opened_at, investigating_at, resolved_at, closed_at,
                    mttr_seconds, mttd_seconds, embedding, updated_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21::vector, NOW())
                ON CONFLICT (incident_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    root_cause = EXCLUDED.root_cause,
                    llm_diagnosis = EXCLUDED.llm_diagnosis,
                    patch_applied = EXCLUDED.patch_applied,
                    approved_by = EXCLUDED.approved_by,
                    resolution_notes = EXCLUDED.resolution_notes,
                    rca_summary = EXCLUDED.rca_summary,
                    worked = EXCLUDED.worked,
                    tags = EXCLUDED.tags,
                    investigating_at = EXCLUDED.investigating_at,
                    resolved_at = EXCLUDED.resolved_at,
                    closed_at = EXCLUDED.closed_at,
                    mttr_seconds = EXCLUDED.mttr_seconds,
                    mttd_seconds = EXCLUDED.mttd_seconds,
                    embedding = COALESCE(EXCLUDED.embedding, incidents.embedding),
                    updated_at = NOW()
                """,
                data["incident_id"], data["state"], data["error_state"], data["error_fingerprint"],
                data["target_deployment"], data["target_namespace"],
                (data.get("llm_diagnosis") or {}).get("root_cause"),
                _to_json(data.get("llm_diagnosis")), _to_json(data.get("patch_applied")),
                data.get("approved_by"), data.get("resolution_notes"), data.get("rca_summary"),
                data.get("worked"), data.get("tags") or [],
                _to_dt(data.get("opened_at")), _to_dt(data.get("investigating_at")),
                _to_dt(data.get("resolved_at")), _to_dt(data.get("closed_at")),
                data.get("mttr_seconds"), data.get("mttd_seconds"), _to_vector(embedding),
            )
    except Exception as exc:
        logger.error("[db] Failed to save incident %s: %s", data.get("incident_id"), exc)


async def mark_incident_outcome(incident_id: str, worked: bool, mttr_seconds: int | None = None) -> None:
    """Lightweight update from outcome_checker — avoids re-serialising the whole Incident."""
    pool = _get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE incidents
                SET worked = $2,
                    state = CASE WHEN $2 THEN 'Closed' ELSE state END,
                    mttr_seconds = COALESCE($3, mttr_seconds),
                    closed_at = CASE WHEN $2 THEN NOW() ELSE closed_at END,
                    updated_at = NOW()
                WHERE incident_id = $1
                """,
                incident_id, worked, mttr_seconds,
            )
    except Exception as exc:
        logger.error("[db] Failed to mark outcome for %s: %s", incident_id, exc)


async def save_applied_patch(incident_id: str, patch: dict, approved_by: str) -> None:
    """Called right after the executor applies a patch — this is what RAG later reuses."""
    pool = _get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE incidents
                SET patch_applied = $2::jsonb,
                    approved_by = $3,
                    state = 'Resolved',
                    resolved_at = NOW(),
                    updated_at = NOW()
                WHERE incident_id = $1
                """,
                incident_id, _to_json(patch), approved_by,
            )
    except Exception as exc:
        logger.error("[db] Failed to save applied patch for %s: %s", incident_id, exc)


RAG_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.75"))


async def find_similar_incident(embedding: list[float], error_state: str) -> dict | None:
    """
    RAG read path: look for a past incident that means almost the same thing
    as this one, filtered to the same error type and only among fixes that
    actually worked. Returns None if nothing clears the similarity bar.
    """
    pool = _get_pool()
    if pool is None or embedding is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT incident_id, root_cause, llm_diagnosis, patch_applied,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM incidents
                WHERE error_state = $2
                  AND worked = true
                  AND embedding IS NOT NULL
                  AND patch_applied IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                _to_vector(embedding), error_state,
            )
    except Exception as exc:
        logger.error("[db] RAG similarity search failed: %s", exc)
        return None

    if row is None or row["similarity"] < RAG_SIMILARITY_THRESHOLD:
        return None

    import json
    llm_diagnosis = json.loads(row["llm_diagnosis"]) if row["llm_diagnosis"] else {}
    patch_applied = json.loads(row["patch_applied"]) if row["patch_applied"] else {}
    return {
        "incident_id": row["incident_id"],
        "root_cause": row["root_cause"],
        "severity": llm_diagnosis.get("severity", "high"),
        "patch_applied": patch_applied,
        "similarity": float(row["similarity"]),
    }


def _to_json(value):
    import json
    return json.dumps(value) if value is not None else None


def _to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_vector(embedding: list[float] | None) -> str | None:
    """pgvector's text input format: '[v1,v2,...]'. Cast to ::vector in SQL."""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
