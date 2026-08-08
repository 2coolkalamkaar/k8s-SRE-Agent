"""
main.py — Kopf Operator entry point for the K8s AI SRE Agent.

Handlers:
  on_startup          — Catch-up scan for pods that failed during controller downtime.
  on_pod_status_change — Main watch handler: runs the full 3-layer dedup + LLM pipeline.
  on_patchrequest_approved — Watches for SRE-approved PatchRequests and applies the patch.

Flow:
  K8s event fires
      → Layer 1: Event dampening (skip self-healing blips)
      → Fetch + clean logs
      → Layer 2: Fingerprint cache (skip if same crash already diagnosed)
      → Layer 3: Active PR check (skip if PR already open for this deployment)
      → Call Ollama → parse JSON → create PatchRequest CRD → create IncidentRecord CRD
      → Log Slack notification (stub: replace with real Slack client)
"""

from __future__ import annotations
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import kopf
import kubernetes_asyncio as k8s

# ── K8s client initialisation (done once at startup) ──────────────────────────
_K8S_CONFIGURED = False

async def _ensure_k8s_configured() -> None:
    """Load in-cluster config once; idempotent."""
    global _K8S_CONFIGURED
    if not _K8S_CONFIGURED:
        try:
            k8s_config.load_incluster_config()   # synchronous in k8s_asyncio ≥ 24
        except Exception:
            await k8s_config.load_kube_config()
        _K8S_CONFIGURED = True
from kubernetes_asyncio import client as k8s_client, config as k8s_config

from controller.dedup import (
    check_fingerprint_cache,
    clear_dampening,
    has_open_patchrequest,
    increment_seen_count,
    register_fingerprint,
    should_trigger,
)
from controller.incident import Incident
from controller.log_preprocessor import (
    detect_error_state,
    make_fingerprint,
    preprocess_logs,
)
from controller.llm_client import diagnose_incident
import controller.telemetry as telemetry
import controller.outcome_checker  # noqa: F401 (Ensure kopf discovers the timer)

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
WATCH_NAMESPACES = os.getenv("WATCH_NAMESPACES", "production").split(",")
CRD_GROUP = "sre.yourdomain.io"
CRD_VERSION = "v1alpha1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_incident_id() -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    suffix = str(uuid.uuid4())[:4].upper()
    return f"INC-{now.year}-{now.strftime('%m%d')}-{suffix}"


def _get_owner_deployment(body: dict) -> str | None:
    """Trace pod → ReplicaSet → Deployment owner reference chain."""
    for ref in body.get("metadata", {}).get("ownerReferences", []):
        if ref.get("kind") == "ReplicaSet":
            # Return the RS name; the caller strips the pod-hash suffix
            rs_name = ref.get("name", "")
            # Heuristic: strip last two hyphen-separated segments (hash + pod-id)
            parts = rs_name.rsplit("-", 1)
            return parts[0] if len(parts) > 1 else rs_name
    return None


async def _fetch_pod_logs(pod_name: str, namespace: str, v1: k8s_client.CoreV1Api) -> str:
    """Fetch previous container logs (the crash logs). Falls back to current logs."""
    try:
        logs = await v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            previous=True,
            tail_lines=200,
        )
        return logs
    except Exception:
        logger.debug("No previous logs available for %s/%s, falling back to current logs", namespace, pod_name)
    try:
        logs = await v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=200,
        )
        return logs
    except Exception as exc:
        logger.warning("Could not fetch logs for %s/%s: %s", namespace, pod_name, exc)
        return ""


async def _fetch_pod_events(pod_name: str, namespace: str, v1: k8s_client.CoreV1Api) -> str:
    """Fetch K8s events related to the specific pod."""
    try:
        events = await v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        lines = [
            f"{e.type} {e.reason}: {e.message}"
            for e in events.items
        ]
        return "\n".join(lines[-20:])  # Last 20 events
    except Exception as exc:
        logger.warning("Could not fetch events for %s/%s: %s", namespace, pod_name, exc)
        return ""


