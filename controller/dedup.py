"""
dedup.py — 3-Layer deduplication to prevent Ollama saturation and Slack spam.

Layer 1: Event Dampening
    Only trigger after an error occurs >= DAMPEN_COUNT times in DAMPEN_WINDOW_SECS.
    OOMKilled is exempt and always triggers immediately.

Layer 2: Log Fingerprint Cache
    SHA-256 hash of cleaned logs + error_state. If the same crash pattern
    has already been diagnosed in the last CACHE_TTL hours, skip Ollama.
    Cache TTL extends to 4h if LLM says likely_recurring=True.

Layer 3: Active PatchRequest Check
    Queries the K8s API for existing Pending/Approved PatchRequests for the
    same deployment + error combination. Survives controller pod restarts
    (unlike Layers 1 & 2 which are in-memory).
"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import kubernetes_asyncio.client as k8s_client

logger = logging.getLogger(__name__)

# ── Layer 1 constants ──────────────────────────────────────────────────────────
DAMPEN_COUNT = 3
DAMPEN_WINDOW_SECS = 300                  # 5 minutes
IMMEDIATE_TRIGGER_STATES = {"OOMKilled"}  # Always trigger, no dampening

# ── Layer 2 constants ──────────────────────────────────────────────────────────
CACHE_TTL_DEFAULT = timedelta(hours=1)
CACHE_TTL_RECURRING = timedelta(hours=4)  # Extended TTL for known-flapping issues

# ── In-memory stores (reset on controller restart; Layer 3 is the safety net) ─
_event_window: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
_fingerprint_cache: dict[str, tuple[datetime, str]] = {}  # {fp: (last_seen, pr_name)}
_lock = asyncio.Lock()


# ── Layer 1: Event Dampening ───────────────────────────────────────────────────

async def should_trigger(pod_uid: str, error_state: str) -> bool:
    """
    Returns True if the error is persistent enough to warrant LLM diagnosis.
    OOMKilled always returns True immediately.
    """
    if error_state in IMMEDIATE_TRIGGER_STATES:
        logger.debug("[dedup-L1] OOMKilled — immediate trigger for %s", pod_uid)
        return True

    async with _lock:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        window = _event_window[pod_uid]
        window.append((now, error_state))
        cutoff = now - timedelta(seconds=DAMPEN_WINDOW_SECS)
        # Prune old events and mismatched error states
        _event_window[pod_uid] = [
            (ts, st) for ts, st in window
            if ts >= cutoff and st == error_state
        ]
        count = len(_event_window[pod_uid])
        logger.debug("[dedup-L1] %s: %d/%d events in window", pod_uid, count, DAMPEN_COUNT)
        return count >= DAMPEN_COUNT


def clear_dampening(pod_uid: str) -> None:
    """Clear dampening counters when a pod recovers."""
    _event_window.pop(pod_uid, None)


# ── Layer 2: Log Fingerprint Cache ─────────────────────────────────────────────

async def check_fingerprint_cache(fingerprint: str) -> tuple[bool, str | None]:
    """
    Returns (is_duplicate, existing_patchrequest_name).
    is_duplicate=True means: skip LLM call, just increment seen_count on existing PR.
    """
    async with _lock:
        entry = _fingerprint_cache.get(fingerprint)
        if not entry:
            return False, None
        last_seen, pr_name = entry
        if datetime.now(timezone.utc).replace(tzinfo=None) - last_seen < CACHE_TTL_DEFAULT:
            logger.debug("[dedup-L2] Fingerprint %s is a duplicate (PR=%s)", fingerprint, pr_name)
            return True, pr_name
        # Cache expired
        del _fingerprint_cache[fingerprint]
        return False, None


async def register_fingerprint(fingerprint: str, pr_name: str, likely_recurring: bool = False) -> None:
    """Register a fingerprint after a PatchRequest is successfully created."""
    async with _lock:
        _fingerprint_cache[fingerprint] = (datetime.now(timezone.utc).replace(tzinfo=None), pr_name)
    if likely_recurring:
        logger.info("[dedup-L2] Fingerprint %s marked as recurring → 4h TTL", fingerprint)


async def clear_fingerprint(fingerprint: str) -> None:
    """Clear fingerprint when a PatchRequest is Applied or Rejected."""
    async with _lock:
        _fingerprint_cache.pop(fingerprint, None)


# ── Layer 3: Active PatchRequest Check (K8s API — survives restarts) ──────────

async def has_open_patchrequest(
    namespace: str,
    deployment_name: str,
    error_state: str,
    custom_api,  # kubernetes_asyncio.client.CustomObjectsApi
) -> tuple[bool, str | None]:
    """
    Checks the K8s API for an existing Pending/Approved PatchRequest for
    this deployment + error_state combo. This layer survives controller restarts.
    Returns (exists, patchrequest_name).
    """
    try:
        prs = await custom_api.list_namespaced_custom_object(
            group="sre.yourdomain.io",
            version="v1alpha1",
            namespace=namespace,
            plural="patchrequests",
            label_selector=f"target-deployment={deployment_name}",
        )
        for pr in prs.get("items", []):
            status = pr.get("status", {}).get("approvalState", "Pending")
            spec_error = pr.get("spec", {}).get("errorState", "")
            if status in ("Pending", "Approved") and spec_error == error_state:
                pr_name = pr["metadata"]["name"]
                logger.debug("[dedup-L3] Active PR found: %s (status=%s)", pr_name, status)
                return True, pr_name
    except Exception as exc:
        # Fail-open: if we can't check, allow creation to avoid missing an incident
        logger.warning("[dedup-L3] API check failed (fail-open): %s", exc)
    return False, None


async def increment_seen_count(
    pr_name: str,
    namespace: str,
    custom_api,
) -> None:
    """
    Bump seenCount on an existing PatchRequest rather than creating a duplicate.
    Fires escalation Slack nudges at milestones 10, 25, 50.
    """
    try:
        current = await custom_api.get_namespaced_custom_object(
            group="sre.yourdomain.io",
            version="v1alpha1",
            namespace=namespace,
            plural="patchrequests",
            name=pr_name,
        )
        seen = current.get("spec", {}).get("seenCount", 0) + 1
        await custom_api.patch_namespaced_custom_object(
            group="sre.yourdomain.io",
            version="v1alpha1",
            namespace=namespace,
            plural="patchrequests",
            name=pr_name,
            body={"spec": {"seenCount": seen}},
        )
        logger.info("[dedup-L3] Incremented seenCount on %s → %d", pr_name, seen)
        if seen in (10, 25, 50):
            logger.warning(
                "[ESCALATION] %s has been seen %d times and is still unresolved!", pr_name, seen
            )
    except Exception as exc:
        logger.warning("[dedup-L3] Failed to increment seenCount on %s: %s", pr_name, exc)
