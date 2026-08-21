"""
triage.py — Sorts an incoming failure signal into a "domain" before diagnosis.

Why this exists (in plain words):
    "Diagnose why this pod crashed" and "diagnose why this node is unhealthy"
    are different jobs that need different context and different prompts.
    This module answers one question first: what KIND of problem is this?

    Most of the time the answer is obvious for free — a signal that came from
    watching Pods is a "pod" problem, no thinking required. We only spend an
    LLM call when the signal genuinely doesn't say what it is.
"""

from __future__ import annotations
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class Domain(str, Enum):
    POD = "pod"
    NODE = "node"
    CLUSTER = "cluster"
    APP = "app"


# Which watcher a signal came from tells us the domain for free, no LLM needed.
_SOURCE_TO_DOMAIN = {
    "pod_status": Domain.POD,
    "init_container_status": Domain.POD,
    "node_condition": Domain.NODE,
    "cluster_event": Domain.CLUSTER,
    "prometheus_alert": Domain.APP,
}


def classify_domain(source: str, signal: dict) -> tuple[Domain, dict]:
    """
    Returns (domain, resource_ref).

    `source` identifies which watcher produced this signal — this is set by
    the caller in main.py, not guessed from the signal's contents, since the
    caller already knows exactly which K8s watch handler fired.

    `resource_ref` is a small dict identifying what's actually broken, in a
    domain-appropriate shape:
        pod/app domain     -> {"namespace": ..., "deployment": ...}
        node domain        -> {"node": ...}
        cluster domain     -> {"namespace": ..., "kind": ..., "name": ...}
    """
    domain = _SOURCE_TO_DOMAIN.get(source)
    if domain is not None:
        return domain, _extract_resource_ref(domain, signal)

    # Signal didn't come from a watcher we recognize — this is the rare,
    # genuinely ambiguous case. Fall back to a small LLM call rather than
    # guessing with more rules that would just be fragile.
    logger.warning("[triage] Unrecognized signal source %r — falling back to LLM classification", source)
    domain = _llm_classify(signal)
    return domain, _extract_resource_ref(domain, signal)


def _extract_resource_ref(domain: Domain, signal: dict) -> dict:
    if domain == Domain.NODE:
        return {"node": signal.get("node_name")}
    if domain == Domain.CLUSTER:
        return {
            "namespace": signal.get("namespace"),
            "kind": signal.get("involved_kind"),
            "name": signal.get("involved_name"),
        }
    # POD and APP domains both key off namespace/deployment
    return {
        "namespace": signal.get("namespace"),
        "deployment": signal.get("deployment"),
    }


def _llm_classify(signal: dict) -> Domain:
    """
    Placeholder for the LLM tie-breaker path. Not yet wired to a real model
    call — every signal source used in Phase 1 (pod watcher) resolves via
    the rule table above, so this path is currently unreachable in practice.
    Kept as an explicit, logged fallback (defaulting to POD, the
    lowest-blast-radius domain) rather than silently guessing, so that if a
    future watcher forgets to register itself in _SOURCE_TO_DOMAIN, that
    shows up loudly in logs instead of being fixed with an assumption.
    """
    logger.error("[triage] LLM classification not yet implemented — defaulting to Domain.POD")
    return Domain.POD
