"""
llm_client.py — Multi-Provider SRE LLM Client (GCP Vertex AI + Cloud Gemini API + Local Ollama).

Features:
- GCP Vertex AI Provider: Direct integration with Vertex AI Gemini 2.5 Flash using GCP $300 credits (~3.9s response time).
- Google Gemini API Provider: Direct API Key authentication option.
- Local Ollama Provider: In-cluster fallback for air-gapped resilience.
- Automatic Provider Failover: Tries Vertex AI / Gemini first; seamlessly falls back to Ollama on any issue.
- 5-Layer Fallback Parser: Guarantees unparseable responses never crash the controller.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess
import time

import httpx

try:
    from google import genai
    GENAI_SDK_AVAILABLE = True
except ImportError:
    GENAI_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Environment Configuration ──────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()  # auto | vertex | gemini | ollama

# GCP Vertex AI Config
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "project-036ddc82-f451-4fae-9e3")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")

# Gemini API Config
GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    os.getenv("GEMINI_API", "")
).strip('"\' \t\n\r')

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Ollama Config
OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://ollama-service.ai-infra.svc.cluster.local:11434"
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-coder:6.7b-instruct")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))

# Helper: try loading .env if GEMINI_API_KEY is not in environment
if not GEMINI_API_KEY:
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API=") or line.startswith("GEMINI_API_KEY="):
                        val = line.split("=", 1)[1].strip('"\' \t\n\r')
                        if val:
                            GEMINI_API_KEY = val
                            break
        except Exception:
            pass

_ollama_semaphore = asyncio.Semaphore(3)

# ── Prompt Templates ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a Kubernetes SRE expert. Your only job is to diagnose pod failures. "
    "Always respond with ONLY valid JSON. No markdown. No explanation. No code blocks. "
    "If you are not confident, set severity to 'low' and auto_restart_safe to false."
)

RESPONSE_FORMAT = """{
    "root_cause": "One sentence explaining exactly what went wrong",
    "severity": "low|medium|high|critical",
    "suggested_fix": "Step-by-step fix the operator should apply",
    "auto_restart_safe": true or false,
    "config_suggestions": ["ENV_VAR=value"],
    "likely_recurring": true or false,
    "estimated_impact": "What breaks if this is not fixed",
    "matches_past_incident": "INC-XXXX or null",
    "confidence_boost": "high|none"
}"""


def _build_history_block(past_incidents: list[dict]) -> str:
    if not past_incidents:
        return ""
    lines = ["=== HISTORICAL CONTEXT (past incidents for reference) ==="]
    for i, inc in enumerate(past_incidents, 1):
        res = inc.get("resolution", {})
        lines.append(
            f"\nIncident #{i} [{inc.get('match_type', '').upper()} MATCH — "
            f"{inc.get('confidence', 'low')} confidence]:\n"
            f"  Previous error:   {inc.get('errorState')} on {inc.get('targetDeployment')}\n"
            f"  Root cause found: {inc.get('rootCause')}\n"
            f"  Fix that worked:  {res.get('resolutionNotes', 'N/A')}\n"
            f"  Resolved in:      {res.get('mttr', '?')} seconds\n"
            f"  Fixed by:         {res.get('approvedBy', 'unknown')}"
        )
    lines.append("\nUse the above as context. If this matches a past incident, say so explicitly.")
    return "\n".join(lines)


def build_prompt(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    past_incidents: list[dict] | None = None,
) -> str:
    history_block = _build_history_block(past_incidents or [])
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{history_block}\n\n"
        f"=== CURRENT INCIDENT ===\n"
        f"Pod: {pod_context.get('pod_name')} | Namespace: {pod_context.get('namespace')}\n"
        f"Error: {pod_context.get('error_state')} | Restarts: {pod_context.get('restart_count', 0)}\n"
        f"Limits: CPU={pod_context.get('cpu_limit', 'N/A')}, Memory={pod_context.get('mem_limit', 'N/A')}\n"
        f"Env vars present: {pod_context.get('env_vars', [])}\n\n"
        f"=== CLEANED LOGS ===\n{cleaned_logs}\n\n"
        f"=== K8S EVENTS ===\n{events}\n\n"
        f"=== RESPONSE FORMAT ===\n{RESPONSE_FORMAT}"
    )


# ── 5-Layer JSON Parser ────────────────────────────────────────────────────────

def parse_llm_response(raw_text: str, incident_id: str) -> dict:
    """
    Parse LLM response with 5 fallback strategies.
    NEVER raises — always returns a dict (safe default on total failure).
    """
    if not raw_text or not raw_text.strip():
        logger.warning("[%s] Empty response from LLM", incident_id)
        return _safe_default("Empty response from LLM")

    # Strategy 1: Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()

    # Strategy 2: Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 3: Extract first {...} block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 4: Remove trailing commas
    no_trailing = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(no_trailing)
    except json.JSONDecodeError:
        pass

    # Strategy 5: Remove JS-style comments then retry
    no_comments = re.sub(r"//.*?$|/\*.*?\*/", "", cleaned, flags=re.MULTILINE | re.DOTALL)
    try:
        return json.loads(no_comments.strip())
    except json.JSONDecodeError:
        pass

    # Total failure — return a safe default
    logger.error(
        "[%s] All 5 JSON parse strategies failed.\nRaw (first 500 chars):\n%s",
        incident_id, raw_text[:500],
    )
    return _safe_default(f"LLM returned unparseable response. Raw:\n{raw_text[:300]}")


def _safe_default(reason: str) -> dict:
    return {
        "root_cause": reason,
        "severity": "high",
        "suggested_fix": "Manual investigation required — automated parse failed.",
        "auto_restart_safe": False,
        "config_suggestions": [],
        "likely_recurring": False,
        "estimated_impact": "Unknown — automated diagnosis failed.",
        "matches_past_incident": None,
        "confidence_boost": "none",
    }


# ── Provider 1: GCP Vertex AI (google-genai SDK with vertexai=True) ───────────

_vertex_client: genai.Client | None = None


def _get_vertex_client() -> genai.Client | None:
    """Initialize and return singleton google-genai Client configured for Vertex AI."""
    global _vertex_client
    if _vertex_client is not None:
        return _vertex_client
    if not GENAI_SDK_AVAILABLE:
        return None
    try:
        adc_path = os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/etc/gcp/credentials.json"
        )
        if os.path.exists(adc_path) and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path

        _vertex_client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=VERTEX_LOCATION,
        )
        return _vertex_client
    except Exception as exc:
        logger.warning("Failed to initialize Vertex AI genai client: %s", exc)
        return None


async def call_vertex_ai(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict | None:
    """
    Call GCP Vertex AI using official google-genai SDK (gemini-2.5-flash).
    Uses GCP $300 credits — completes in ~3.9 seconds.
    """
    client = _get_vertex_client()
    if not client:
        logger.warning("[%s] Vertex AI client not available", incident_id)
        return None

    prompt = build_prompt(pod_context, cleaned_logs, events, past_incidents)
    logger.info("[%s] 🚀 Sending request to GCP Vertex AI (%s)", incident_id, VERTEX_MODEL)
    t_start = time.monotonic()

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=VERTEX_MODEL,
                contents=prompt,
            )
        )
        raw_text = response.text or ""
        elapsed = time.monotonic() - t_start
        logger.info("[%s] ⚡ Vertex AI responded in %.2fs!", incident_id, elapsed)
        return parse_llm_response(raw_text, incident_id)
    except Exception as exc:
        logger.warning("[%s] Vertex AI request error: %s", incident_id, exc)
        return None


# ── Provider 2: Gemini API Key Provider ────────────────────────────────────────

async def call_gemini(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict | None:
    """
    Call Google Gemini API Key REST API (gemini-2.0-flash / gemini-1.5-flash).
    """
    if not GEMINI_API_KEY:
        logger.warning("[%s] Gemini API key not configured", incident_id)
        return None

    prompt = build_prompt(pod_context, cleaned_logs, events, past_incidents)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
        }
    }

    logger.info("[%s] Sending request to Cloud Gemini API (model=%s)", incident_id, GEMINI_MODEL)
    t_start = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)

            if resp.status_code != 200:
                logger.warning(
                    "[%s] Gemini API returned HTTP %d: %s",
                    incident_id, resp.status_code, resp.text[:200]
                )
                return None

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning("[%s] Gemini API returned no candidates", incident_id)
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                logger.warning("[%s] Gemini candidate content empty", incident_id)
                return None

            raw_text = parts[0].get("text", "")
            elapsed = time.monotonic() - t_start
            logger.info("[%s] ⚡ Gemini API responded in %.2fs!", incident_id, elapsed)
            return parse_llm_response(raw_text, incident_id)

    except Exception as exc:
        logger.warning("[%s] Gemini API request failed: %s", incident_id, exc)
        return None


# ── Provider 3: Local Ollama Client ────────────────────────────────────────────

async def call_ollama(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict:
    """
    Send a memory-augmented prompt to local Ollama.
    Enforces the global semaphore to cap concurrent CPU usage.
    """
    prompt = build_prompt(pod_context, cleaned_logs, events, past_incidents)
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }

    logger.info("[%s] Queuing Ollama request (model=%s)", incident_id, OLLAMA_MODEL)
    t_start = time.monotonic()

    async with _ollama_semaphore:
        logger.info("[%s] Acquired semaphore — sending to Ollama", incident_id)
        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "")
        except httpx.TimeoutException:
            logger.error("[%s] Ollama request timed out after %.0fs", incident_id, OLLAMA_TIMEOUT)
            return _safe_default("Ollama timeout — inference took too long.")
        except Exception as exc:
            logger.error("[%s] Ollama request failed: %s", incident_id, exc)
            return _safe_default(f"Ollama request error: {exc}")

    elapsed = time.monotonic() - t_start
    logger.info("[%s] Ollama responded in %.1fs", incident_id, elapsed)
    return parse_llm_response(raw, incident_id)


# ── Unified Multi-Provider Entrypoint ──────────────────────────────────────────

async def call_llm(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict:
    """
    Unified entrypoint for LLM diagnosis.
    Priority: Vertex AI (GCP $300 credits) → Gemini API Key → Local Ollama.
    """
    provider = LLM_PROVIDER
    if provider == "auto":
        # Check if Vertex AI client is available
        if _get_vertex_client():
            provider = "vertex"
        elif GEMINI_API_KEY:
            provider = "gemini"
        else:
            provider = "ollama"

    if provider == "vertex":
        result = await call_vertex_ai(pod_context, cleaned_logs, events, incident_id, past_incidents)
        if result is not None:
            return result
        logger.info("[%s] Vertex AI provider failed — trying fallback provider...", incident_id)

    if provider in ("gemini", "vertex"):
        result = await call_gemini(pod_context, cleaned_logs, events, incident_id, past_incidents)
        if result is not None:
            return result
        logger.info("[%s] Cloud providers unavailable or failed — falling back to local Ollama...", incident_id)

    # Final fallback or explicit Ollama execution
    return await call_ollama(pod_context, cleaned_logs, events, incident_id, past_incidents)