async def _build_pod_context(body: dict, container_statuses: list) -> dict:
    """Build the structured context dict sent to the LLM prompt."""
    spec = body.get("spec", {})
    containers = spec.get("containers", [{}])
    container = containers[0] if containers else {}
    resources = container.get("resources", {})
    limits = resources.get("limits", {})
    env_vars = [e.get("name") for e in container.get("env", [])]

    restart_count = 0
    for cs in container_statuses:
        restart_count = max(restart_count, cs.get("restartCount", 0))

    return {
        "pod_name": body["metadata"]["name"],
        "namespace": body["metadata"]["namespace"],
        "error_state": detect_error_state(container_statuses),
        "restart_count": restart_count,
        "cpu_limit": limits.get("cpu", "N/A"),
        "mem_limit": limits.get("memory", "N/A"),
        "env_vars": env_vars,
    }


async def _create_patch_request_crd(
    namespace: str,
    deployment_name: str,
    incident_id: str,
    diagnosis: dict,
    custom_api: k8s_client.CustomObjectsApi,
) -> str:
    """Create a PatchRequest CRD object in K8s and return its name."""
    pr_name = f"{deployment_name}-{incident_id.lower().replace('inc-', 'pr-')}"
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "PatchRequest",
        "metadata": {
            "name": pr_name,
            "namespace": namespace,
            "labels": {
                "target-deployment": deployment_name,
                "incident-id": incident_id,
            },
        },
        "spec": {
            "incidentId": incident_id,
            "targetDeployment": deployment_name,
            "targetNamespace": namespace,
            "errorState": diagnosis.get("error_state", ""),
            "rootCause": diagnosis.get("root_cause", "")[:500],
            "severity": diagnosis.get("severity", "high"),
            "confidence": "high" if diagnosis.get("confidence_boost") == "high" else "medium",
            "seenCount": 1,
            "llmSummary": diagnosis.get("suggested_fix", "")[:500],
            "humanNote": diagnosis.get("estimated_impact", ""),
            "autoRestartSafe": diagnosis.get("auto_restart_safe", False),
            "likelyRecurring": diagnosis.get("likely_recurring", False),
            "proposedPatch": diagnosis.get("proposed_patch") or {},
            "llmDiagnosis": diagnosis,
        },
        "status": {
            "approvalState": "Pending",
        },
    }
    await custom_api.create_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="patchrequests",
        body=body,
    )
    logger.info("[%s] PatchRequest CRD created: %s", incident_id, pr_name)
    return pr_name


async def _create_incident_record_crd(
    incident: Incident,
    custom_api: k8s_client.CustomObjectsApi,
) -> None:
    """Create a lightweight IncidentRecord CRD for kubectl CLI access."""
    name = incident.incident_id.lower()
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "IncidentRecord",
        "metadata": {
            "name": name,
            "labels": {
                "deployment": incident.target_deployment,
                "error-state": incident.error_state,
                "fingerprint": incident.error_fingerprint,
            },
        },
        "spec": {
            "incidentId": incident.incident_id,
            "errorState": incident.error_state,
            "errorFingerprint": incident.error_fingerprint,
            "targetDeployment": incident.target_deployment,
            "targetNamespace": incident.target_namespace,
            "rootCause": (incident.llm_diagnosis or {}).get("root_cause", ""),
            "state": incident.state,
            "recurrenceCount": 1,
            "tags": incident.tags,
        },
    }
    try:
        await custom_api.create_cluster_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural="incidentrecords",
            body=body,
        )
        logger.info("[%s] IncidentRecord CRD created", incident.incident_id)
    except k8s.client.ApiException as exc:
        if exc.status == 409:
            logger.warning("[%s] IncidentRecord already exists, skipping", incident.incident_id)
        else:
            logger.error("[%s] Failed to create IncidentRecord: %s", incident.incident_id, exc)


# ── Startup: Catch-up scan ────────────────────────────────────────────────────

@kopf.on.startup()
async def on_startup(logger: logging.Logger, **kwargs):
    """Initialise telemetry (OTEL traces → Tempo, metrics → Prometheus)."""
    telemetry.setup_telemetry()
    logger.info("✅ Telemetry initialised — traces → Tempo, metrics → Prometheus :9090")


