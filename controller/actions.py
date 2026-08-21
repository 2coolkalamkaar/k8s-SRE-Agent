"""
actions.py — Typed, pluggable remediation actions.

Why this exists (in plain words):
    Before this file, the only thing the agent could ever do to fix something
    was "patch a Deployment's spec." That's fine for pod crashes, but a node
    running out of disk isn't fixed by a Deployment patch — it's fixed by
    cordoning the node. A full ResourceQuota isn't a Deployment patch either.

    So instead of one hardcoded "apply this patch" step, the Fixer agent now
    returns a LIST of actions, each with a `type` (e.g. "patch_deployment",
    "cordon_node") and whatever parameters that action type needs. This file
    is the lookup table: given an action's `type`, find the code that knows
    how to (a) safely dry-run it and (b) actually execute it.

    Adding a new kind of fix later (e.g. "restart_daemonset") means adding one
    new class here — nothing in the Validator or Executor needs to change.
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from kubernetes_asyncio import client as k8s_client

logger = logging.getLogger(__name__)


class BaseAction(ABC):
    """
    Every action type implements the same two operations:

    dry_run  — "would this work, without actually doing it?" Used by the
               Validator agent before a PatchRequest is ever created, and
               again when RAG proposes reusing an old fix.
    execute  — "actually do it." Only ever called after a human (or, for
               low-risk domains, the auto-approve policy) has approved the
               PatchRequest.

    Both return (ok: bool, message: str) so the caller can log/store why
    something failed without needing to catch exceptions everywhere.
    """

    type_name: str  # must match the "type" field the Fixer LLM outputs

    @abstractmethod
    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]: ...

    @abstractmethod
    async def execute(self, params: dict, context: dict) -> tuple[bool, str]: ...


class PatchDeploymentAction(BaseAction):
    """
    The action type that already exists today, ported unchanged: apply a
    strategic merge patch to a Deployment's pod template. This is the only
    action type Phase 1 implements — node/cluster action types (cordon_node,
    drain_node, patch_resourcequota, ...) are added in later phases once
    each one has been designed and tested against a live cluster.
    """

    type_name = "patch_deployment"

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = context.get("namespace")
        deployment_name = context.get("deployment_name")
        patch = params.get("patch", {})
        if not patch:
            return True, ""
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=patch,
                dry_run="All",
                _content_type="application/strategic-merge-patch+json",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = context.get("namespace")
        deployment_name = context.get("deployment_name")
        patch = params.get("patch", {})

        restart_annotation = {
            "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        }
        patch_body: dict = {"spec": {"template": {"metadata": {"annotations": restart_annotation}}}}

        if patch:
            lp_spec = patch.get("spec", {})
            lp_tpl = lp_spec.get("template", {})
            lp_pod_spec = lp_tpl.get("spec", {})
            if lp_pod_spec:
                patch_body["spec"]["template"].update(
                    {k: v for k, v in lp_tpl.items() if k != "metadata"}
                )
            elif patch.get("spec_patch"):
                patch_body["spec"]["template"]["spec"] = {"containers": [patch["spec_patch"]]}

        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body=patch_body,
            )
            return True, f"Patched {namespace}/{deployment_name}"
        except Exception as exc:
            return False, str(exc)


class CordonNodeAction(BaseAction):
    """
    Marks a Node unschedulable (spec.unschedulable = true) — stops new pods
    from being scheduled there, but does NOT touch pods already running on
    it. This is the low-risk half of "take a bad node out of rotation";
    DrainNodeAction below is the higher-risk half (moving existing pods off).
    """

    type_name = "cordon_node"

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        node_name = params.get("node_name") or context.get("node_name")
        if not node_name:
            return False, "cordon_node requires a node_name"
        try:
            core_api = k8s_client.CoreV1Api()
            await core_api.patch_node(
                name=node_name,
                body={"spec": {"unschedulable": True}},
                dry_run="All",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        node_name = params.get("node_name") or context.get("node_name")
        if not node_name:
            return False, "cordon_node requires a node_name"
        try:
            core_api = k8s_client.CoreV1Api()
            await core_api.patch_node(
                name=node_name,
                body={"spec": {"unschedulable": True}},
            )
            return True, f"Cordoned node {node_name}"
        except Exception as exc:
            return False, str(exc)


class DrainNodeAction(BaseAction):
    """
    Evicts non-DaemonSet pods off a node so it can be safely taken out of
    service. This is the highest-blast-radius action in the whole system —
    a wrong drain can disrupt many workloads at once, and unlike a
    Deployment patch there's no automatic rollback for "oops, drained the
    wrong node."

    Per an explicit decision: execute() is disabled (dry-run only) until
    this action type has been verified thoroughly on a real cluster and a
    human has deliberately turned it on. dry_run() is still fully real —
    it calls the Eviction API with dry_run=All, which respects
    PodDisruptionBudgets server-side, so it reports an honest answer to
    "would this actually work," it just never performs the eviction.
    """

    type_name = "drain_node"
    EXECUTION_ENABLED = False  # flip only after live verification + sign-off

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        node_name = params.get("node_name") or context.get("node_name")
        pod_refs = params.get("pods", [])  # [{"name": ..., "namespace": ...}, ...]
        if not node_name:
            return False, "drain_node requires a node_name"
        if not pod_refs:
            return True, "No evictable pods on this node (nothing to drain)"

        core_api = k8s_client.CoreV1Api()
        failures = []
        for pod in pod_refs:
            eviction = k8s_client.V1Eviction(
                metadata=k8s_client.V1ObjectMeta(name=pod["name"], namespace=pod["namespace"]),
            )
            try:
                await core_api.create_namespaced_pod_eviction(
                    name=pod["name"], namespace=pod["namespace"],
                    body=eviction, dry_run="All",
                )
            except Exception as exc:
                failures.append(f"{pod['namespace']}/{pod['name']}: {exc}")

        if failures:
            return False, f"{len(failures)}/{len(pod_refs)} pods would fail eviction: " + "; ".join(failures[:3])
        return True, f"All {len(pod_refs)} pod(s) on {node_name} would evict cleanly"

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        if not self.EXECUTION_ENABLED:
            logger.warning(
                "[actions] drain_node execution was requested but is disabled — "
                "dry-run only until this action type is verified and explicitly enabled"
            )
            return False, "drain_node execution is disabled (dry-run only) — no pods were evicted"
        # Real eviction loop intentionally not implemented yet — see
        # docstring. Implement here once EXECUTION_ENABLED is flipped.
        return False, "drain_node execute() not implemented"


class PatchResourceQuotaAction(BaseAction):
    """
    Raises one or more resource limits on a namespace's ResourceQuota. Lower
    blast-radius than node actions — it only affects future scheduling
    decisions within one namespace, doesn't touch anything already running.
    """

    type_name = "patch_resourcequota"

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        quota_name = params.get("quota_name")
        patch = params.get("patch", {})
        if not namespace or not quota_name or not patch:
            return False, "patch_resourcequota requires namespace, quota_name, and patch"
        try:
            core_api = k8s_client.CoreV1Api()
            await core_api.patch_namespaced_resource_quota(
                name=quota_name, namespace=namespace, body=patch, dry_run="All",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        quota_name = params.get("quota_name")
        patch = params.get("patch", {})
        if not namespace or not quota_name or not patch:
            return False, "patch_resourcequota requires namespace, quota_name, and patch"
        try:
            core_api = k8s_client.CoreV1Api()
            await core_api.patch_namespaced_resource_quota(
                name=quota_name, namespace=namespace, body=patch,
            )
            return True, f"Patched ResourceQuota {namespace}/{quota_name}"
        except Exception as exc:
            return False, str(exc)


class RolloutRestartAction(BaseAction):
    """Restarts every pod in a Deployment (rolling, no downtime) by
    stamping a restart annotation — same mechanism PatchDeploymentAction
    already uses for its base patch, just without any actual spec change
    riding along with it."""

    type_name = "rollout_restart"

    def _restart_patch(self) -> dict:
        return {
            "spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            }}}}
        }

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        deployment_name = params.get("deployment_name") or context.get("deployment_name")
        if not namespace or not deployment_name:
            return False, "rollout_restart requires namespace and deployment_name"
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body=self._restart_patch(), dry_run="All",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        deployment_name = params.get("deployment_name") or context.get("deployment_name")
        if not namespace or not deployment_name:
            return False, "rollout_restart requires namespace and deployment_name"
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body=self._restart_patch(),
            )
            return True, f"Rolled out restart for {namespace}/{deployment_name}"
        except Exception as exc:
            return False, str(exc)


class ScaleDeploymentAction(BaseAction):
    """Changes a Deployment's replica count."""

    type_name = "scale_deployment"

    async def dry_run(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        deployment_name = params.get("deployment_name") or context.get("deployment_name")
        replicas = params.get("replicas")
        if not namespace or not deployment_name or replicas is None:
            return False, "scale_deployment requires namespace, deployment_name, and replicas"
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace,
                body={"spec": {"replicas": replicas}}, dry_run="All",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def execute(self, params: dict, context: dict) -> tuple[bool, str]:
        namespace = params.get("namespace") or context.get("namespace")
        deployment_name = params.get("deployment_name") or context.get("deployment_name")
        replicas = params.get("replicas")
        if not namespace or not deployment_name or replicas is None:
            return False, "scale_deployment requires namespace, deployment_name, and replicas"
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body={"spec": {"replicas": replicas}},
            )
            return True, f"Scaled {namespace}/{deployment_name} to {replicas} replicas"
        except Exception as exc:
            return False, str(exc)


