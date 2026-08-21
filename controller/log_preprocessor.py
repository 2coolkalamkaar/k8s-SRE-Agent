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

        # Generic non-zero exit — normalise to CrashLoopBackOff so the dampening
        # window counter accumulates even when kubelet alternates between the
        # terminated phase (exitCode != 0) and the waiting phase (CrashLoopBackOff).
        if terminated.get("exitCode", 0) not in (0, None):
            return "CrashLoopBackOff"

    return None


# Node conditions that mean "something is wrong" when in this state.
# Ready is inverted (False/Unknown = bad); the rest are True = bad.
_BAD_NODE_CONDITIONS = {
    "Ready": "False",       # or "Unknown" — checked separately below
    "DiskPressure": "True",
    "MemoryPressure": "True",
    "PIDPressure": "True",
    "NetworkUnavailable": "True",
}

# Node conditions carry a lastHeartbeatTime that updates every ~40s from the
# kubelet even when nothing is actually wrong. Stripping it out before
# comparing old vs new prevents re-triggering diagnosis on every heartbeat —
# the same problem pod dedup already solves for crash loops, applied here to
# avoid spamming diagnosis for a node that's been unhealthy for hours.
def strip_heartbeat(conditions: list[dict]) -> list[tuple]:
    return sorted(
        (c.get("type"), c.get("status"), c.get("reason")) for c in (conditions or [])
    )


def detect_node_condition(conditions: list[dict]) -> str | None:
    """
    Parse a Node's status.conditions list and return the canonical problem
    name if the node is unhealthy, else None. Mirrors detect_error_state's
    shape/contract but for nodes instead of pods.
    """
    if not conditions:
        return None

    by_type = {c.get("type"): c.get("status") for c in conditions}

    ready = by_type.get("Ready")
    if ready in ("False", "Unknown"):
        return "NodeNotReady"

    for cond_type, bad_status in _BAD_NODE_CONDITIONS.items():
        if cond_type == "Ready":
            continue
        if by_type.get(cond_type) == bad_status:
            return cond_type  # e.g. "DiskPressure"

    return None


# Kubernetes resource quantity suffixes, mapped to a multiplier that converts
# them to a common base unit so "500m" and "1" (for the same resource key)
# can be compared correctly. Binary (Ki/Mi/Gi/...) and decimal SI (m/k/M/...)
# suffixes are both used by the API depending on the resource type.
_QUANTITY_SUFFIXES = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}


def _parse_k8s_quantity(qty: str) -> float:
    """Parse a Kubernetes resource quantity string (e.g. '500m', '2Gi', '10')
    into a plain float in a consistent base unit. Returns 0.0 on anything
    unparseable rather than raising — a quota check that can't parse a
    value should skip it, not crash the whole watcher."""
    match = re.match(r"^([0-9.eE+-]+)([a-zA-Z]*)$", (qty or "").strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    try:
        return float(number) * _QUANTITY_SUFFIXES.get(suffix, 1)
    except ValueError:
        return 0.0


def detect_quota_pressure(used: dict, hard: dict, near_limit_ratio: float = 0.9) -> str | None:
    """
    Compare a ResourceQuota's status.used against status.hard for every
    tracked resource. Returns 'ResourceQuotaExceeded' if any resource is at
    or over its limit, 'ResourceQuotaNearLimit' if any is within
    near_limit_ratio of it (early warning, before anything actually starts
    failing to schedule), else None.
    """
    exceeded = False
    near_limit = False
    for key, hard_val in (hard or {}).items():
        used_val = used.get(key)
        if used_val is None:
            continue
        hard_qty = _parse_k8s_quantity(hard_val)
        used_qty = _parse_k8s_quantity(used_val)
        if hard_qty <= 0:
            continue
        ratio = used_qty / hard_qty
        if ratio >= 1.0:
            exceeded = True
        elif ratio >= near_limit_ratio:
            near_limit = True

    if exceeded:
        return "ResourceQuotaExceeded"
    if near_limit:
        return "ResourceQuotaNearLimit"
    return None


def detect_init_error_state(init_container_statuses: list[dict]) -> str | None:
    """
    Parse Kubernetes initContainerStatuses and return a canonical error state
    if any init container is failing, else None.

    Init containers don't CrashLoopBackOff the same way main containers do —
    they cycle through Terminated(Error) → Waiting(PodInitializing) → repeat.
    We surface this as 'InitCrashLoopBackOff' so the LLM gets a clear signal.
    """
    if not init_container_statuses:
        return None

    for cs in init_container_statuses:
        state = cs.get("state", {})
        last_state = cs.get("lastState", {})

        waiting = state.get("waiting", {})
        # Kubernetes labels the pod Init:CrashLoopBackOff when an init container
        # keeps failing — the container's own waiting reason will be CrashLoopBackOff
        if waiting.get("reason") in ("CrashLoopBackOff", "Error"):
            return "InitCrashLoopBackOff"

        # Init container terminated with non-zero exit — about to be retried
        terminated = state.get("terminated", {})
        if terminated.get("exitCode", 0) not in (0, None):
            return "InitCrashLoopBackOff"

        # Also check lastState for init containers that are being retried
        last_terminated = last_state.get("terminated", {})
        if last_terminated.get("exitCode", 0) not in (0, None):
            return "InitCrashLoopBackOff"

    return None
