"""
llm_client.py — Multi-Agent Remediation Pipeline.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
import httpx
from kubernetes_asyncio import client as k8s_client

try:
    from google import genai
    GENAI_SDK_AVAILABLE = True
except ImportError:
    GENAI_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)
import controller.telemetry as telemetry

# ── Environment Configuration ──────────────────────────────────────────────────
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "project-036ddc82-f451-4fae-9e3")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")
_vertex_client: genai.Client | None = None

def _get_vertex_client() -> genai.Client | None:
    global _vertex_client
    if _vertex_client is not None:
        return _vertex_client
    if not GENAI_SDK_AVAILABLE:
        return None
    try:
        adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/gcp/credentials.json")
        if os.path.exists(adc_path) and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path
        _vertex_client = genai.Client(vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION)
        return _vertex_client
    except Exception as exc:
        logger.warning("Failed to initialize Vertex AI client: %s", exc)
        return None

# ── Core LLM Generation Wrapper ────────────────────────────────────────────────
async def _generate_content(prompt: str, incident_id: str) -> str:
    """Routes the prompt to Vertex AI and returns raw text."""
    tracer = telemetry.get_tracer()
    start = time.time()
    
    with tracer.start_as_current_span(
        "sre.llm.generate",
        attributes={"provider": "vertex", "model": VERTEX_MODEL, "incident.id": incident_id},
    ) as llm_span:
        try:
            client = _get_vertex_client()
            if not client:
                logger.error("[%s] Vertex AI client could not be initialized.", incident_id)
                return ""
                
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: client.models.generate_content(model=VERTEX_MODEL, contents=prompt)
            )
            return response.text or ""

        except Exception as exc:
            llm_span.record_exception(exc)
            logger.error("[%s] Vertex AI generation failed: %s", incident_id, exc)
            return ""

def _parse_json(raw_text: str) -> dict:
    if not raw_text:
        return {}
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    try: return json.loads(cleaned)
    except: pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try: return json.loads(match.group())
        except: pass
    return {}

# ── Multi-Agent Classes ────────────────────────────────────────────────────────

class AnalystAgent:
    @staticmethod
    async def analyze(pod_context: dict, cleaned_logs: str, events: str, past_incidents: list[dict], incident_id: str) -> dict:
        history_block = ""
        if past_incidents:
            lines = ["=== HISTORICAL CONTEXT ==="]
            for i, inc in enumerate(past_incidents, 1):
                res = inc.get("resolution", {})
                lines.append(f"Incident #{i}: {inc.get('errorState')} -> {res.get('resolutionNotes')}")
            history_block = "\n".join(lines)

        prompt = f"""You are an expert Kubernetes SRE Analyst. Diagnose the pod failure.
Respond in valid JSON format: {{"root_cause": "exact reason", "severity": "low|medium|high|critical", "likely_recurring": true, "estimated_impact": "impact"}}

=== METADATA ===
Deployment: {pod_context.get('deployment')} (Namespace: {pod_context.get('namespace')})
Pod: {pod_context.get('pod')}
Error State: {pod_context.get('error_state')}

=== PREPROCESSED LOGS ===
{cleaned_logs}

=== KUBERNETES EVENTS ===
{events}

{history_block}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)

class FixerAgent:
    @staticmethod
    async def propose_fix(rca: dict, deployment_spec: dict, validation_error: str | None, incident_id: str) -> dict:
        error_context = f"\n=== VALIDATION ERROR (Previous attempt failed) ===\n{validation_error}\nFix the patch to avoid this error." if validation_error else ""
        
        prompt = f"""You are an expert Kubernetes Infrastructure Engineer. 
Based on the Root Cause Analysis, propose a valid Kubernetes JSON Merge Patch to fix the deployment.
Respond in valid JSON format: {{"suggested_fix_description": "step-by-step human description", "auto_restart_safe": true, "patch": {{"spec": {{"template": ...}}}}}}
If no automated patch can be applied, set "patch" to {{}}. Note: Use strategic merge patch format for Deployments.
CRITICAL RULES FOR JSON PATCHES:
If modifying a container in a Deployment, you MUST include the container 'name' field so Kubernetes knows which container to patch. For example:
{{"spec": {{"template": {{"spec": {{"containers": [{{"name": "YOUR_CONTAINER_NAME", "env": [...]}}]}}}}}}}}

=== ROOT CAUSE ANALYSIS ===
{json.dumps(rca, indent=2)}

=== DEPLOYMENT SPEC (Current) ===
{json.dumps(deployment_spec, indent=2)}
{error_context}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)

class ValidatorAgent:
    @staticmethod
    async def validate(namespace: str, deployment_name: str, patch: dict) -> tuple[bool, str]:
        if not patch:
            return True, ""
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=patch,
                dry_run="All",
                _content_type="application/strategic-merge-patch+json"
            )
            return True, ""
        except Exception as e:
            return False, str(e)

# ── Orchestrator ───────────────────────────────────────────────────────────────

async def diagnose_incident(
    pod_context: dict,
    deployment_spec: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict:
    logger.info("[%s] Starting Multi-Agent Remediation Pipeline", incident_id)
    
    # 1. Analyst Agent
    try:
        rca = await AnalystAgent.analyze(pod_context, cleaned_logs, events, past_incidents or [], incident_id)
        logger.info("[%s] Analyst RCA: %s", incident_id, rca.get("root_cause", "N/A"))
    except Exception as exc:
        logger.error("[%s] Analyst Agent failed with exception: %s", incident_id, exc)
        rca = {}

    # 2. Fixer & Validator Loop
    patch_result = {}
    validation_error = None
    max_retries = 3
    
    for attempt in range(max_retries):
        logger.info("[%s] Fixer Agent attempt %d/%d", incident_id, attempt + 1, max_retries)
        try:
            patch_result = await FixerAgent.propose_fix(rca, deployment_spec, validation_error, incident_id)
        except Exception as exc:
            logger.error("[%s] Fixer Agent failed with exception: %s", incident_id, exc)
            break
        
        patch = patch_result.get("patch", {})
        if not patch:
            logger.info("[%s] Fixer Agent determined no patch applies", incident_id)
            break
            
        logger.info("[%s] Validator Agent dry-running patch", incident_id)
        is_valid, error_msg = await ValidatorAgent.validate(pod_context.get("namespace"), pod_context.get("deployment"), patch)
        
        if is_valid:
            logger.info("[%s] ✅ Validator Agent approved patch", incident_id)
            patch_result["validation_passed"] = True
            break
        else:
            logger.warning("[%s] ❌ Validator Agent rejected patch: %s", incident_id, error_msg)
            validation_error = error_msg

    # Format output for PR creation
    return {
        "root_cause": rca.get("root_cause", "Analysis failed"),
        "severity": rca.get("severity", "high"),
        "suggested_fix": patch_result.get("suggested_fix_description", "Manual investigation required."),
        "auto_restart_safe": patch_result.get("auto_restart_safe", False),
        "config_suggestions": [],
        "likely_recurring": rca.get("likely_recurring", False),
        "estimated_impact": rca.get("estimated_impact", "Unknown"),
        "matches_past_incident": None,
        "confidence_boost": "high" if patch_result.get("validation_passed") else "none",
        "proposed_patch": patch_result.get("patch", {}) if patch_result.get("validation_passed") else {}
    }
