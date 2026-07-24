"""
states.py — State Pattern implementation for the Incident lifecycle.

Strict rules (enforced by raising exceptions on illegal transitions):
    OPEN        → INVESTIGATING only
    INVESTIGATING → RESOLVED (patch approved) | OPEN (PR rejected, re-queues)
    RESOLVED    → CLOSED (RCA validated) | INVESTIGATING (patch failed)
    CLOSED      → terminal. No further transitions.
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.incident import Incident

logger = logging.getLogger(__name__)


# ── Custom Exceptions ──────────────────────────────────────────────────────────

class InvalidTransitionError(Exception):
    """Raised when a caller attempts a state transition that is not allowed."""


class RCAValidationError(Exception):
    """Raised when RCA fields do not meet the minimum requirements for CLOSED."""


# ── Abstract Base ──────────────────────────────────────────────────────────────

class State(ABC):
    """Abstract base class for all incident states."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def to_investigating(self, incident: "Incident", llm_diagnosis: dict) -> None: ...

    @abstractmethod
    def to_resolved(self, incident: "Incident", patch: dict, approved_by: str) -> None: ...

    @abstractmethod
    def to_closed(self, incident: "Incident", rca_summary: str) -> None: ...

    def _deny(self, target: str, incident: "Incident"):
        raise InvalidTransitionError(
            f"[{incident.incident_id}] Illegal transition: "
            f"{self.name!r} → {target!r}. See state diagram for allowed transitions."
        )


# ── Concrete States ────────────────────────────────────────────────────────────

class OpenState(State):
    name = "Open"

    def to_investigating(self, incident: "Incident", llm_diagnosis: dict) -> None:
        logger.info("[%s] Open → Investigating", incident.incident_id)
        incident._set_state(InvestigatingState(), reason="LLM diagnosis received")
        incident.llm_diagnosis = llm_diagnosis
        incident.investigating_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved", incident)

    def to_closed(self, incident, rca_summary):
        self._deny("Closed", incident)


class InvestigatingState(State):
    name = "Investigating"

    def to_investigating(self, incident: "Incident", llm_diagnosis: dict) -> None:
        # Re-diagnosis is allowed (e.g. after a PR was rejected)
        logger.info("[%s] Re-diagnosing (still Investigating)", incident.incident_id)
        incident.llm_diagnosis = llm_diagnosis

    def to_resolved(self, incident: "Incident", patch: dict, approved_by: str) -> None:
        if not patch:
            raise RCAValidationError(
                f"[{incident.incident_id}] A non-empty patch is required to move to Resolved."
            )
        if not approved_by:
            raise RCAValidationError(
                f"[{incident.incident_id}] approved_by must be set — auto-patches are not allowed."
            )
        logger.info("[%s] Investigating → Resolved (approved by %s)", incident.incident_id, approved_by)
        incident._set_state(ResolvedState(), reason=f"Patch approved by {approved_by}")
        incident.patch_applied = patch
        incident.approved_by = approved_by
        incident.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_closed(self, incident, rca_summary):
        self._deny("Closed", incident)


class ResolvedState(State):
    name = "Resolved"

    def to_investigating(self, incident: "Incident", llm_diagnosis: dict) -> None:
        # Patch didn't fix it — cycle back for re-diagnosis
        logger.warning("[%s] Resolved → Investigating (patch failed, re-diagnosing)", incident.incident_id)
        incident._set_state(InvestigatingState(), reason="Patch failed, patch outcome check returned failure")
        incident.llm_diagnosis = llm_diagnosis
        incident.patch_applied = None
        incident.resolved_at = None

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved", incident)  # Already resolved

    def to_closed(self, incident: "Incident", rca_summary: str) -> None:
        """
        Transactional RCA Validation — ALL conditions must be met atomically.
        If any fail, the transition is rejected and the incident stays in Resolved.
        """
        errors = []
        if not rca_summary or len(rca_summary.strip()) < 30:
            errors.append("rca_summary must be at least 30 characters.")
        if not incident.worked:
            errors.append("Cannot close: outcome not yet verified (worked must be True).")
        if not incident.approved_by:
            errors.append("Cannot close: approved_by is not set.")
        if errors:
            raise RCAValidationError(
                f"[{incident.incident_id}] RCA validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        logger.info("[%s] Resolved → Closed", incident.incident_id)
        incident._set_state(ClosedState(), reason="RCA validated and accepted")
        incident.rca_summary = rca_summary.strip()
        incident.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if incident.opened_at and incident.closed_at:
            incident.mttr_seconds = int(
                (incident.closed_at - incident.opened_at).total_seconds()
            )


class ClosedState(State):
    name = "Closed"

    def to_investigating(self, incident, llm_diagnosis):
        self._deny("Investigating", incident)  # Terminal state

    def to_resolved(self, incident, patch, approved_by):
        self._deny("Resolved", incident)  # Terminal state

    def to_closed(self, incident, rca_summary):
        self._deny("Closed", incident)  # Already closed


# ── Factory ────────────────────────────────────────────────────────────────────

_STATE_MAP: dict[str, State] = {
    "Open": OpenState(),
    "Investigating": InvestigatingState(),
    "Resolved": ResolvedState(),
    "Closed": ClosedState(),
}


def state_from_name(name: str) -> State:
    """Reconstruct a State object from a persisted state name string."""
    if name not in _STATE_MAP:
        raise ValueError(f"Unknown state name: {name!r}")
    return _STATE_MAP[name]