# ── Registry: type_name -> action instance ─────────────────────────────────────
_REGISTRY: dict[str, BaseAction] = {
    PatchDeploymentAction.type_name: PatchDeploymentAction(),
    CordonNodeAction.type_name: CordonNodeAction(),
    DrainNodeAction.type_name: DrainNodeAction(),
    PatchResourceQuotaAction.type_name: PatchResourceQuotaAction(),
    RolloutRestartAction.type_name: RolloutRestartAction(),
    ScaleDeploymentAction.type_name: ScaleDeploymentAction(),
}


def get_action(type_name: str) -> BaseAction | None:
    return _REGISTRY.get(type_name)


async def dry_run_all(actions: list[dict], context: dict) -> tuple[bool, str]:
    """Dry-run every action in a plan; stop at the first failure."""
    for action in actions:
        handler = get_action(action.get("type", ""))
        if handler is None:
            return False, f"Unknown action type: {action.get('type')!r}"
        ok, msg = await handler.dry_run(action.get("params", {}), context)
        if not ok:
            return False, f"{action.get('type')} failed dry-run: {msg}"
    return True, ""


async def execute_all(actions: list[dict], context: dict) -> list[dict]:
    """Execute every action in a plan, best-effort. Returns a result per action."""
    results = []
    for action in actions:
        handler = get_action(action.get("type", ""))
        if handler is None:
            results.append({"type": action.get("type"), "ok": False, "message": "unknown action type"})
            continue
        ok, msg = await handler.execute(action.get("params", {}), context)
        results.append({"type": action.get("type"), "ok": ok, "message": msg})
        logger.info("[actions] %s -> ok=%s msg=%s", action.get("type"), ok, msg)
    return results
