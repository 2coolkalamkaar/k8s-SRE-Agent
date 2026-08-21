"""
audit.py — Hash-chained, append-only audit log.

Why this exists (in plain words):
    Every human approval, and every automatic action the agent takes on its
    own (a rollback, an auto-close), gets one row here. A plain database
    table only stops *accidental* edits, and only if nobody with database
    access is willing to bypass that. This log is HASH-CHAINED: each row's
    hash is computed from its own contents PLUS the previous row's hash. If
    anyone edits a past row — even someone with full database access — the
    hash of every row after it stops matching. verify_chain() walks the
    whole table and reports exactly where a break happened, if any.

    This module never UPDATEs or DELETEs a row. It only ever inserts.
"""

from __future__ import annotations
import hashlib
import json
import logging
from datetime import datetime, timezone

import controller.db as db

logger = logging.getLogger(__name__)

# prev_hash for the very first row that will ever exist in this table.
GENESIS_HASH = "0" * 64


def _canonical_ts(dt: datetime) -> str:
    """
    Format a datetime the same way regardless of whether it came from
    datetime.now() just now (naive or tz-aware) or was just read back out
    of Postgres (asyncpg returns tz-aware UTC datetimes for TIMESTAMPTZ
    columns). Explicit strftime — not .isoformat() — because isoformat()
    conditionally appends a +00:00/UTC offset only when tzinfo is set,
    which would silently produce two different strings for "the same
    instant" depending on which code path constructed the datetime,
    breaking hash verification even with zero tampering.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _compute_hash(
    prev_hash: str, incident_id: str | None, action_type: str,
    actor: str, reason: str, payload: dict, recorded_at: str,
) -> str:
    """Deterministic hash over one entry's contents + the hash before it.
    sort_keys=True so the same logical entry always hashes the same way
    regardless of dict insertion order."""
    canonical = json.dumps({
        "prev_hash": prev_hash,
        "incident_id": incident_id,
        "action_type": action_type,
        "actor": actor,
        "reason": reason,
        "payload": payload,
        "recorded_at": recorded_at,
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def record(
    incident_id: str | None, action_type: str, actor: str,
    reason: str = "", payload: dict | None = None,
) -> None:
    """
    Append one entry to the audit log.

    Reads the current last row's hash and this new row's hash inside a
    single transaction (with FOR UPDATE locking the last row) so two
    concurrent writes can't both read the same prev_hash and silently fork
    the chain into two valid-looking branches.
    """
    pool = db.get_pool()
    if pool is None:
        logger.warning(
            "[audit] DB pool not available — entry NOT recorded (incident=%s, action=%s). "
            "This is a real gap, not a soft failure: if this happens in production, an "
            "approval or rollback just went unlogged.",
            incident_id, action_type,
        )
        return

    payload = payload or {}
    recorded_at_dt = datetime.now(timezone.utc)
    recorded_at_str = _canonical_ts(recorded_at_dt)

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE"
                )
                prev_hash = row["entry_hash"] if row else GENESIS_HASH
                entry_hash = _compute_hash(
                    prev_hash, incident_id, action_type, actor, reason, payload, recorded_at_str
                )
                await conn.execute(
                    """
                    INSERT INTO audit_log
                        (entry_hash, prev_hash, incident_id, action_type, actor, reason, payload, recorded_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                    """,
                    entry_hash, prev_hash, incident_id, action_type, actor, reason,
                    json.dumps(payload), recorded_at_dt,
                )
        logger.info(
            "[audit] Recorded %s for %s by %s (hash=%s…)",
            action_type, incident_id or "-", actor, entry_hash[:12],
        )
    except Exception as exc:
        logger.error("[audit] Failed to record entry: %s", exc)


async def verify_chain() -> tuple[bool, dict | None]:
    """
    Walk the entire audit log in order and confirm every row's entry_hash
    is exactly what recomputing it from that row's own stored contents (and
    the previous row's stored hash) produces.

    Returns (True, None) if the whole chain is intact, or (False, row) for
    the FIRST row where it isn't — recomputing its hash from its own stored
    fields doesn't match what's stored, meaning either that row or the one
    before it was altered after the fact.
    """
    pool = db.get_pool()
    if pool is None:
        logger.warning("[audit] DB pool not available — cannot verify chain")
        return False, None

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, entry_hash, prev_hash, incident_id, action_type, actor, "
                "reason, payload, recorded_at FROM audit_log ORDER BY id ASC"
            )
    except Exception as exc:
        logger.error("[audit] Failed to read audit log for verification: %s", exc)
        return False, None

    expected_prev = GENESIS_HASH
    for row in rows:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else (row["payload"] or {})
        recomputed = _compute_hash(
            expected_prev, row["incident_id"], row["action_type"], row["actor"],
            row["reason"] or "", payload, _canonical_ts(row["recorded_at"]),
        )
        if recomputed != row["entry_hash"] or row["prev_hash"] != expected_prev:
            logger.error("[audit] Chain broken at row id=%s (incident=%s)", row["id"], row["incident_id"])
            return False, dict(row)
        expected_prev = row["entry_hash"]

    logger.info("[audit] Chain verified intact — %d entries", len(rows))
    return True, None
