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
    clear_fingerprint,
    has_open_patchrequest,
    has_open_patchrequest_for_node,
    increment_seen_count,
    register_fingerprint,
    should_trigger,
)
from controller.incident import Incident
import controller.db as db
import controller.embeddings as embeddings
import controller.triage as triage
import controller.actions as actions
from controller.log_preprocessor import (
    detect_error_state,
    detect_init_error_state,
    detect_node_condition,
    make_fingerprint,
    preprocess_logs,
    strip_heartbeat,
)
from controller.llm_client import diagnose_incident, diagnose_node_incident, ValidatorAgent
import controller.telemetry as telemetry
import controller.webhook_client as webhook_client
import controller.outcome_checker  # noqa: F401 (Ensure kopf discovers the timer)

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
WATCH_NAMESPACES = os.getenv("WATCH_NAMESPACES", "production").split(",")
CRD_GROUP = "sre.yourdomain.io"
CRD_VERSION = "v1alpha1"

# ── Authentication Override ───────────────────────────────────────────────────

@kopf.on.login()
async def login_fn(**kwargs):
    """
    Override Kopf's default minimalistic login handler.
    Kopf's default handler hardcodes 'https://kubernetes.default.svc'.
    This custom handler uses the robust KUBERNETES_SERVICE_HOST env vars provided
    by the Kubelet, completely bypassing DNS resolution issues in Alpine/slim images.
    """
    token_path = '/var/run/secrets/kubernetes.io/serviceaccount/token'
    ns_path = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
    ca_path = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'

    if not os.path.exists(token_path):
        return None

    with open(token_path, encoding='utf-8') as f:
        token = f.read().strip()

    namespace = None
    if os.path.exists(ns_path):
        with open(ns_path, encoding='utf-8') as f:
            namespace = f.read().strip()

    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    server = f"https://{host}:{port}" if host else 'https://kubernetes.default.svc'

    return kopf.ConnectionInfo(
        server=server,
        ca_path=ca_path if os.path.exists(ca_path) else None,
        token=token or None,
        default_namespace=namespace or None,
    )
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
    target_node: str | None = None,
) -> str:
    """
    Create a PatchRequest CRD object in K8s and return its name.

    For domain=node incidents, `deployment_name` is the empty string and
    `target_node` carries the node name instead — there's no Deployment to
    key the PatchRequest name off, so the node name is used instead.
    """
    key = target_node or deployment_name
    pr_name = f"{key}-{incident_id.lower().replace('inc-', 'pr-')}"
    labels = {
        "incident-id": incident_id,
        "source": diagnosis.get("source", "ai_pipeline"),
    }
    if target_node:
        labels["target-node"] = target_node
    else:
        labels["target-deployment"] = deployment_name

    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "PatchRequest",
        "metadata": {
            "name": pr_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "incidentId": incident_id,
            "targetDeployment": deployment_name,
            "targetNamespace": namespace,
            "targetNode": target_node or "",
            "blastRadius": diagnosis.get("blast_radius", ""),
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
            "proposedActions": diagnosis.get("proposed_actions") or [],
            "domain": diagnosis.get("domain", "pod"),
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
    await db.init_db_pool()
    embeddings.init_embedding_model()


# @kopf.on.startup()
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


# ── Init Container Watch Handler ──────────────────────────────────────────────

@kopf.on.field("pods", field="status.initContainerStatuses")
async def on_pod_init_status_change(body, name, namespace, new, logger, **kwargs):
    """
    Fires whenever a pod's initContainerStatuses field changes.
    Handles Init:CrashLoopBackOff scenarios where the main container
    never starts (DB migration failures, wait-for-service timeouts, etc.).
    Routes through the same 3-layer dedup + LLM pipeline as the main handler.
    """
    if namespace not in WATCH_NAMESPACES:
        return

    error_state = detect_init_error_state(new or [])
    if not error_state:
        return  # Init containers healthy or not yet started

    deployment_name = _get_owner_deployment(body)
    if not deployment_name:
        return

    pod_uid = body["metadata"].get("uid", name)
    logger.info(
        "[init-handler] %s/%s → error_state=%s deployment=%s",
        namespace, name, error_state, deployment_name
    )

    # Layer 1: InitCrashLoopBackOff is in IMMEDIATE_TRIGGER_STATES—fires on first event
    if not await should_trigger(pod_uid, error_state):
        logger.info("[dedup-L1] %s/%s: init error not yet persistent, skipping", namespace, name)
        return

    logger.info("[dedup-L1] ✅ %s/%s: init container failure confirmed — queuing pipeline", namespace, name)
    await asyncio.create_task(
        _run_diagnosis_pipeline(
            pod_name=name,
            namespace=namespace,
            deployment_name=deployment_name,
            error_state=error_state,
            body=body,
            container_statuses=new or [],  # pass initContainerStatuses as context
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

            # ── Triage: sort this signal into a domain before diagnosing ───────
            # Phase 1 only wires the pod watcher through triage — node/cluster/app
            # watchers (and their domains) are added in later phases. This call
            # is free (rule-based, no LLM) for the "pod_status" source.
            domain, _resource_ref = triage.classify_domain(
                "pod_status", {"namespace": namespace, "deployment": deployment_name}
            )

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
                still_exists = await increment_seen_count(existing_pr, namespace, custom_api)
                if still_exists:
                    return
                # Cached fingerprint pointed at a PatchRequest that's gone (approved,
                # closed, or manually deleted) — purge the stale entry and fall
                # through so this recurrence is actually diagnosed instead of
                # silently dropped.
                await clear_fingerprint(fingerprint)

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
                still_exists = await increment_seen_count(existing_pr, namespace, custom_api)
                if still_exists:
                    return
                # Rare race: the PR was deleted between the has_open_patchrequest
                # check above and this increment — fall through and diagnose fresh.

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

            # Record MTTD: time from pod start to pipeline trigger
            try:
                pod_obj = await v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                pod_start = pod_obj.status.start_time
                if pod_start and telemetry.mttd_histogram:
                    mttd_seconds = (datetime.now(timezone.utc) - pod_start).total_seconds()
                    telemetry.mttd_histogram.record(max(0, mttd_seconds), {
                        "namespace": namespace,
                        "error_state": error_state,
                    })
            except Exception as mttd_exc:
                logger.debug("[%s] Could not record MTTD: %s", incident_id, mttd_exc)

            # ── RAG: embed cleaned logs so this incident can be reused later ───
            with telemetry_tracer.start_as_current_span("sre.rag.embed_logs") as _:
                try:
                    embedding_vec = await embeddings.embed_text(cleaned_logs)
                except Exception as embed_exc:
                    logger.warning("[%s] Embedding failed (non-fatal): %s", incident_id, embed_exc)
                    embedding_vec = None

            # ── RAG: reuse a past fix instead of calling the AI pipeline ────────
            diagnosis = None
            with telemetry_tracer.start_as_current_span("sre.rag.lookup") as rag_span:
                rag_match = await db.find_similar_incident(embedding_vec, error_state) if embedding_vec else None
                rag_span.set_attribute("rag.hit", bool(rag_match))
                if rag_match:
                    logger.info(
                        "[%s] 🧠 RAG match: %s (similarity=%.3f) — reusing its patch instead of calling the AI agents",
                        incident_id, rag_match["incident_id"], rag_match["similarity"],
                    )
                    is_valid, validation_err = await ValidatorAgent.validate(
                        namespace, deployment_name, rag_match["patch_applied"]
                    )
                    if is_valid:
                        diagnosis = {
                            "root_cause": rag_match["root_cause"],
                            "severity": rag_match["severity"],
                            "suggested_fix": f"Reused from {rag_match['incident_id']} "
                                             f"(semantic match, similarity={rag_match['similarity']:.2f})",
                            "auto_restart_safe": True,
                            "config_suggestions": [],
                            "likely_recurring": True,
                            "estimated_impact": "See original incident " + rag_match["incident_id"],
                            "matches_past_incident": rag_match["incident_id"],
                            "confidence_boost": "high",
                            "proposed_patch": rag_match["patch_applied"],
                            "proposed_actions": [{"type": "patch_deployment", "params": {"patch": rag_match["patch_applied"]}}],
                            "source": "rag_cache",
                        }
                        if telemetry.rag_hits_counter:
                            telemetry.rag_hits_counter.add(1, {"namespace": namespace, "error_state": error_state})
                    else:
                        logger.warning(
                            "[%s] RAG match found but its patch no longer validates (%s) — falling back to AI pipeline",
                            incident_id, validation_err,
                        )

            if diagnosis is None:
                diagnosis = await diagnose_incident(
                    pod_context=pod_context,
                    deployment_spec=deployment_spec,
                    cleaned_logs=cleaned_logs,
                    events=events_text,
                    incident_id=incident_id,
                )
                diagnosis["source"] = "ai_pipeline"
            diagnosis["error_state"] = error_state
            diagnosis["domain"] = domain.value

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
            await db.save_incident(incident, embedding=embedding_vec)

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

            # ── Notify via Webhook (Discord / webhook.site) ────────────────────
            severity = diagnosis.get("severity", "unknown")
            root_cause = diagnosis.get("root_cause", "Unknown")
            patch_preview = str(diagnosis.get("proposed_patch", {}).get("spec", "Annotation-only patch"))
            
            # Fire and forget async webhook alert
            import asyncio
            asyncio.create_task(webhook_client.send_incident_alert(
                pr_name=pr_name,
                deployment=deployment_name,
                error_state=error_state,
                severity=severity,
                root_cause=root_cause,
                patch_preview=patch_preview
            ))

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


# ── Node Watch Handler (Phase 2: Node domain) ─────────────────────────────────
# PatchRequests/IncidentRecords for node problems are filed under this
# namespace since a Node isn't itself namespaced — there's no natural
# namespace to use otherwise.
CLUSTER_INCIDENT_NAMESPACE = os.getenv("CLUSTER_INCIDENT_NAMESPACE", "monitoring")


@kopf.on.field("nodes", field="status.conditions")
async def on_node_condition_change(body, name, old, new, logger, **kwargs):
    """
    Watches every Node's status.conditions for problems (NotReady,
    DiskPressure, MemoryPressure, PIDPressure, NetworkUnavailable).

    Node conditions carry a lastHeartbeatTime that updates roughly every 40s
    from the kubelet even when nothing changed — comparing old/new after
    stripping that out (strip_heartbeat) avoids re-running diagnosis on every
    heartbeat for a node that's been unhealthy for hours.
    """
    if strip_heartbeat(old) == strip_heartbeat(new):
        return

    condition = detect_node_condition(new)
    if condition is None:
        return  # node is healthy (or recovered) — nothing to do

    await _run_node_diagnosis_pipeline(node_name=name, condition=condition, conditions=new)


async def _run_node_diagnosis_pipeline(node_name: str, condition: str, conditions: list) -> None:
    await _ensure_k8s_configured()
    core_api = k8s_client.CoreV1Api()
    custom_api = k8s_client.CustomObjectsApi()

    telemetry_tracer = telemetry.get_tracer()
    with telemetry_tracer.start_as_current_span(
        "sre.node_diagnosis.pipeline",
        attributes={"node": node_name, "condition": condition},
    ) as pipeline_span:
        try:
            domain, _resource_ref = triage.classify_domain("node_condition", {"node_name": node_name})

            # ── Layer 3 equivalent: skip if a PatchRequest is already open ─────
            has_pr, existing_pr = await has_open_patchrequest_for_node(
                CLUSTER_INCIDENT_NAMESPACE, node_name, condition, custom_api
            )
            if has_pr:
                logger.info("[dedup-node] %s: active PR exists (%s) — skipping", node_name, existing_pr)
                if telemetry.dedup_hits_counter:
                    telemetry.dedup_hits_counter.add(1, {"layer": "node_pr_check", "namespace": CLUSTER_INCIDENT_NAMESPACE})
                return

            # ── Gather node context: capacity, and blast radius ────────────────
            node_obj = await core_api.read_node(name=node_name)
            node_dict = custom_api.api_client.sanitize_for_serialization(node_obj)

            all_pods = await core_api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}")
            # DaemonSet pods are excluded from blast-radius / drain candidates —
            # kubectl drain skips them too, since they're meant to run on every
            # node and evicting them doesn't reduce load the way it does for a
            # normal Deployment-owned pod.
            pod_refs = []
            namespaces = set()
            for pod in all_pods.items:
                owners = pod.metadata.owner_references or []
                if any(o.kind == "DaemonSet" for o in owners):
                    continue
                pod_refs.append({"name": pod.metadata.name, "namespace": pod.metadata.namespace})
                namespaces.add(pod.metadata.namespace)

            node_context = {
                "node_name": node_name,
                "condition": condition,
                "conditions": conditions,
                "capacity": node_dict.get("status", {}).get("capacity", {}),
                "allocatable": node_dict.get("status", {}).get("allocatable", {}),
                "pod_count": len(pod_refs),
                "namespaces": sorted(namespaces),
                "pod_refs": pod_refs,
            }

            incident_id = _make_incident_id()
            logger.info("[%s] New node incident: %s in state %s", incident_id, node_name, condition)
            if telemetry.incidents_counter:
                telemetry.incidents_counter.add(1, {
                    "namespace": CLUSTER_INCIDENT_NAMESPACE, "deployment": node_name, "error_state": condition,
                })

            diagnosis = await diagnose_node_incident(node_context, incident_id)
            diagnosis["error_state"] = condition
            diagnosis["domain"] = domain.value
            diagnosis["source"] = "ai_pipeline"

            fingerprint = make_fingerprint(str(node_context.get("conditions")), condition)

            incident = Incident(
                incident_id=incident_id,
                error_state=condition,
                error_fingerprint=fingerprint,
                target_deployment="",
                target_namespace=CLUSTER_INCIDENT_NAMESPACE,
            )
            incident.start_investigation(diagnosis)

            pr_name = await _create_patch_request_crd(
                CLUSTER_INCIDENT_NAMESPACE, "", incident_id, diagnosis, custom_api, target_node=node_name,
            )
            await _create_incident_record_crd(incident, custom_api)
            await db.save_incident(incident)

            if telemetry.patchrequests_counter:
                telemetry.patchrequests_counter.add(1, {"namespace": CLUSTER_INCIDENT_NAMESPACE, "outcome": "created"})

            logger.info(
                "🔴 [%s] %s — node/%s\n"
                "   Root Cause: %s\n"
                "   Blast Radius: %s\n"
                "   PatchRequest: kubectl get pr %s -n %s",
                diagnosis.get("severity", "unknown").upper(), condition, node_name,
                diagnosis.get("root_cause", "Unknown"),
                diagnosis.get("blast_radius", "unknown"),
                pr_name, CLUSTER_INCIDENT_NAMESPACE,
            )

        except Exception as exc:
            pipeline_span.record_exception(exc)
            logger.exception("Node diagnosis pipeline failed for node %s: %s", node_name, exc)
        finally:
            await core_api.api_client.close()
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
    domain = spec.get("domain", "pod")

    if domain == "node":
        await _execute_node_actions(body, name, target_namespace, approved_by, logger)
        return

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
        # proposedPatch is a full deployment-spec fragment from the LLM, e.g.:
        #   {"spec": {"template": {"spec": {"containers": [{"name": "coredns", "args": [...]}]}}}}
        # We deep-merge a restart annotation into it so Kubernetes rolls the pods.
        restart_annotation = {
            "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        }

        # Build the base patch: always stamp the restart annotation
        patch_body: dict = {
            "spec": {
                "template": {
                    "metadata": {"annotations": restart_annotation}
                }
            }
        }

        if proposed_patch:
            # Merge the LLM's full spec fragment on top of the base annotation patch.
            # proposed_patch may be shaped like {"spec": {"template": {"spec": {...}}}}
            # OR like {"spec_patch": {...}} (legacy single-container shortcut).
            lp_spec = proposed_patch.get("spec", {})
            lp_tpl = lp_spec.get("template", {})
            lp_pod_spec = lp_tpl.get("spec", {})

            if lp_pod_spec:
                # Full nested structure — merge containers list in
                patch_body["spec"]["template"].update(
                    {k: v for k, v in lp_tpl.items() if k != "metadata"}
                )
                logger.info("[%s] Applying full LLM proposedPatch to %s/%s", name, target_namespace, deployment_name)
            elif proposed_patch.get("spec_patch"):
                # Legacy flat container spec (single-container shortcut)
                patch_body["spec"]["template"]["spec"] = {
                    "containers": [proposed_patch["spec_patch"]]
                }
                logger.info("[%s] Applying legacy spec_patch to %s/%s", name, target_namespace, deployment_name)
            else:
                logger.info("[%s] No actionable patch fields — forcing rollout restart on %s/%s", name, target_namespace, deployment_name)
        else:
            logger.info("[%s] No proposedPatch — forcing rollout restart on %s/%s", name, target_namespace, deployment_name)

        await apps_api.patch_namespaced_deployment(
            name=deployment_name,
            namespace=target_namespace,
            body=patch_body,
        )
        logger.info("[%s] Deployment %s/%s patched successfully", name, target_namespace, deployment_name)

        # ── Persist the applied patch so RAG can reuse it on future incidents ──
        incident_id = spec.get("incidentId")
        if incident_id and proposed_patch:
            await db.save_applied_patch(incident_id, proposed_patch, approved_by)

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


async def _execute_node_actions(body: dict, name: str, namespace: str, approved_by: str, logger) -> None:
    """
    Executes an approved node-domain action plan (cordon_node today;
    drain_node refuses to run for real — see actions.py). This is the
    action-plugin system's first real caller on the execute() side, not
    just the dry-run side Phase 1 proved out.

    Unlike the pod/deployment path, this does not hand off to
    outcome_checker's health-polling loop afterward — there's no
    Deployment to health-check, and "is a cordoned node okay now" isn't
    something that resolves on its own the way a pod restart does. The
    PatchRequest is marked Applied and left there for a human to close once
    they're satisfied (e.g. after replacing failed hardware).
    """
    spec = body.get("spec", {})
    target_node = spec.get("targetNode", "")
    action_list = spec.get("proposedActions", [])

    logger.info("[%s] PatchRequest approved by %s — executing node actions on %s", name, approved_by, target_node)

    if not target_node or not action_list:
        logger.warning("[%s] No target node or no actions — skipping", name)
        return

    await _ensure_k8s_configured()
    custom_api = k8s_client.CustomObjectsApi()

    try:
        context = {"node_name": target_node}
        results = await actions.execute_all(action_list, context)

        all_ok = all(r["ok"] for r in results)
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        await custom_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
            plural="patchrequests", name=name,
            body={"status": {
                "approvalState": "Applied" if all_ok else "Failed",
                "appliedAt": now_iso,
            }},
            _content_type="application/merge-patch+json",
        )
        logger.info("[%s] Node action results: %s", name, results)

    except Exception as exc:
        logger.exception("[%s] Node action execution failed: %s", name, exc)
    finally:
        await custom_api.api_client.close()
