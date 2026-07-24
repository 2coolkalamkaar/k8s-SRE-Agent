"""
conftest.py — Shared fixtures for the SRE controller test suite.
No K8s cluster or Ollama required; all external calls are mocked.
"""

import pytest


@pytest.fixture
def base_incident_kwargs():
    return {
        "incident_id": "INC-2026-0101-TEST",
        "error_state": "CrashLoopBackOff",
        "error_fingerprint": "abc123def456",
        "target_deployment": "auth-service",
        "target_namespace": "production",
    }