@kopf.on.startup()
async def catch_up_scan(logger: logging.Logger, **kwargs):
    """
    Runs once on controller startup.
    Finds any PatchRequests stuck in 'Applied' state (e.g. after a controller restart)
    and refreshes their observationStartTime so the outcome_checker timer picks them up.
    """
    try:
        await _ensure_k8s_configured()
        custom_api = k8s_client.CustomObjectsApi()
        prs = await custom_api.list_cluster_custom_object(
            group=CRD_GROUP, version=CRD_VERSION, plural="patchrequests"
        )
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        requeued = 0
        for pr in prs.get("items", []):
            state = pr.get("status", {}).get("approvalState")
            if state == "Applied":
                pr_name = pr["metadata"]["name"]
                pr_ns = pr["metadata"]["namespace"]
                logger.info("[catch-up] Re-queuing Applied PR %s/%s for outcome checking", pr_ns, pr_name)
                try:
                    await custom_api.patch_namespaced_custom_object_status(
                        group=CRD_GROUP, version=CRD_VERSION, namespace=pr_ns,
                        plural="patchrequests", name=pr_name,
                        body={"status": {"observationStartTime": now_iso}},
                        _content_type="application/merge-patch+json",
                    )
                    requeued += 1
                except Exception as e:
                    logger.warning("[catch-up] Failed to re-queue %s: %s", pr_name, e)
        if requeued:
            logger.info("[catch-up] Re-queued %d Applied PatchRequest(s) for outcome checking", requeued)
        await custom_api.api_client.close()
    except Exception as exc:
        logger.warning("[catch-up] Scan failed (non-fatal): %s", exc)




# ── Main Watch Handler ────────────────────────────────────────────────────────

@kopf.on.field("pods", field="status.containerStatuses")
async def on_pod_status_change(body, name, namespace, new, logger, **kwargs):
    """
    Fires whenever a pod's containerStatuses field changes.
    Runs the full 3-layer dedup check + LLM diagnosis pipeline.
    """
    if namespace not in WATCH_NAMESPACES:
        return

    error_state = detect_error_state(new or [])
    if not error_state:
        # Pod is healthy — clear dampening counters
        pod_uid = body["metadata"].get("uid", name)
        clear_dampening(pod_uid)
        return

    deployment_name = _get_owner_deployment(body)
    if not deployment_name:
        logger.info("[handler] Could not determine deployment for pod %s/%s, skipping", namespace, name)
        return

    pod_uid = body["metadata"].get("uid", name)
    logger.info("[handler] %s/%s → error_state=%s deployment=%s uid=%s", namespace, name, error_state, deployment_name, pod_uid)

    # ── Layer 1: Event Dampening ────────────────────────────────────────────
    if not await should_trigger(pod_uid, error_state):
        logger.info("[dedup-L1] %s/%s: not yet persistent enough, skipping", namespace, name)
        if telemetry.dedup_hits_counter:
            telemetry.dedup_hits_counter.add(1, {"layer": "l1_dampening", "namespace": namespace})
        return

    logger.info("[dedup-L1] ✅ %s/%s: dampening threshold crossed — queuing diagnosis pipeline", namespace, name)
    await asyncio.create_task(
        _run_diagnosis_pipeline(
            pod_name=name,
            namespace=namespace,
            deployment_name=deployment_name,
            error_state=error_state,
            body=body,
            container_statuses=new or [],
        )
    )


# ── Core Diagnosis Pipeline ───────────────────────────────────────────────────

