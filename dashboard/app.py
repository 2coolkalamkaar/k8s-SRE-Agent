"""
app.py — SRE Agent Dashboard API Backend

FastAPI service that proxies the Kubernetes API to serve live PatchRequest data
to the frontend. Runs inside the cluster using the sre-observer-sa ServiceAccount.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from kubernetes_asyncio import client as k8s_client, config as k8s_config

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SRE Agent Dashboard", version="1.0.0")

CRD_GROUP = "sre.yourdomain.io"
CRD_VERSION = "v1alpha1"
WATCH_NAMESPACE = os.getenv("WATCH_NAMESPACES", "production")

_K8S_CONFIGURED = False


async def _ensure_k8s():
    global _K8S_CONFIGURED
    if not _K8S_CONFIGURED:
        try:
            k8s_config.load_incluster_config()
        except Exception:
            await k8s_config.load_kube_config()  # local dev fallback
        _K8S_CONFIGURED = True


def _format_age(ts: str) -> str:
    """Convert an ISO timestamp to a human-readable 'X minutes ago' string."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        total = int(delta.total_seconds())
        if total < 60:
            return f"{total}s ago"
        if total < 3600:
            return f"{total // 60}m ago"
        return f"{total // 3600}h ago"
    except Exception:
        return ts


def _severity_order(sev: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(sev, 4)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/incidents")
async def list_incidents():
    """List all PatchRequests across all watched namespaces, sorted by severity."""
    await _ensure_k8s()
    custom_api = k8s_client.CustomObjectsApi()
    try:
        prs = await custom_api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural="patchrequests"
        )
        items = []
        for pr in prs.get("items", []):
            spec = pr.get("spec", {})
            status = pr.get("status", {})
            meta = pr.get("metadata", {})
            diagnosis = spec.get("llmDiagnosis", {})
            items.append({
                "name": meta.get("name"),
                "namespace": meta.get("namespace"),
                "incidentId": spec.get("incidentId"),
                "deployment": spec.get("targetDeployment"),
                "errorState": spec.get("errorState"),
                "severity": spec.get("severity", "unknown"),
                "approvalState": status.get("approvalState", "Pending"),
                "rootCause": spec.get("rootCause", ""),
                "suggestedFix": diagnosis.get("suggested_fix", ""),
                "confidence": spec.get("confidence", ""),
                "seenCount": spec.get("seenCount", 1),
                "autoRestartSafe": spec.get("autoRestartSafe", False),
                "likelyRecurring": spec.get("likelyRecurring", False),
                "age": _format_age(meta.get("creationTimestamp", "")),
                "createdAt": meta.get("creationTimestamp"),
                "mttrSeconds": status.get("mttrSeconds"),
            })
        items.sort(key=lambda x: (
            {"Pending": 0, "Approved": 1, "Applied": 2, "Closed": 3, "Rejected": 4}.get(x["approvalState"], 5),
            _severity_order(x["severity"])
        ))
        return {"incidents": items, "total": len(items)}
    except Exception as exc:
        logger.error("Error listing PatchRequests: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/incidents/{name}")
async def get_incident(name: str, namespace: str = "production"):
    """Get the full detail of a single PatchRequest by name."""
    await _ensure_k8s()
    custom_api = k8s_client.CustomObjectsApi()
    try:
        pr = await custom_api.get_namespaced_custom_object(
            group=CRD_GROUP, version=CRD_VERSION,
            namespace=namespace, plural="patchrequests", name=name
        )
        spec = pr.get("spec", {})
        status = pr.get("status", {})
        meta = pr.get("metadata", {})
        diagnosis = spec.get("llmDiagnosis", {})
        return {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "incidentId": spec.get("incidentId"),
            "deployment": spec.get("targetDeployment"),
            "targetNamespace": spec.get("targetNamespace"),
            "errorState": spec.get("errorState"),
            "severity": spec.get("severity"),
            "approvalState": status.get("approvalState", "Pending"),
            "approvedBy": status.get("approvedBy"),
            "rootCause": spec.get("rootCause", ""),
            "suggestedFix": diagnosis.get("suggested_fix", ""),
            "humanNote": spec.get("humanNote", ""),
            "proposedPatch": spec.get("proposedPatch", {}),
            "confidence": spec.get("confidence", ""),
            "seenCount": spec.get("seenCount", 1),
            "autoRestartSafe": spec.get("autoRestartSafe", False),
            "likelyRecurring": spec.get("likelyRecurring", False),
            "mttrSeconds": status.get("mttrSeconds"),
            "age": _format_age(meta.get("creationTimestamp", "")),
            "createdAt": meta.get("creationTimestamp"),
            "appliedAt": status.get("appliedAt"),
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"PatchRequest {name} not found: {exc}")


@app.get("/api/stats")
async def get_stats():
    """Return aggregate statistics for the dashboard header."""
    await _ensure_k8s()
    custom_api = k8s_client.CustomObjectsApi()
    try:
        prs = await custom_api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural="patchrequests"
        )
        items = prs.get("items", [])
        total = len(items)
        active = sum(1 for i in items if i.get("status", {}).get("approvalState") in ("Pending", "Approved", "Applied"))
        closed = sum(1 for i in items if i.get("status", {}).get("approvalState") == "Closed")
        mttr_values = [
            i.get("status", {}).get("mttrSeconds")
            for i in items
            if i.get("status", {}).get("mttrSeconds")
        ]
        avg_mttr = int(sum(mttr_values) / len(mttr_values)) if mttr_values else None
        auto_resolve_rate = round(closed / total * 100, 1) if total > 0 else 0

        return {
            "total": total,
            "active": active,
            "closed": closed,
            "avgMttrSeconds": avg_mttr,
            "autoResolveRate": auto_resolve_rate,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Static Files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")
