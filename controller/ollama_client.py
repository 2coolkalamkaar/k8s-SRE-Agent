"""
ollama_client.py — Async Ollama HTTP client with memory-augmented prompts.

Features:
- asyncio.Semaphore(3): max 3 concurrent Ollama calls on 16GB CPU cluster
- 5-layer fallback JSON parser: never raises, always returns a safe default
- Memory-augmented prompting: injects top-3 past incidents as few-shot context
- Configurable Ollama endpoint via OLLAMA_URL env var
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://ollama-service.ai-infra.svc.cluster.local:11434"
)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-coder:6.7b-instruct")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))

# Gate: max 3 simultaneous inferences to prevent CPU starvation on 16GB host
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
    Parse Ollama response with 5 fallback strategies.
    NEVER raises — always returns a dict (safe default on total failure).
    """
    if not raw_text or not raw_text.strip():
        logger.warning("[%s] Empty response from Ollama", incident_id)
        return _safe_default("Empty response from Ollama")

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

    # Total failure — return a safe default so the incident is never silently lost
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


# ── Main Async Client ──────────────────────────────────────────────────────────

async def call_ollama(
    pod_context: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict:
    """
    Send a memory-augmented prompt to Ollama and return parsed JSON diagnosis.
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