async def _run_diagnosis_pipeline(
    pod_name: str,
    namespace: str,
    deployment_name: str,
    error_state: str,
    body: dict,
    container_statuses: list,
):
    """
    Full pipeline: fetch logs → preprocess → dedup L2/L3 → call Ollama
    → create PatchRequest CRD → create IncidentRecord CRD.
    """
    await _ensure_k8s_configured()
    v1 = k8s_client.CoreV1Api()
    custom_api = k8s_client.CustomObjectsApi()

    telemetry_tracer = telemetry.get_tracer()
    with telemetry_tracer.start_as_current_span(
        "sre.diagnosis.pipeline",
        attributes={
            "deployment": deployment_name,
            "namespace": namespace,
            "error_state": error_state,
            "pod": pod_name,
        },
    ) as pipeline_span:
        try:
            # ── Fetch pod context ─────────────────────────────────────────────
            raw_logs = await _fetch_pod_logs(pod_name, namespace, v1)
            events_text = await _fetch_pod_events(pod_name, namespace, v1)
            pod_context = await _build_pod_context(body, container_statuses)
            pod_context["error_state"] = error_state
            pod_context["deployment"] = deployment_name
            
            # Fetch deployment spec for Fixer Agent
            apps_api = k8s_client.AppsV1Api()
            deployment = await apps_api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            deployment_dict = custom_api.api_client.sanitize_for_serialization(deployment)
            deployment_spec = deployment_dict.get("spec", {})

            # ── Preprocess logs ───────────────────────────────────────────────
            cleaned_logs = preprocess_logs(raw_logs, error_state)
            fingerprint = make_fingerprint(cleaned_logs, error_state)

            # ── Layer 2: Fingerprint cache ─────────────────────────────────────
            with telemetry_tracer.start_as_current_span("sre.dedup.l2_fingerprint",
                    attributes={"fingerprint": fingerprint}) as _:
                is_dup, existing_pr = await check_fingerprint_cache(fingerprint)
            if is_dup:
                logger.info(
                    "[dedup-L2] %s/%s: duplicate fingerprint — incrementing seenCount on %s",
                    namespace, pod_name, existing_pr,
                )
                if telemetry.dedup_hits_counter:
                    telemetry.dedup_hits_counter.add(1, {"layer": "l2_fingerprint", "namespace": namespace})
                await increment_seen_count(existing_pr, namespace, custom_api)
                return

            # ── Layer 3: Active PatchRequest check ────────────────────────────
            with telemetry_tracer.start_as_current_span("sre.dedup.l3_pr_check",
                    attributes={"deployment": deployment_name}) as _:
                has_pr, existing_pr = await has_open_patchrequest(
                    namespace, deployment_name, error_state, custom_api
                )
            if has_pr:
                logger.info(
                    "[dedup-L3] %s/%s: active PR exists (%s) — incrementing seenCount",
                    namespace, pod_name, existing_pr,
                )
                if telemetry.dedup_hits_counter:
                    telemetry.dedup_hits_counter.add(1, {"layer": "l3_pr_check", "namespace": namespace})
                await increment_seen_count(existing_pr, namespace, custom_api)
                return

            # ── All 3 layers passed → call LLM ────────────────────────────────
            incident_id = _make_incident_id()
            logger.info(
                "[%s] New incident: %s/%s in state %s",
                incident_id, namespace, deployment_name, error_state,
            )
            pipeline_span.set_attribute("incident.id", incident_id)

            # Increment incident counter
            if telemetry.incidents_counter:
                telemetry.incidents_counter.add(1, {
                    "namespace": namespace,
                    "deployment": deployment_name,
                    "error_state": error_state,
                })

            diagnosis = await diagnose_incident(
                pod_context=pod_context,
                deployment_spec=deployment_spec,
                cleaned_logs=cleaned_logs,
                events=events_text,
                incident_id=incident_id,
            )
            diagnosis["error_state"] = error_state

            # ── Build Incident domain object ──────────────────────────────────
            incident = Incident(
                incident_id=incident_id,
                error_state=error_state,
                error_fingerprint=fingerprint,
                target_deployment=deployment_name,
                target_namespace=namespace,
            )
            incident.start_investigation(diagnosis)

            # ── Persist to K8s CRDs ───────────────────────────────────────────
            with telemetry_tracer.start_as_current_span("sre.crd.create_patchrequest") as _:
                pr_name = await _create_patch_request_crd(
                    namespace, deployment_name, incident_id, diagnosis, custom_api
                )
            await _create_incident_record_crd(incident, custom_api)

            # Increment patchrequest counter
            if telemetry.patchrequests_counter:
                telemetry.patchrequests_counter.add(1, {
                    "namespace": namespace,
                    "outcome": "created",
                })

            # ── Register fingerprint for future dedup ─────────────────────────
            await register_fingerprint(
                fingerprint, pr_name,
                likely_recurring=diagnosis.get("likely_recurring", False),
            )

            # ── Notify (stub — replace with real Slack client) ────────────────
            severity = diagnosis.get("severity", "unknown")
            root_cause = diagnosis.get("root_cause", "Unknown")
            logger.info(
                "🔴 [%s] %s — %s/%s\n"
                "   Root Cause: %s\n"
                "   Suggested Fix: %s\n"
                "   PatchRequest: kubectl get pr %s -n %s",
                severity.upper(), error_state, namespace, deployment_name,
                root_cause,
                diagnosis.get("suggested_fix", ""),
                pr_name, namespace,
            )

        except Exception as exc:
            pipeline_span.record_exception(exc)
            logger.exception(
                "Diagnosis pipeline failed for %s/%s: %s", namespace, pod_name, exc
            )
        finally:
            await v1.api_client.close()
            await custom_api.api_client.close()


