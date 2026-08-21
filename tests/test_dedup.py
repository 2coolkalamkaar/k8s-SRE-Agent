"""
test_dedup.py — Unit tests for the 3-layer deduplication engine.

No K8s cluster required. Layer 1 & 2 are pure in-memory.
Layer 3 tests use a mocked CustomObjectsApi.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import controller.dedup as dedup_module
from controller.dedup import (
    DAMPEN_COUNT,
    check_fingerprint_cache,
    clear_dampening,
    clear_fingerprint,
    has_open_patchrequest,
    increment_seen_count,
    register_fingerprint,
    should_trigger,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reset_dedup_state():
    """Reset in-memory dedup state between tests to ensure isolation."""
    dedup_module._event_window.clear()
    dedup_module._fingerprint_cache.clear()


@pytest.fixture(autouse=True)
def reset_state():
    """Auto-reset dedup state before every test."""
    _reset_dedup_state()
    yield
    _reset_dedup_state()


# ── Layer 1: Event Dampening ───────────────────────────────────────────────────

class TestLayer1Dampening:
    def test_does_not_trigger_below_threshold(self):
        pod_uid = "uid-test-001"
        for _ in range(DAMPEN_COUNT - 1):
            result = asyncio.get_event_loop().run_until_complete(
                should_trigger(pod_uid, "CrashLoopBackOff")
            )
        assert result is False

    def test_triggers_at_threshold(self):
        pod_uid = "uid-test-002"
        result = False
        for _ in range(DAMPEN_COUNT):
            result = asyncio.get_event_loop().run_until_complete(
                should_trigger(pod_uid, "CrashLoopBackOff")
            )
        assert result is True

    def test_triggers_above_threshold(self):
        pod_uid = "uid-test-003"
        result = False
        for _ in range(DAMPEN_COUNT + 5):
            result = asyncio.get_event_loop().run_until_complete(
                should_trigger(pod_uid, "CrashLoopBackOff")
            )
        assert result is True

    def test_oomkilled_bypasses_dampening_immediately(self):
        """OOMKilled should trigger on the very first event — no dampening."""
        result = asyncio.get_event_loop().run_until_complete(
            should_trigger("uid-oom-001", "OOMKilled")
        )
        assert result is True

    def test_imagepullbackoff_bypasses_dampening_immediately(self):
        """ImagePullBackOff IS in IMMEDIATE_TRIGGER_STATES — pod is stuck and will
        never generate a 2nd event, so dampening would permanently suppress it.
        Bug fixed in feat/rag: moved ImagePullBackOff into IMMEDIATE_TRIGGER_STATES.
        """
        result = asyncio.get_event_loop().run_until_complete(
            should_trigger("uid-img-001", "ImagePullBackOff")
        )
        assert result is True

    def test_different_error_states_dont_share_count(self):
        """Switching error state on the same pod UID resets the effective count
        for dampened states. Uses ContainerCrashed (a dampened state) as the
        second state since ImagePullBackOff is now in IMMEDIATE_TRIGGER_STATES.
        """
        pod_uid = "uid-mixed-002"
        # Two CrashLoopBackOff events — but not yet at threshold (need 3)
        for _ in range(2):
            asyncio.get_event_loop().run_until_complete(
                should_trigger(pod_uid, "CrashLoopBackOff")
            )
        # Now fire a different dampened state — its own window starts at 1,
        # so it should NOT cross the threshold of 3 yet.
        result = asyncio.get_event_loop().run_until_complete(
            should_trigger(pod_uid, "ContainerCrashed")
        )
        assert result is False

    def test_clear_dampening_resets_counter(self):
        """After clear_dampening, the pod must re-accumulate events."""
        pod_uid = "uid-clear-001"
        for _ in range(DAMPEN_COUNT):
            asyncio.get_event_loop().run_until_complete(
                should_trigger(pod_uid, "CrashLoopBackOff")
            )
        clear_dampening(pod_uid)
        # After clear, 1 event should not trigger
        result = asyncio.get_event_loop().run_until_complete(
            should_trigger(pod_uid, "CrashLoopBackOff")
        )
        assert result is False

    def test_clear_dampening_unknown_uid_is_noop(self):
        """clear_dampening on an unknown UID should not raise."""
        clear_dampening("nonexistent-uid")  # Should not raise


# ── Layer 2: Log Fingerprint Cache ────────────────────────────────────────────

class TestLayer2FingerprintCache:
    def test_miss_on_empty_cache(self):
        is_dup, pr_name = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache("fp-unknown")
        )
        assert is_dup is False
        assert pr_name is None

    def test_hit_after_register(self):
        asyncio.get_event_loop().run_until_complete(
            register_fingerprint("fp-test-001", "my-service-pr-001")
        )
        is_dup, pr_name = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache("fp-test-001")
        )
        assert is_dup is True
        assert pr_name == "my-service-pr-001"

    def test_miss_after_clear(self):
        asyncio.get_event_loop().run_until_complete(
            register_fingerprint("fp-test-002", "my-service-pr-002")
        )
        asyncio.get_event_loop().run_until_complete(clear_fingerprint("fp-test-002"))
        is_dup, _ = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache("fp-test-002")
        )
        assert is_dup is False

    def test_miss_after_ttl_expires(self):
        """Simulate TTL expiry by backdating the cache entry timestamp."""
        fp = "fp-expired-001"
        asyncio.get_event_loop().run_until_complete(
            register_fingerprint(fp, "old-pr")
        )
        # Backdate the cache entry to 2 hours ago (past the 1h TTL)
        expired_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        dedup_module._fingerprint_cache[fp] = (expired_time, "old-pr")

        is_dup, _ = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache(fp)
        )
        assert is_dup is False

    def test_clear_unknown_fingerprint_is_noop(self):
        """clear_fingerprint on a non-existent key should not raise."""
        asyncio.get_event_loop().run_until_complete(clear_fingerprint("nonexistent-fp"))

    def test_different_fingerprints_are_independent(self):
        asyncio.get_event_loop().run_until_complete(
            register_fingerprint("fp-a", "pr-a")
        )
        is_dup_a, _ = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache("fp-a")
        )
        is_dup_b, _ = asyncio.get_event_loop().run_until_complete(
            check_fingerprint_cache("fp-b")
        )
        assert is_dup_a is True
        assert is_dup_b is False


# ── Layer 3: Active PatchRequest Check ────────────────────────────────────────

class TestLayer3ActivePRCheck:
    def test_returns_false_when_no_prs_exist(self, mock_custom_api):
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={"items": []}
        )
        exists, name = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "CrashLoopBackOff", mock_custom_api)
        )
        assert exists is False
        assert name is None

    def test_returns_true_when_pending_pr_exists(self, mock_custom_api):
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [
                    {
                        "metadata": {"name": "my-svc-pr-2026-001"},
                        "spec": {"errorState": "CrashLoopBackOff"},
                        "status": {"approvalState": "Pending"},
                    }
                ]
            }
        )
        exists, pr_name = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "CrashLoopBackOff", mock_custom_api)
        )
        assert exists is True
        assert pr_name == "my-svc-pr-2026-001"

    def test_returns_true_when_approved_pr_exists(self, mock_custom_api):
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [
                    {
                        "metadata": {"name": "my-svc-pr-approved"},
                        "spec": {"errorState": "OOMKilled"},
                        "status": {"approvalState": "Approved"},
                    }
                ]
            }
        )
        exists, _ = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "OOMKilled", mock_custom_api)
        )
        assert exists is True

    def test_ignores_pr_for_different_error_state(self, mock_custom_api):
        """A PR for CrashLoopBackOff should NOT block a new OOMKilled incident."""
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [
                    {
                        "metadata": {"name": "my-svc-pr-clbo"},
                        "spec": {"errorState": "CrashLoopBackOff"},
                        "status": {"approvalState": "Pending"},
                    }
                ]
            }
        )
        exists, _ = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "OOMKilled", mock_custom_api)
        )
        assert exists is False

    def test_ignores_closed_pr(self, mock_custom_api):
        """A Closed PR should not prevent a new incident from being created."""
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            return_value={
                "items": [
                    {
                        "metadata": {"name": "my-svc-pr-closed"},
                        "spec": {"errorState": "CrashLoopBackOff"},
                        "status": {"approvalState": "Closed"},
                    }
                ]
            }
        )
        exists, _ = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "CrashLoopBackOff", mock_custom_api)
        )
        assert exists is False

    def test_fails_open_on_api_error(self, mock_custom_api):
        """If the K8s API fails, Layer 3 must fail-open (allow new PR creation)."""
        mock_custom_api.list_namespaced_custom_object = AsyncMock(
            side_effect=Exception("K8s API unavailable")
        )
        exists, _ = asyncio.get_event_loop().run_until_complete(
            has_open_patchrequest("production", "my-svc", "CrashLoopBackOff", mock_custom_api)
        )
        assert exists is False


# ── increment_seen_count ───────────────────────────────────────────────────────

class TestIncrementSeenCount:
    def test_increments_seen_count(self, mock_custom_api):
        mock_custom_api.get_namespaced_custom_object = AsyncMock(
            return_value={"spec": {"seenCount": 4}}
        )
        asyncio.get_event_loop().run_until_complete(
            increment_seen_count("my-svc-pr-001", "production", mock_custom_api)
        )
        mock_custom_api.patch_namespaced_custom_object.assert_awaited_once()

    def test_does_not_raise_on_api_error(self, mock_custom_api):
        """increment_seen_count failure must be silent — it's a best-effort operation."""
        mock_custom_api.get_namespaced_custom_object = AsyncMock(
            side_effect=Exception("API error")
        )
        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            increment_seen_count("bad-pr", "production", mock_custom_api)
        )
