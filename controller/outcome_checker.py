"""
outcome_checker.py - Outcome checker for PatchRequests.
Runs a background timer to check deployment health after a patch is applied.
"""

from __future__ import annotations
import logging
import os
import asyncio
from datetime import datetime, timezone

import kopf
from kubernetes_asyncio import client as k8s_client, config as k8s_config

import controller.telemetry as telemetry
import controller.db as db
from controller.dedup import clear_fingerprint

logger = logging.getLogger(__name__)

CRD_GROUP = "sre.yourdomain.io"
CRD_VERSION = "v1alpha1"

OBSERVATION_WINDOW_SECONDS = int(os.getenv("OUTCOME_OBSERVATION_WINDOW", "600"))

_K8S_CONFIGURED = False
async def _ensure_k8s_configured() -> None:
    global _K8S_CONFIGURED
    if not _K8S_CONFIGURED:
        k8s_config.load_incluster_config()
        _K8S_CONFIGURED = True

async def _check_deployment_health(deployment_name: str, namespace: str) -> tuple[bool, str]:
    """Check if the deployment is healthy (replicas ready and no new pod crashes)."""
    await _ensure_k8s_configured()
    apps_api = k8s_client.AppsV1Api()
    try:
        deployment = await apps_api.read_namespaced_deployment(deployment_name, namespace)
            
        v1 = k8s_client.CoreV1Api()
        pods = await v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={deployment_name}"
        )
        
        for pod in pods.items:
            for cs in (pod.status.container_statuses or []):
                state = cs.state
                if state.waiting and state.waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"):
                    return False, f"Pod {pod.metadata.name} is in {state.waiting.reason}"
                if state.terminated and state.terminated.reason == "Error":
                    return False, f"Pod {pod.metadata.name} terminated with Error"
                
        # If all replicas are available, it's healthy.
        if deployment.status.available_replicas == deployment.spec.replicas:
            return True, ""
            
        # Still rolling out, consider it neither success nor failure yet.
        return True, ""
    except Exception as exc:
        logger.error("[outcome] Error checking deployment health: %s", exc)
        return True, ""
    finally:
        await apps_api.api_client.close()


async def _execute_rollback(deployment_name: str, target_namespace: str, pr_name: str, namespace: str, crash_reason: str, fingerprint: str, incident_id: str | None = None) -> None:
    await _ensure_k8s_configured()
    apps_api = k8s_client.AppsV1Api()
    custom_api = k8s_client.CustomObjectsApi()
    
    try:
        logger.warning("[outcome] 🔴 Executing rollback for %s in %s via rollout undo", deployment_name, target_namespace)
        
        # Best effort rollout undo using kubectl
        proc = await asyncio.create_subprocess_shell(
            f"kubectl rollout undo deployment/{deployment_name} -n {target_namespace}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        
        # Clear fingerprint cache so new crash generates new PR
        if fingerprint:
            await clear_fingerprint(fingerprint)
            
        await custom_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
            plural="patchrequests", name=pr_name,
            body={"status": {
                "approvalState": "Failed",
                "workedOutcome": False,
            }},
            _content_type="application/merge-patch+json",
        )
        
        if telemetry.outcome_counter:
            telemetry.outcome_counter.add(1, {"outcome": "rollback"})
        if incident_id:
            await db.mark_incident_outcome(incident_id, worked=False)

    except Exception as exc:
        logger.error("[outcome] Rollback failed for %s: %s", pr_name, exc)
    finally:
        await apps_api.api_client.close()
        await custom_api.api_client.close()

