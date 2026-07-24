"""
test_preprocessor.py — Unit tests for log_preprocessor module.
"""

import pytest
from controller.log_preprocessor import (
    detect_error_state,
    make_fingerprint,
    preprocess_logs,
)


class TestPreprocessLogs:
    def test_strips_timestamps(self):
        raw = "2026-07-24T10:00:00Z ERROR: secret not found"
        result = preprocess_logs(raw)
        assert "2026-07-24" not in result
        assert "secret not found" in result or "ERROR" in result

    def test_keeps_error_lines(self):
        raw = "\n".join([
            "2026-07-24T10:00:00Z INFO: Server starting",
            "2026-07-24T10:00:01Z ERROR: connection refused to db:5432",
            "2026-07-24T10:00:02Z INFO: Listening on port 8080",
        ])
        result = preprocess_logs(raw)
        assert "connection refused" in result

    def test_deduplicates_consecutive_identical_lines(self):
        raw = "ERROR: same error\nERROR: same error\nERROR: same error"
        result = preprocess_logs(raw)
        assert result.count("same error") == 1

    def test_returns_placeholder_for_empty_logs(self):
        result = preprocess_logs("", "CrashLoopBackOff")
        assert "No logs available" in result

    def test_truncates_to_max_chars(self):
        long_log = "FATAL: something broke\n" * 500
        result = preprocess_logs(long_log)
        assert len(result) <= 4100  # Allow slight buffer for truncation notice


class TestMakeFingerprint:
    def test_same_logs_same_fingerprint(self):
        fp1 = make_fingerprint("error: secret not found", "CrashLoopBackOff")
        fp2 = make_fingerprint("error: secret not found", "CrashLoopBackOff")
        assert fp1 == fp2

    def test_different_logs_different_fingerprint(self):
        fp1 = make_fingerprint("error: secret not found", "CrashLoopBackOff")
        fp2 = make_fingerprint("error: OOMKilled", "OOMKilled")
        assert fp1 != fp2

    def test_fingerprint_is_16_chars(self):
        fp = make_fingerprint("some log content", "CrashLoopBackOff")
        assert len(fp) == 16


class TestDetectErrorState:
    def test_detects_crashloopbackoff(self):
        container_statuses = [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {}}]
        assert detect_error_state(container_statuses) == "CrashLoopBackOff"

    def test_detects_imagepullbackoff(self):
        container_statuses = [{"state": {"waiting": {"reason": "ImagePullBackOff"}}, "lastState": {}}]
        assert detect_error_state(container_statuses) == "ImagePullBackOff"

    def test_detects_oomkilled(self):
        container_statuses = [{
            "state": {"running": {}},
            "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
        }]
        assert detect_error_state(container_statuses) == "OOMKilled"

    def test_detects_create_container_config_error(self):
        container_statuses = [{
            "state": {"waiting": {"reason": "CreateContainerConfigError"}},
            "lastState": {},
        }]
        assert detect_error_state(container_statuses) == "CreateContainerConfigError"

    def test_returns_none_for_healthy_pod(self):
        container_statuses = [{"state": {"running": {}}, "lastState": {}}]
        assert detect_error_state(container_statuses) is None

    def test_returns_none_for_empty_list(self):
        assert detect_error_state([]) is None
