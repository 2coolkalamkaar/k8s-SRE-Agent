"""
incident.py — Incident domain object.

Wraps the state machine and acts as the single source of truth for an
incident's lifecycle. All state transitions are delegated to the current
State object, which enforces the allowed transitions.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from controller.states import OpenState, State, state_from_name

logger = logging.getLogger(__name__)


@dataclass
class Incident:
    """
    Represents a single in-cluster incident from detection to closure.

    The _state field drives the lifecycle; all transitions go through
    the public helpers (start_investigation, mark_resolved, close, reopen).
    Direct mutation of _state is only done inside State subclasses via
    _set_state(), which also fires the optional persistence callback.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    incident_id: str                    # INC-2026-0047
    error_state: str                    # CrashLoopBackOff | OOMKilled | etc.
    error_fingerprint: str              # SHA-256 hex (first 16 chars)
    target_deployment: str
    target_namespace: str

    # ── Diagnosis data (populated through transitions) ─────────────────────
    llm_diagnosis: Optional[dict] = None
    patch_applied: Optional[dict] = None
    approved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    rca_summary: Optional[str] = None
    worked: Optional[bool] = None       # Set by outcome_checker after 15 min
    tags: list = field(default_factory=list)

    # ── Timestamps ─────────────────────────────────────────────────────────
    opened_at: Optional[datetime] = None
    investigating_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    mttr_seconds: Optional[int] = None
    mttd_seconds: Optional[int] = None

    # ── Internal ───────────────────────────────────────────────────────────
    _state: State = field(default_factory=OpenState, repr=False)
    # Optional async persistence callback: called on every state transition.
    # Signature: async def on_transition(incident: Incident, from_state: str, to_state: str)
    _on_transition: Optional[Callable] = field(default=None, repr=False)

    def __post_init__(self):
        if self.opened_at is None:
            self.opened_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── Internal state-setter (called only by State subclasses) ───────────
    def _set_state(self, new_state: State, reason: str = "") -> None:
        old_name = self._state.name
        self._state = new_state
        logger.debug(
            "[%s] State changed: %s → %s | reason=%s",
            self.incident_id, old_name, new_state.name, reason,
        )
        # Fire the persistence callback if registered (non-blocking; awaited by caller)
        if self._on_transition:
            # Store the pending callback for the outer async context to await.
            # We can't await here (dataclass __post_init__ is sync), so we
            # expose it and the caller awaits _flush_transition().
            self._pending_transition = (old_name, new_state.name, reason)

    # ── Public state query ─────────────────────────────────────────────────
    @property
    def state(self) -> str:
        return self._state.name

    # ── Public transition API ──────────────────────────────────────────────
    def start_investigation(self, llm_diagnosis: dict) -> None:
        """Open → Investigating (or re-diagnose while Investigating)."""
        self._state.to_investigating(self, llm_diagnosis)

    def mark_resolved(self, patch: dict, approved_by: str) -> None:
        """Investigating → Resolved."""
        self._state.to_resolved(self, patch, approved_by)

    def close(self, rca_summary: str) -> None:
        """Resolved → Closed (transactional RCA validation enforced)."""
        self._state.to_closed(self, rca_summary)

    def reopen_investigation(self, llm_diagnosis: dict) -> None:
        """Resolved → Investigating (patch didn't work)."""
        self._state.to_investigating(self, llm_diagnosis)

    # ── Serialisation helpers ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        """Flat dict suitable for CRD spec or PostgreSQL row."""
        return {
            "incident_id": self.incident_id,
            "state": self.state,
            "error_state": self.error_state,
            "error_fingerprint": self.error_fingerprint,
            "target_deployment": self.target_deployment,
            "target_namespace": self.target_namespace,
            "llm_diagnosis": self.llm_diagnosis,
            "patch_applied": self.patch_applied,
            "approved_by": self.approved_by,
            "resolution_notes": self.resolution_notes,
            "rca_summary": self.rca_summary,
            "worked": self.worked,
            "tags": self.tags,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "investigating_at": self.investigating_at.isoformat() if self.investigating_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "mttr_seconds": self.mttr_seconds,
            "mttd_seconds": self.mttd_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Incident":
        """Reconstruct an Incident from a persisted dict (e.g. loaded from PostgreSQL)."""
        inc = cls(
            incident_id=data["incident_id"],
            error_state=data["error_state"],
            error_fingerprint=data["error_fingerprint"],
            target_deployment=data["target_deployment"],
            target_namespace=data["target_namespace"],
        )
        inc._state = state_from_name(data.get("state", "Open"))
        inc.llm_diagnosis = data.get("llm_diagnosis")
        inc.patch_applied = data.get("patch_applied")
        inc.approved_by = data.get("approved_by")
        inc.rca_summary = data.get("rca_summary")
        inc.worked = data.get("worked")
        inc.tags = data.get("tags", [])
        if data.get("opened_at"):
            inc.opened_at = datetime.fromisoformat(data["opened_at"])
        if data.get("investigating_at"):
            inc.investigating_at = datetime.fromisoformat(data["investigating_at"])
        if data.get("resolved_at"):
            inc.resolved_at = datetime.fromisoformat(data["resolved_at"])
        if data.get("closed_at"):
            inc.closed_at = datetime.fromisoformat(data["closed_at"])
        inc.mttr_seconds = data.get("mttr_seconds")
        return inc