async def _close_incident(body: dict, pr_name: str, namespace: str, elapsed: float) -> None:
    await _ensure_k8s_configured()
    custom_api = k8s_client.CustomObjectsApi()
    
    incident_id = body.get("spec", {}).get("incidentId")
    
    try:
        await custom_api.patch_namespaced_custom_object_status(
            group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
            plural="patchrequests", name=pr_name,
            body={"status": {
                "approvalState": "Closed",
                "workedOutcome": True,
                "mttrSeconds": int(elapsed)
            }},
            _content_type="application/merge-patch+json",
        )
        
        try:
            ir_name = incident_id.lower()
            await custom_api.patch_cluster_custom_object(
                group=CRD_GROUP, version=CRD_VERSION, plural="incidentrecords",
                name=ir_name,
                body={"spec": {
                    "state": "Closed",
                }}
            )
        except Exception as exc:
            logger.warning("[outcome] Failed to patch IncidentRecord %s: %s", ir_name, exc)
            
        if telemetry.outcome_counter:
            telemetry.outcome_counter.add(1, {"outcome": "success"})
        if telemetry.mttr_histogram:
            telemetry.mttr_histogram.record(elapsed)
        if incident_id:
            await db.mark_incident_outcome(incident_id, worked=True, mttr_seconds=int(elapsed))
            
    except Exception as exc:
        logger.error("[outcome] Failed to close incident %s: %s", pr_name, exc)
    finally:
        await custom_api.api_client.close()


@kopf.timer("patchrequests", group=CRD_GROUP, interval=30.0, initial_delay=30.0)
async def outcome_checker_timer(body, name, namespace, logger, **kwargs):
    """
    Runs every 30s on every PatchRequest. Skips anything not in 'Applied' state.
    Checks deployment health and transitions to Closed or triggers rollback.
    """
    approval_state = body.get("status", {}).get("approvalState")
    if approval_state != "Applied":
        return

    spec = body.get("spec", {})

    # Node-domain PatchRequests have no Deployment to health-check, and a
    # cordoned node doesn't "resolve" on its own the way a pod restart does
    # — it stays Applied until a human closes it once satisfied (e.g. after
    # replacing hardware). Auto-observation for this domain is deferred.
    if spec.get("domain") == "node":
        return

    deployment_name = spec.get("targetDeployment")
    target_namespace = spec.get("targetNamespace", namespace)
    observation_start = body.get("status", {}).get("observationStartTime")
    llm_diagnosis = spec.get("llmDiagnosis", {})
    fingerprint = llm_diagnosis.get("error_fingerprint")

    if not observation_start:
        # Fallback: use appliedAt if observationStartTime was lost (status subresource merge-patch race)
        observation_start = body.get("status", {}).get("appliedAt")
        if observation_start:
            logger.info("[outcome] %s: observationStartTime missing — using appliedAt as fallback", name)
            # Re-stamp so future ticks use the dedicated field
            try:
                await _ensure_k8s_configured()
                _fix_api = k8s_client.CustomObjectsApi()
                await _fix_api.patch_namespaced_custom_object_status(
                    group=CRD_GROUP, version=CRD_VERSION, namespace=namespace,
                    plural="patchrequests", name=name,
                    body={"status": {"observationStartTime": observation_start}},
                    _content_type="application/merge-patch+json",
                )
                await _fix_api.api_client.close()
            except Exception as fix_exc:
                logger.warning("[outcome] Could not re-stamp observationStartTime: %s", fix_exc)
        else:
            logger.warning("[outcome] %s: Applied but no observationStartTime or appliedAt — skipping", name)
            return

    obs_dt = datetime.fromisoformat(observation_start).replace(tzinfo=None)
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - obs_dt).total_seconds()

    is_healthy, crash_reason = await _check_deployment_health(deployment_name, target_namespace)

    if not is_healthy:
        logger.warning("[outcome] 🔴 %s: pod crashed again after patch — triggering rollback! Reason: %s", name, crash_reason)
        incident_id = spec.get("incidentId")
        await _execute_rollback(deployment_name, target_namespace, name, namespace, crash_reason, fingerprint, incident_id)
        return

    if elapsed >= OBSERVATION_WINDOW_SECONDS:
        logger.info("[outcome] ✅ %s: deployment healthy for %ds — marking CLOSED", name, elapsed)
        await _close_incident(body, name, namespace, elapsed)