# ── PatchRequest Executor Handler ─────────────────────────────────────────────

@kopf.on.field(
    "patchrequests",
    group=CRD_GROUP,
    field="status.approvalState",
)
async def on_patchrequest_approved(body, name, namespace, new, old, logger, **kwargs):
    """
    Watches for PatchRequest objects transitioning to approvalState=Approved.
    Applies the proposedPatch to the target Deployment.

    Security: this handler runs as sre-executor-sa which only has patch
    rights on Deployments/StatefulSets/ConfigMaps — not Secrets or RBAC.
    """
    if new != "Approved" or old == "Approved":
        return

    spec = body.get("spec", {})
    deployment_name = spec.get("targetDeployment")
    target_namespace = spec.get("targetNamespace", namespace)
    proposed_patch = spec.get("proposedPatch", {})
    approved_by = body.get("status", {}).get("approvedBy", "unknown")

    logger.info(
        "[%s] PatchRequest approved by %s — applying patch to %s/%s",
        name, approved_by, target_namespace, deployment_name,
    )

    if not deployment_name:
        logger.warning("[%s] No deployment name — skipping", name)
        return

    await _ensure_k8s_configured()
    apps_api = k8s_client.AppsV1Api()
    custom_api = k8s_client.CustomObjectsApi()

    try:
        # ── Validate patch kind is within allowed resources ────────────────
        patch_kind = proposed_patch.get("kind", "Deployment")
        if patch_kind not in ("Deployment", "StatefulSet", "ConfigMap"):
            logger.error(
                "[%s] Patch kind %r is not in the allowed whitelist — REJECTED",
                name, patch_kind,
            )
            await custom_api.patch_namespaced_custom_object_status(
                group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
                plural="patchrequests", name=name,
                body={"status": {"approvalState": "Rejected"}},
                _content_type="application/merge-patch+json",
            )
            return

        # ── Apply the patch to the Deployment (strategic merge patch) ─────
        spec_patch = proposed_patch.get("spec_patch", {})
        restart_annotation = {
            "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        }
        if spec_patch:
            patch_body = {
                "spec": {
                    "template": {
                        "metadata": {"annotations": restart_annotation},
                        "spec": {"containers": [spec_patch]},
                    }
                }
            }
        else:
            # No specific resource fix — force a rollout restart via annotation bump
            patch_body = {
                "spec": {
                    "template": {
                        "metadata": {"annotations": restart_annotation}
                    }
                }
            }

        await apps_api.patch_namespaced_deployment(
            name=deployment_name,
            namespace=target_namespace,
            body=patch_body,
        )
        logger.info("[%s] Deployment %s/%s patched successfully", name, target_namespace, deployment_name)

        # ── Update PatchRequest status → Applied + start observation ──────
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        await custom_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
            plural="patchrequests", name=name,
            body={"status": {
                "approvalState": "Applied",
                "appliedAt": now_iso,
                "observationStartTime": now_iso,
            }},
            _content_type="application/merge-patch+json",
        )
        logger.info(
            "✅ [%s] Applied to %s/%s by %s — outcome checker observation window started",
            name, target_namespace, deployment_name, approved_by,
        )

    except Exception as exc:
        logger.exception("[%s] Patch execution failed: %s", name, exc)
    finally:
        await apps_api.api_client.close()
        await custom_api.api_client.close()
