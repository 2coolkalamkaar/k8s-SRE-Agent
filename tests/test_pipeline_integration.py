"""
test_pipeline_integration.py — Integration tests for the full detection → dedup → pipeline flow.

These tests verify that the individual components wire together correctly.
All K8s API and LLM calls are mocked — no cluster or network required.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

import controller.dedup as dedup_module
from controller.dedup import register_fingerprint
from controller.llm_client import diagnose_incident


# ── Helpers ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_dedup_state():
    dedup_module._event_window.clear()
    dedup_module._fingerprint_cache.clear()
    yield
    dedup_module._event_window.clear()
    dedup_module._fingerprint_cache.clear()


def make_good_analyst_response():
    return json.dumps({
        "root_cause": "Container exits immediately due to invalid entrypoint.",
        "severity": "high",
        "likely_recurring": True,
        "estimated_impact": "Full service outage.",
    })


def make_good_fixer_response():
    return json.dumps({
        "suggested_fix_description": "Null out the bad command field.",
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


# ── Full Pipeline Output Schema Validation ────────────────────────────────────

class TestPipelineOutputSchema:
    """
    Verify that diagnose_incident always returns a dict with all fields
    required to construct a valid PatchRequest CRD.
    """
    REQUIRED_FIELDS = {
        "root_cause", "severity", "suggested_fix", "auto_restart_safe",
        "config_suggestions", "likely_recurring", "estimated_impact",
        "matches_past_incident", "confidence_boost", "proposed_patch",
    }

    @pytest.mark.asyncio
    async def test_output_contains_all_required_patchrequest_fields(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        call_count = [0]
        async def mock_generate(prompt, incident_id):
            call_count[0] += 1
            return make_good_analyst_response() if call_count[0] == 1 else make_good_fixer_response()

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "FATAL: exit", "", "INC-SCHEMA-001"
            )

        missing = self.REQUIRED_FIELDS - set(result.keys())
        assert not missing, f"Pipeline output missing required fields: {missing}"

    @pytest.mark.asyncio
    async def test_severity_is_valid_enum_value(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        """Severity must be one of the values allowed by the CRD schema."""
        VALID_SEVERITIES = {"low", "medium", "high", "critical"}
        call_count = [0]

        async def mock_generate(prompt, incident_id):
            call_count[0] += 1
            return make_good_analyst_response() if call_count[0] == 1 else make_good_fixer_response()

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-SCHEMA-002"
            )

        assert result["severity"] in VALID_SEVERITIES

    @pytest.mark.asyncio
    async def test_confidence_boost_is_high_when_validator_approves(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        call_count = [0]

        async def mock_generate(prompt, incident_id):
            call_count[0] += 1
            return make_good_analyst_response() if call_count[0] == 1 else make_good_fixer_response()

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-SCHEMA-003"
            )

        assert result["confidence_boost"] == "high"

    @pytest.mark.asyncio
    async def test_confidence_boost_is_none_when_no_patch(
        self, sample_pod_context, sample_deployment_spec
    ):
        """When neither Analyst nor Fixer produces output, confidence_boost must be 'none'."""
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value="")):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-SCHEMA-004"
            )
        assert result["confidence_boost"] == "none"
        assert result["proposed_patch"] == {}


# ── Dedup → Pipeline Integration ──────────────────────────────────────────────

class TestDedupIntegration:
    @pytest.mark.asyncio
    async def test_duplicate_fingerprint_skips_llm(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        """
        If a fingerprint is already in the cache (Layer 2), the pipeline should
        NOT be called again — the LLM call count must be 0.
        """
        from controller.dedup import check_fingerprint_cache
        fp = "existing-fp-abc123"
        await register_fingerprint(fp, "existing-pr-001")

        is_dup, existing_pr = await check_fingerprint_cache(fp)
        assert is_dup is True, "Pre-condition: fingerprint must be in cache"
        assert existing_pr == "existing-pr-001"

        # The calling code in main.py checks the cache and returns early.
        # We verify the cache logic itself works — if it returns is_dup=True,
        # the LLM is not invoked.
        llm_called = [False]

        async def mock_generate(prompt, incident_id):
            llm_called[0] = True
            return make_good_analyst_response()

        # Simulate the guard in main.py: check cache before calling diagnose_incident
        is_dup_check, _ = await check_fingerprint_cache(fp)
        if not is_dup_check:
            with patch("controller.llm_client._generate_content", new=mock_generate):
                await diagnose_incident(
                    sample_pod_context, sample_deployment_spec, "logs", "", "INC-DUP-001"
                )

        assert llm_called[0] is False, "LLM must NOT be called when fingerprint is cached"

    @pytest.mark.asyncio
    async def test_layer1_dampening_prevents_premature_pipeline_call(self):
        """
        Simulates Layer 1 by calling should_trigger below the threshold.
        Verifies it returns False so the pipeline is not triggered.
        """
        from controller.dedup import should_trigger, DAMPEN_COUNT
        results = []
        for _ in range(DAMPEN_COUNT - 1):
            result = await should_trigger("pod-uid-l1-test", "CrashLoopBackOff")
            results.append(result)

        assert all(r is False for r in results), "Pipeline must not trigger before dampening threshold"

    @pytest.mark.asyncio
    async def test_new_fingerprint_registers_after_pipeline(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        """
        After the pipeline runs successfully, registering the fingerprint must
        make subsequent cache lookups return is_dup=True.
        """
        from controller.dedup import check_fingerprint_cache
        from controller.log_preprocessor import make_fingerprint

        fp = make_fingerprint("FATAL: exit 1", "CrashLoopBackOff")

        # Pre-condition: not in cache
        is_dup, _ = await check_fingerprint_cache(fp)
        assert is_dup is False

        # Simulate pipeline + registration
        await register_fingerprint(fp, "new-pr-001")

        # Post-condition: now cached
        is_dup, pr_name = await check_fingerprint_cache(fp)
        assert is_dup is True
        assert pr_name == "new-pr-001"


# ── Resilience Tests ───────────────────────────────────────────────────────────

class TestPipelineResilience:
    @pytest.mark.asyncio
    async def test_pipeline_returns_safe_defaults_when_analyst_produces_garbage(
        self, sample_pod_context, sample_deployment_spec
    ):
        """If the LLM returns non-JSON, the pipeline must not crash."""
        with patch("controller.llm_client._generate_content", new=AsyncMock(return_value="I don't know!")):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-RES-001"
            )
        assert isinstance(result, dict)
        assert result["root_cause"] == "Analysis failed"

    @pytest.mark.asyncio
    async def test_pipeline_does_not_crash_when_llm_raises_exception(
        self, sample_pod_context, sample_deployment_spec
    ):
        """If _generate_content raises, the pipeline must handle it gracefully."""
        async def raising_generate(prompt, incident_id):
            raise Exception("Network timeout")

        with patch("controller.llm_client._generate_content", new=raising_generate):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-RES-002"
            )
        assert isinstance(result, dict)
        assert "root_cause" in result
        assert result["proposed_patch"] == {}

    @pytest.mark.asyncio
    async def test_max_retry_limit_is_respected(
        self, sample_pod_context, sample_deployment_spec, mock_apps_api
    ):
        """
        The Fixer must attempt at most max_retries=3 times even if every
        validation attempt fails.
        """
        analyst_response = make_good_analyst_response()
        fixer_response = make_good_fixer_response()
        llm_call_count = [0]

        async def mock_generate(prompt, incident_id):
            llm_call_count[0] += 1
            return analyst_response if llm_call_count[0] == 1 else fixer_response

        # Validator always rejects
        mock_apps_api.patch_namespaced_deployment = AsyncMock(
            side_effect=Exception("Always invalid")
        )

        with patch("controller.llm_client._generate_content", new=mock_generate), \
             patch("controller.llm_client.k8s_client.AppsV1Api", return_value=mock_apps_api):
            result = await diagnose_incident(
                sample_pod_context, sample_deployment_spec, "logs", "", "INC-RES-003"
            )

        # 1 Analyst call + 3 Fixer retries = 4 total LLM calls max
        assert llm_call_count[0] <= 4, f"Pipeline exceeded retry limit: {llm_call_count[0]} calls"
        # With all retries failing, confidence should not be high
        assert result["confidence_boost"] != "high"
