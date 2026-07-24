"""
log_preprocessor.py — Cleans raw pod logs for efficient LLM consumption.

Key responsibilities:
1. Strip noisy timestamps and repeated lines.
2. Keep only actionable signal (Exception / Error / FATAL / Caused by lines).
3. SHA-256 fingerprint the cleaned log for deduplication.
4. Enforce a token budget so the LLM context window is never exhausted.
"""

from __future__ import annotations
import hashlib
import re
import logging

logger = logging.getLogger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Lines we KEEP (actionable signal)
_KEEP_PATTERN = re.compile(
    r"(exception|error|fatal|critical|caused by|traceback|oomkilled|"
    r"killed|sigkill|panic|segfault|connection refused|timeout|"
    r"secret.*not found|image.*not found|failed|exit code [^0])",
    re.IGNORECASE,
)

# Timestamp formats we STRIP (prefix-only, so the rest of the line survives)
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
    r"(\[?\w+\]?\s*)?"  # optional log level like [INFO] or INFO
)

# Lines that are pure noise (health checks, normal start-up chatter)
_NOISE_PATTERN = re.compile(
    r"(health.?check|liveness|readiness|GET /|POST /|prometheus|"
    r"metrics|starting up|listening on|server config|level=INFO)",
    re.IGNORECASE,
)

MAX_LINES = 100          # Maximum lines sent to LLM
MAX_CHARS = 4000         # Rough token budget guard (~1000 tokens at 4 chars/token)


# ── Public API ─────────────────────────────────────────────────────────────────

def preprocess_logs(raw_logs: str, error_state: str = "") -> str:
    """
    Clean raw pod logs and return a compact, signal-rich excerpt.

    Steps:
        1. Split into lines, strip timestamps.
        2. Drop pure-noise lines.
        3. Prefer lines that match error/exception patterns.
        4. Deduplicate consecutive identical lines.
        5. Truncate to MAX_LINES / MAX_CHARS.
    """
    if not raw_logs or not raw_logs.strip():
        return f"[No logs available — error state: {error_state}]"

    lines = raw_logs.splitlines()
    cleaned: list[str] = []
    last_line = ""

    for raw_line in lines:
        # Strip leading timestamp
        line = _TIMESTAMP_PATTERN.sub("", raw_line).strip()
        if not line:
            continue
        # Skip pure noise
        if _NOISE_PATTERN.search(line):
            continue
        # Deduplicate consecutive identical lines
        if line == last_line:
            continue
        last_line = line
        cleaned.append(line)

    if not cleaned:
        # If everything was noise, fall back to raw (truncated)
        logger.warning("log_preprocessor: all lines were noise, using raw fallback")
        cleaned = [l.strip() for l in lines if l.strip()][:MAX_LINES]

    # Prefer signal lines, but keep the full list for context
    signal_lines = [l for l in cleaned if _KEEP_PATTERN.search(l)]
    other_lines  = [l for l in cleaned if not _KEEP_PATTERN.search(l)]

    # Build output: signal lines first, then context lines up to budget
    output_lines = signal_lines + other_lines
    output_lines = output_lines[:MAX_LINES]

    result = "\n".join(output_lines)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n... [truncated]"

    logger.debug("log_preprocessor: %d raw lines → %d cleaned lines", len(lines), len(output_lines))
    return result


def make_fingerprint(cleaned_logs: str, error_state: str) -> str:
    """
    Create a stable 16-hex-char fingerprint of the crash pattern.
    Used as Layer-2 dedup key: same crash pattern → same fingerprint.
    """
    content = f"{error_state}::{cleaned_logs}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def detect_error_state(container_statuses: list[dict]) -> str | None:
    """
    Parse Kubernetes containerStatuses list and return the canonical
    error state string if the pod is unhealthy, else None.
    """
    if not container_statuses:
        return None

    for cs in container_statuses:
        state = cs.get("state", {})
        last_state = cs.get("lastState", {})

        # CrashLoopBackOff
        waiting = state.get("waiting", {})
        if waiting.get("reason") in ("CrashLoopBackOff", "Error"):
            return "CrashLoopBackOff"

        # ImagePullBackOff
        if waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull"):
            return "ImagePullBackOff"

        # CreateContainerConfigError (missing secret / configmap)
        if waiting.get("reason") == "CreateContainerConfigError":
            return "CreateContainerConfigError"

        # OOMKilled (check lastState.terminated)
        terminated = last_state.get("terminated", {})
        if terminated.get("reason") == "OOMKilled":
            return "OOMKilled"

        # Generic non-zero exit
        if terminated.get("exitCode", 0) not in (0, None):
            return "ContainerCrashed"

    return None
