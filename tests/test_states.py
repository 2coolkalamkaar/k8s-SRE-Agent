"""
test_states.py — Unit tests for the State Pattern (no K8s cluster required).
"""

import pytest
from controller.incident import Incident
from controller.states import (
    ClosedState, InvestigatingState, InvalidTransitionError, OpenState,
    RCAValidationError, ResolvedState,
)


def make_incident(**kwargs) -> Incident:
    defaults = {
        "incident_id": "INC-TEST-0001",
        "error_state": "CrashLoopBackOff",
        "error_fingerprint": "abc123def456",
        "target_deployment": "auth-service",
        "target_namespace": "production",
    }
    defaults.update(kwargs)
    return Incident(**defaults)


# ── Open State ─────────────────────────────────────────────────────────────────

class TestOpenState:
    def test_open_can_transition_to_investigating(self):
        inc = make_incident()
        assert inc.state == "Open"
        inc.start_investigation({"root_cause": "Missing secret"})
        assert inc.state == "Investigating"

    def test_open_cannot_transition_to_resolved(self):
        inc = make_incident()
        with pytest.raises(InvalidTransitionError):
            inc.mark_resolved({"patch": "data"}, "rahul@company.com")

    def test_open_cannot_transition_to_closed(self):
        inc = make_incident()
        with pytest.raises(InvalidTransitionError):
            inc.close("Some RCA text that is long enough to meet the minimum requirement here")


# ── Investigating State ────────────────────────────────────────────────────────

class TestInvestigatingState:
    def setup_method(self):
        self.inc = make_incident()
        self.inc.start_investigation({"root_cause": "Missing secret"})

    def test_investigating_can_rediagnose(self):
        self.inc.start_investigation({"root_cause": "Updated diagnosis"})
        assert self.inc.state == "Investigating"
        assert self.inc.llm_diagnosis["root_cause"] == "Updated diagnosis"

    def test_investigating_can_transition_to_resolved(self):
        self.inc.mark_resolved({"patch": "increase memory"}, "rahul@company.com")
        assert self.inc.state == "Resolved"
        assert self.inc.approved_by == "rahul@company.com"

    def test_investigating_cannot_resolve_without_patch(self):
        with pytest.raises(RCAValidationError):
            self.inc.mark_resolved({}, "rahul@company.com")

    def test_investigating_cannot_resolve_without_approver(self):
        with pytest.raises(RCAValidationError):
            self.inc.mark_resolved({"patch": "data"}, "")

    def test_investigating_cannot_close_directly(self):
        with pytest.raises(InvalidTransitionError):
            self.inc.close("Some long enough RCA summary text here for testing purposes.")


# ── Resolved State ─────────────────────────────────────────────────────────────

class TestResolvedState:
    def setup_method(self):
        self.inc = make_incident()
        self.inc.start_investigation({"root_cause": "OOMKilled due to low memory limit"})
        self.inc.mark_resolved({"patch": "increase memory to 256Mi"}, "rahul@company.com")

    def test_resolved_can_reopen_if_patch_failed(self):
        self.inc.reopen_investigation({"root_cause": "Still OOMKilling after patch"})
        assert self.inc.state == "Investigating"
        assert self.inc.patch_applied is None  # Cleared on reopen

    def test_resolved_cannot_close_without_rca(self):
        self.inc.worked = True
        with pytest.raises(RCAValidationError):
            self.inc.close("Short")  # Too short

    def test_resolved_cannot_close_without_worked_true(self):
        self.inc.worked = False
        with pytest.raises(RCAValidationError):
            self.inc.close("Detailed RCA: The memory limit of 32Mi was too low for the payment-gateway service.")

    def test_resolved_can_close_with_full_valid_rca(self):
        self.inc.worked = True
        self.inc.close("Memory limit of 32Mi was insufficient for the Python process. Raised to 256Mi. Verified running for 15 minutes.")
        assert self.inc.state == "Closed"
        assert self.inc.mttr_seconds is not None

    def test_resolved_cannot_resolve_again(self):
        with pytest.raises(InvalidTransitionError):
            self.inc.mark_resolved({"patch": "another patch"}, "someone@company.com")


# ── Closed State (Terminal) ────────────────────────────────────────────────────

class TestClosedState:
    def setup_method(self):
        self.inc = make_incident()
        self.inc.start_investigation({"root_cause": "OOMKilled"})
        self.inc.mark_resolved({"patch": "increase memory"}, "rahul@company.com")
        self.inc.worked = True
        self.inc.close("Memory limit was 32Mi, raised to 256Mi. Pod stable for 15 minutes post-fix.")

    def test_closed_is_terminal_cannot_investigate(self):
        with pytest.raises(InvalidTransitionError):
            self.inc.start_investigation({"root_cause": "New issue"})

    def test_closed_is_terminal_cannot_resolve(self):
        with pytest.raises(InvalidTransitionError):
            self.inc.mark_resolved({"patch": "new"}, "someone")

    def test_closed_is_terminal_cannot_close_again(self):
        with pytest.raises(InvalidTransitionError):
            self.inc.close("Another RCA text that is definitely long enough here for testing.")


# ── Serialisation ──────────────────────────────────────────────────────────────

class TestSerialization:
    def test_to_dict_and_from_dict_roundtrip(self):
        inc = make_incident()
        inc.start_investigation({"root_cause": "Missing secret"})
        d = inc.to_dict()
        restored = Incident.from_dict(d)
        assert restored.incident_id == inc.incident_id
        assert restored.state == "Investigating"
        assert restored.target_deployment == "auth-service"

    def test_mttr_calculated_on_close(self):
        inc = make_incident()
        inc.start_investigation({"root_cause": "OOM"})
        inc.mark_resolved({"patch": "data"}, "admin")
        inc.worked = True
        inc.close("Detailed root cause: Memory limit was too low. Raised to 256Mi. Validated for 15 minutes.")
        assert isinstance(inc.mttr_seconds, int)
        assert inc.mttr_seconds >= 0
