"""
conftest.py — Shared fixtures for the SRE controller test suite.
No K8s cluster or LLM required; all external calls are mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def base_incident_kwargs():
    return {
        "incident_id": "INC-2026-0101-TEST",
        "error_state": "CrashLoopBackOff",
        "error_fingerprint": "abc123def456",
        "target_deployment": "auth-service",
        "target_namespace": "production",
    }


@pytest.fixture
def sample_pod_context():
    """A standard pod context dict representing a failing pod."""
    return {
        "deployment": "shipping-service",
        "namespace": "production",
        "pod": "shipping-service-594b79b5f8-gq6vp",
        "error_state": "CrashLoopBackOff",
        "uid": "4fb99fff-2e76-4573-a8c8-d96f0b33d9fa",
    }


@pytest.fixture
def sample_deployment_spec():
    """A minimal Deployment spec as returned by the K8s API."""
    return {
        "metadata": {"name": "shipping-service", "namespace": "production"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "shipping-app",
                            "image": "python:3.9-slim",
                            "command": ["python", "-c", "import sys; sys.exit(1)"],
                        }
                    ]
                }
            },
        },
    }


@pytest.fixture
def sample_rca():
    """A valid RCA dict as returned by the AnalystAgent."""
    return {
        "root_cause": "Invalid entrypoint command causes immediate container exit.",
        "severity": "high",
        "likely_recurring": True,
        "estimated_impact": "The shipping-service is completely unavailable.",
    }


@pytest.fixture
def sample_patch():
    """A valid strategic merge patch as returned by the FixerAgent."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "shipping-app",
                            "command": None,
                            "args": ["app.py"],
                        }
                    ]
                }
            }
        }
    }


@pytest.fixture
def mock_vertex_response():
    """
    Factory fixture: returns a mock for _generate_content that returns
    a configurable JSON string.
    Use with monkeypatch or patch() in individual tests.
    """
    return MagicMock(return_value='{"root_cause": "test", "severity": "high", "likely_recurring": false, "estimated_impact": "service down"}')


@pytest.fixture
def mock_custom_api():
    """A fully mocked kubernetes_asyncio CustomObjectsApi."""
    api = AsyncMock()
    api.list_namespaced_custom_object = AsyncMock(return_value={"items": []})
    api.get_namespaced_custom_object = AsyncMock(
        return_value={"spec": {"seenCount": 1}}
    )
    api.patch_namespaced_custom_object = AsyncMock(return_value={})
    return api


@pytest.fixture
def mock_apps_api():
    """A fully mocked kubernetes_asyncio AppsV1Api for dry-run validation."""
    api = AsyncMock()
    api.patch_namespaced_deployment = AsyncMock(return_value={})
    api.api_client = AsyncMock()
    api.api_client.close = AsyncMock()
    return api
