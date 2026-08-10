"""
test_llm_client.py — Unit tests for the Multi-Agent Remediation Pipeline.

All Vertex AI and K8s API calls are mocked — no network, no cost, runs in CI.
Uses pytest-asyncio for async test support.
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from controller.llm_client import (
    AnalystAgent,
    FixerAgent,
    ValidatorAgent,
    _parse_json,
    diagnose_incident,
)


# ── _parse_json ────────────────────────────────────────────────────────────────

class TestParseJson:
    def test_parses_clean_json(self):
        raw = '{"root_cause": "OOM", "severity": "high"}'
        result = _parse_json(raw)
        assert result["root_cause"] == "OOM"
        assert result["severity"] == "high"

    def test_parses_json_wrapped_in_markdown_fence(self):
        raw = '```json\n{"root_cause": "OOM", "severity": "high"}\n```'
        result = _parse_json(raw)
        assert result["severity"] == "high"

    def test_parses_json_embedded_in_text(self):
        raw = 'Here is the diagnosis:\n{"root_cause": "bad image", "severity": "low"}\nEnd.'
        result = _parse_json(raw)
        assert result["severity"] == "low"

    def test_returns_empty_dict_on_invalid_json(self):
        result = _parse_json("This is not JSON at all.")
        assert result == {}

    def test_returns_empty_dict_on_empty_string(self):
        result = _parse_json("")
        assert result == {}

    def test_returns_empty_dict_on_none(self):
        result = _parse_json(None)
        assert result == {}


# ── AnalystAgent ───────────────────────────────────────────────────────────────

class TestAnalystAgent:
    @pytest.mark.asyncio
    async def test_returns_structured_rca(self, sample_pod_context):
        rca_json = json.dumps({
            "root_cause": "Invalid entrypoint exits immediately.",
            "severity": "high",
            "likely_recurring": True,
            "estimated_impact": "Service completely down.",
        })
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value=rca_json)):
            result = await AnalystAgent.analyze(
                sample_pod_context, "FATAL: exec /bin/sh: exit 1", "", [], "INC-TEST-001"
            )
        assert result["severity"] == "high"
        assert result["likely_recurring"] is True
        assert "root_cause" in result

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_llm_returns_invalid_json(self, sample_pod_context):
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value="not json")):
            result = await AnalystAgent.analyze(
                sample_pod_context, "some logs", "", [], "INC-TEST-002"
            )
        assert result == {}

    @pytest.mark.asyncio
    async def test_includes_historical_context_in_prompt(self, sample_pod_context):
        """Verify that past incidents are included in the prompt sent to the LLM."""
        past_incidents = [
            {"errorState": "CrashLoopBackOff", "resolution": {"resolutionNotes": "Increased memory."}}
        ]
        captured_prompts = []

        async def capture_prompt(prompt, incident_id):
            captured_prompts.append(prompt)
            return '{"root_cause": "x", "severity": "low", "likely_recurring": false, "estimated_impact": "x"}'

        with patch("controller.llm_client._generate_content", new=capture_prompt):
            await AnalystAgent.analyze(
                sample_pod_context, "logs", "", past_incidents, "INC-TEST-003"
            )
        assert "HISTORICAL CONTEXT" in captured_prompts[0]
        assert "Increased memory." in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_llm_fails(self, sample_pod_context):
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value="")):
            result = await AnalystAgent.analyze(
                sample_pod_context, "logs", "", [], "INC-TEST-004"
            )
        assert result == {}


# ── FixerAgent ────────────────────────────────────────────────────────────────

class TestFixerAgent:
    @pytest.mark.asyncio
    async def test_returns_patch_with_container_name(self, sample_rca, sample_deployment_spec):
        """Critical: every container in a patch MUST include the 'name' field."""
        patch_json = json.dumps({
            "suggested_fix_description": "Remove the bad command.",
            "auto_restart_safe": True,
            "patch": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "shipping-app", "command": None}]
                        }
                    }
                }
            },
        })
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value=patch_json)):
            result = await FixerAgent.propose_fix(sample_rca, sample_deployment_spec, None, "INC-TEST-010")

        containers = result.get("patch", {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        assert len(containers) > 0
        assert all("name" in c for c in containers), "All containers in patch MUST have a 'name' field"

    @pytest.mark.asyncio
    async def test_includes_validation_error_in_retry_prompt(self, sample_rca, sample_deployment_spec):
        """On retry, the validation error must appear in the prompt."""
        captured_prompts = []

        async def capture(prompt, incident_id):
            captured_prompts.append(prompt)
            return '{"patch": {}, "suggested_fix_description": "x", "auto_restart_safe": false}'

        with patch("controller.llm_client._generate_content", new=capture):
            await FixerAgent.propose_fix(
                sample_rca, sample_deployment_spec, "422 Unprocessable Entity", "INC-TEST-011"
            )
        assert "VALIDATION ERROR" in captured_prompts[0]
        assert "422 Unprocessable Entity" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_returns_empty_patch_when_llm_says_no_patch(self, sample_rca, sample_deployment_spec):
        """When the LLM cannot propose a fix, patch should be {}."""
        fixer_json = json.dumps({
            "suggested_fix_description": "Manual investigation required.",
            "auto_restart_safe": False,
            "patch": {},
        })
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value=fixer_json)):
            result = await FixerAgent.propose_fix(sample_rca, sample_deployment_spec, None, "INC-TEST-012")
        assert result.get("patch") == {}


# ── ValidatorAgent ────────────────────────────────────────────────────────────

class TestValidatorAgent:
    @pytest.mark.asyncio
    async def test_approves_valid_patch(self, sample_patch, mock_apps_api):
        with patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            is_valid, err = await ValidatorAgent.validate("production", "shipping-service", sample_patch)
        assert is_valid is True
        assert err == ""

    @pytest.mark.asyncio
    async def test_rejects_invalid_patch(self, sample_patch, mock_apps_api):
        mock_apps_api.patch_namespaced_deployment = AsyncMock(
            side_effect=Exception("422 Unprocessable Entity: required field missing")
        )
        with patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            is_valid, err = await ValidatorAgent.validate("production", "shipping-service", sample_patch)
        assert is_valid is False
        assert "422" in err

    @pytest.mark.asyncio
    async def test_approves_empty_patch_without_calling_api(self, mock_apps_api):
        """An empty patch is trivially valid — no API call needed."""
        with patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            is_valid, _ = await ValidatorAgent.validate("production", "shipping-service", {})
        assert is_valid is True
        mock_apps_api.patch_namespaced_deployment.assert_not_awaited()


# ── diagnose_incident (Orchestrator) ─────────────────────────────────────────

class TestDiagnoseIncident:
    @pytest.mark.asyncio
    async def test_full_pipeline_happy_path(self, sample_pod_context, sample_deployment_spec, mock_apps_api):
        """Full pipeline: Analyst → Fixer → Validator succeeds on first try."""
        analyst_response = json.dumps({
            "root_cause": "Bad entrypoint.",
            "severity": "high",
            "likely_recurring": True,
            "estimated_impact": "Service down.",
        })
        fixer_response = json.dumps({
            "suggested_fix_description": "Remove the bad command.",
            "auto_restart_safe": True,
            "patch": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "shipping-app", "command": None}]
                        }
                    }
                }
            },
        })
        # LLM returns analyst response first, then fixer response
        call_count = [0]
        async def mock_generate(prompt, incident_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return analyst_response
            return fixer_response

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "FATAL: exit 1", "", "INC-HAPPY-001"
            )

        assert result["severity"] == "high"
        assert result["root_cause"] == "Bad entrypoint."
        assert result["confidence_boost"] == "high"  # Validator approved
        assert result["proposed_patch"] != {}
        assert call_count[0] == 2  # Analyst + Fixer called once each

    @pytest.mark.asyncio
    async def test_fixer_retries_on_validation_failure(self, sample_pod_context, sample_deployment_spec, mock_apps_api):
        """If Validator rejects, Fixer is retried with the error feedback."""
        analyst_response = json.dumps({
            "root_cause": "Bad image.", "severity": "medium",
            "likely_recurring": False, "estimated_impact": "degraded",
        })
        fixer_response = json.dumps({
            "suggested_fix_description": "Fix the image tag.",
            "auto_restart_safe": True,
            "patch": {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "nginx:latest"}]}}}},
        })
        # Validator fails first, succeeds second
        validate_call_count = [0]
        async def mock_validate(*args, **kwargs):
            validate_call_count[0] += 1
            if validate_call_count[0] == 1:
                raise Exception("422 invalid patch")
            return MagicMock()

        mock_apps_api.patch_namespaced_deployment = mock_validate
        llm_call_count = [0]

        async def mock_generate(prompt, incident_id):
            llm_call_count[0] += 1
            if llm_call_count[0] == 1:
                return analyst_response
            return fixer_response

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-RETRY-001"
            )

        # Analyst called once + Fixer called at least twice (1 fail + 1 success)
        assert llm_call_count[0] >= 3

    @pytest.mark.asyncio
    async def test_pipeline_handles_analyst_failure_gracefully(self, sample_pod_context, sample_deployment_spec):
        """If the Analyst returns nothing, the pipeline should return safe defaults."""
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value="")):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-FAIL-001"
            )
        assert result["root_cause"] == "Analysis failed"
        assert result["severity"] == "high"  # Defaults to high for safety
        assert result["proposed_patch"] == {}
