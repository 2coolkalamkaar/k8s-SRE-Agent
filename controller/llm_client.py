"""
llm_client.py — Multi-Agent Remediation Pipeline.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
import httpx
from kubernetes_asyncio import client as k8s_client

import controller.actions as actions

try:
    from google import genai
    GENAI_SDK_AVAILABLE = True
except ImportError:
    GENAI_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)
import controller.telemetry as telemetry

# ── Environment Configuration ──────────────────────────────────────────────────
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "project-036ddc82-f451-4fae-9e3")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")
_vertex_client: genai.Client | None = None

def _get_vertex_client() -> genai.Client | None:
    global _vertex_client
    if _vertex_client is not None:
        return _vertex_client
    if not GENAI_SDK_AVAILABLE:
        return None
    try:
        adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/gcp/credentials.json")
        if os.path.exists(adc_path) and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path
        _vertex_client = genai.Client(vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION)
        return _vertex_client
    except Exception as exc:
        logger.warning("Failed to initialize Vertex AI client: %s", exc)
        return None

# ── Core LLM Generation Wrapper ────────────────────────────────────────────────
async def _generate_content(prompt: str, incident_id: str) -> str:
    """Routes the prompt to Vertex AI and returns raw text."""
    tracer = telemetry.get_tracer()
    start = time.time()
    
    with tracer.start_as_current_span(
        "sre.llm.generate",
        attributes={"provider": "vertex", "model": VERTEX_MODEL, "incident.id": incident_id},
    ) as llm_span:
        try:
            client = _get_vertex_client()
            if not client:
                logger.error("[%s] Vertex AI client could not be initialized.", incident_id)
                return ""
                
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: client.models.generate_content(model=VERTEX_MODEL, contents=prompt)
            )
            return response.text or ""

        except Exception as exc:
            llm_span.record_exception(exc)
            logger.error("[%s] Vertex AI generation failed: %s", incident_id, exc)
            return ""

def _parse_json(raw_text: str) -> dict:
    if not raw_text:
        return {}
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_text)
    cleaned = re.sub(r"\s*```", "", cleaned).strip()
    try: return json.loads(cleaned)
    except: pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try: return json.loads(match.group())
        except: pass
    return {}

# ── Multi-Agent Classes ────────────────────────────────────────────────────────

class AnalystAgent:
    @staticmethod
    async def analyze(pod_context: dict, cleaned_logs: str, events: str, past_incidents: list[dict], incident_id: str) -> dict:
        history_block = ""
        if past_incidents:
            lines = ["=== HISTORICAL CONTEXT ==="]
            for i, inc in enumerate(past_incidents, 1):
                res = inc.get("resolution", {})
                lines.append(f"Incident #{i}: {inc.get('errorState')} -> {res.get('resolutionNotes')}")
            history_block = "\n".join(lines)

        prompt = f"""You are an expert Kubernetes SRE Analyst. Diagnose the pod failure.
Respond in valid JSON format: {{"root_cause": "exact reason", "severity": "low|medium|high|critical", "likely_recurring": true, "estimated_impact": "impact"}}

=== METADATA ===
Deployment: {pod_context.get('deployment')} (Namespace: {pod_context.get('namespace')})
Pod: {pod_context.get('pod')}
Error State: {pod_context.get('error_state')}

=== PREPROCESSED LOGS ===
{cleaned_logs}

=== KUBERNETES EVENTS ===
{events}

{history_block}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)

class FixerAgent:
    @staticmethod
    async def propose_fix(rca: dict, deployment_spec: dict, validation_error: str | None, incident_id: str) -> dict:
        error_context = f"\n=== VALIDATION ERROR (Previous attempt failed) ===\n{validation_error}\nFix the patch to avoid this error." if validation_error else ""
        
        prompt = f"""You are an expert Kubernetes Infrastructure Engineer. 
Based on the Root Cause Analysis, propose a valid Kubernetes JSON Merge Patch to fix the deployment.
Respond in valid JSON format: {{"suggested_fix_description": "step-by-step human description", "auto_restart_safe": true, "patch": {{"spec": {{"template": ...}}}}}}
If no automated patch can be applied, set "patch" to {{}}. Note: Use strategic merge patch format for Deployments.
CRITICAL RULES FOR JSON PATCHES:
If modifying a container in a Deployment, you MUST include the container 'name' field so Kubernetes knows which container to patch. For example:
{{"spec": {{"template": {{"spec": {{"containers": [{{"name": "YOUR_CONTAINER_NAME", "env": [...]}}]}}}}}}}}

=== ROOT CAUSE ANALYSIS ===
{json.dumps(rca, indent=2)}

=== DEPLOYMENT SPEC (Current) ===
{json.dumps(deployment_spec, indent=2)}
{error_context}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)

class ValidatorAgent:
    @staticmethod
    async def validate(namespace: str, deployment_name: str, patch: dict) -> tuple[bool, str]:
        """Legacy single-patch validation path — still used by the RAG reuse
        flow in main.py, where a past incident's stored patch_applied is a
        raw patch dict, not an action list."""
        if not patch:
            return True, ""
        try:
            apps_api = k8s_client.AppsV1Api()
            await apps_api.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=patch,
                dry_run="All",
                _content_type="application/strategic-merge-patch+json"
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    @staticmethod
    async def validate_actions(
        namespace: str | None, deployment_name: str | None, action_list: list[dict],
        node_name: str | None = None,
    ) -> tuple[bool, str]:
        """New action-plan validation path — dry-runs every action in the
        plan via the pluggable action registry (controller/actions.py)."""
        context = {"namespace": namespace, "deployment_name": deployment_name, "node_name": node_name}
        return await actions.dry_run_all(action_list, context)

class NodeAnalystAgent:
    """Domain-specific Analyst for node problems. Separate from AnalystAgent
    (pod domain) because the questions are genuinely different: 'why is this
    node unhealthy' looks at capacity/conditions/blast-radius, not container
    logs — forcing one prompt to cover both makes it vague at everything."""

    @staticmethod
    async def analyze(node_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes SRE analyzing an unhealthy cluster Node
(not a pod — a physical/virtual machine that runs pods).
Respond in valid JSON format: {{"root_cause": "exact reason", "severity": "low|medium|high|critical", "likely_recurring": true, "estimated_impact": "impact"}}

=== NODE ===
Name: {node_context.get('node_name')}
Problem: {node_context.get('condition')}
Condition details: {json.dumps(node_context.get('conditions', []), indent=2)}
Capacity: {json.dumps(node_context.get('capacity', {}), indent=2)}
Allocatable: {json.dumps(node_context.get('allocatable', {}), indent=2)}

=== BLAST RADIUS ===
Pods currently scheduled on this node: {node_context.get('pod_count', 0)}
Namespaces affected: {', '.join(node_context.get('namespaces', [])) or 'none'}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


class NodeFixerAgent:
    """Proposes an action plan for a node problem. Unlike the pod-domain
    Fixer, this never produces a Deployment patch — the available moves are
    cordon (stop new scheduling) and drain (move existing pods off), which
    is why the output is action-plan-shaped from the start rather than
    wrapped into that shape after the fact."""

    @staticmethod
    async def propose_fix(rca: dict, node_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes Infrastructure Engineer deciding how to
respond to an unhealthy Node. You can ONLY choose from these two actions —
you cannot patch a Deployment to fix a node problem:
  - "cordon_node": stops new pods being scheduled on this node. Low risk —
    does not touch pods already running there.
  - "drain_node": moves existing pods off this node. Higher risk — can
    disrupt running workloads. Only recommend this for severe/persistent
    problems (e.g. DiskPressure, not a transient blip).

Respond in valid JSON: {{"suggested_fix_description": "plain-language description",
"actions": [{{"type": "cordon_node", "params": {{}}}}]}}
(the node_name parameter will be filled in automatically — do not include it)
If no action is warranted yet (e.g. problem looks transient), return {{"actions": []}}.

=== ROOT CAUSE ANALYSIS ===
{json.dumps(rca, indent=2)}

=== NODE CONTEXT ===
{json.dumps(node_context, indent=2)}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


async def diagnose_node_incident(node_context: dict, incident_id: str) -> dict:
    """
    Node-domain counterpart to diagnose_incident(). Kept as a separate
    function rather than branching inside diagnose_incident() — the two
    pipelines share almost no logic (different agents, different action
    types, no Fixer retry-on-validation-error loop since node actions are
    simple enough not to need it yet), so merging them would mostly be
    if/else scaffolding around two unrelated flows.
    """
    logger.info("[%s] Starting Node Remediation Pipeline (domain=node)", incident_id)

    try:
        rca = await NodeAnalystAgent.analyze(node_context, incident_id)
        logger.info("[%s] Node Analyst RCA: %s", incident_id, rca.get("root_cause", "N/A"))
    except Exception as exc:
        logger.error("[%s] Node Analyst Agent failed with exception: %s", incident_id, exc)
        rca = {}

    try:
        fix_result = await NodeFixerAgent.propose_fix(rca, node_context, incident_id)
    except Exception as exc:
        logger.error("[%s] Node Fixer Agent failed with exception: %s", incident_id, exc)
        fix_result = {}

    proposed_actions = fix_result.get("actions", [])
    # Fill in node_name server-side rather than trusting the LLM to echo it
    # back correctly — this is the one parameter that must never be wrong.
    for action in proposed_actions:
        action.setdefault("params", {})["node_name"] = node_context.get("node_name")
        if action.get("type") == "drain_node":
            action["params"]["pods"] = node_context.get("pod_refs", [])

    validated = False
    if proposed_actions:
        logger.info("[%s] Validator Agent dry-running node action plan", incident_id)
        is_valid, error_msg = await ValidatorAgent.validate_actions(
            None, None, proposed_actions, node_name=node_context.get("node_name")
        )
        if is_valid:
            logger.info("[%s] ✅ Validator Agent approved node action plan", incident_id)
            validated = True
        else:
            logger.warning("[%s] ❌ Validator Agent rejected node action plan: %s", incident_id, error_msg)

    return {
        "root_cause": rca.get("root_cause", "Analysis failed"),
        "severity": rca.get("severity", "high"),
        "suggested_fix": fix_result.get("suggested_fix_description", "Manual investigation required."),
        "auto_restart_safe": False,  # node actions are never treated as auto-safe
        "config_suggestions": [],
        "likely_recurring": rca.get("likely_recurring", False),
        "estimated_impact": rca.get("estimated_impact", "Unknown"),
        "matches_past_incident": None,
        "confidence_boost": "high" if validated else "none",
        "proposed_patch": {},
        "proposed_actions": proposed_actions if validated else [],
        "blast_radius": f"{node_context.get('pod_count', 0)} pod(s) across "
                         f"{len(node_context.get('namespaces', []))} namespace(s)",
    }


class ClusterAnalystAgent:
    """Domain-specific Analyst for cluster-scoped resource problems — right
    now, ResourceQuota pressure/exhaustion within a namespace. Kept separate
    from the pod/node Analysts for the same reason they're separate from
    each other: the questions here are about capacity accounting within a
    namespace, not container logs or node health."""

    @staticmethod
    async def analyze(quota_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes SRE analyzing a ResourceQuota problem
in a namespace (not a pod or node — a namespace-level resource limit).
Respond in valid JSON format: {{"root_cause": "exact reason", "severity": "low|medium|high|critical", "likely_recurring": true, "estimated_impact": "impact"}}

=== RESOURCE QUOTA ===
Namespace: {quota_context.get('namespace')}
Quota name: {quota_context.get('quota_name')}
Problem: {quota_context.get('condition')}
Used:  {json.dumps(quota_context.get('used', {}), indent=2)}
Hard limit: {json.dumps(quota_context.get('hard', {}), indent=2)}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


class ClusterFixerAgent:
    """Proposes an action plan for a ResourceQuota problem. The only
    available move today is raising the specific limit(s) that are
    constrained — there's no equivalent of 'cordon' or 'drain' for a quota,
    it's a much simpler decision than the node domain."""

    @staticmethod
    async def propose_fix(rca: dict, quota_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes Infrastructure Engineer deciding how to
respond to a ResourceQuota problem. You can ONLY propose raising the specific
hard limit(s) that are constrained — do not propose lowering usage or
touching any Deployment.

Respond in valid JSON: {{"suggested_fix_description": "plain-language description",
"actions": [{{"type": "patch_resourcequota", "params": {{"patch": {{"spec": {{"hard": {{"<resource-key>": "<new-higher-value>"}}}}}}}}}}]}}
(namespace and quota_name will be filled in automatically — do not include them)
Only raise the specific resource key(s) that are actually near/at their limit —
leave every other key in the quota untouched. If you cannot determine a safe
new value, return {{"actions": []}}.

=== ROOT CAUSE ANALYSIS ===
{json.dumps(rca, indent=2)}

=== RESOURCE QUOTA CONTEXT ===
{json.dumps(quota_context, indent=2)}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


async def diagnose_cluster_incident(quota_context: dict, incident_id: str) -> dict:
    """Cluster-domain counterpart to diagnose_incident()/diagnose_node_incident().
    Same shape as the node pipeline (no Fixer retry loop — a quota patch is
    simple enough not to need one yet), kept as its own function for the
    same reason: the domains share almost no logic."""
    logger.info("[%s] Starting Cluster Remediation Pipeline (domain=cluster)", incident_id)

    try:
        rca = await ClusterAnalystAgent.analyze(quota_context, incident_id)
        logger.info("[%s] Cluster Analyst RCA: %s", incident_id, rca.get("root_cause", "N/A"))
    except Exception as exc:
        logger.error("[%s] Cluster Analyst Agent failed with exception: %s", incident_id, exc)
        rca = {}

    try:
        fix_result = await ClusterFixerAgent.propose_fix(rca, quota_context, incident_id)
    except Exception as exc:
        logger.error("[%s] Cluster Fixer Agent failed with exception: %s", incident_id, exc)
        fix_result = {}

    proposed_actions = fix_result.get("actions", [])
    for action in proposed_actions:
        params = action.setdefault("params", {})
        params["namespace"] = quota_context.get("namespace")
        params["quota_name"] = quota_context.get("quota_name")

    validated = False
    if proposed_actions:
        logger.info("[%s] Validator Agent dry-running cluster action plan", incident_id)
        is_valid, error_msg = await ValidatorAgent.validate_actions(
            quota_context.get("namespace"), None, proposed_actions
        )
        if is_valid:
            logger.info("[%s] ✅ Validator Agent approved cluster action plan", incident_id)
            validated = True
        else:
            logger.warning("[%s] ❌ Validator Agent rejected cluster action plan: %s", incident_id, error_msg)

    return {
        "root_cause": rca.get("root_cause", "Analysis failed"),
        "severity": rca.get("severity", "high"),
        "suggested_fix": fix_result.get("suggested_fix_description", "Manual investigation required."),
        "auto_restart_safe": False,
        "config_suggestions": [],
        "likely_recurring": rca.get("likely_recurring", False),
        "estimated_impact": rca.get("estimated_impact", "Unknown"),
        "matches_past_incident": None,
        "confidence_boost": "high" if validated else "none",
        "proposed_patch": {},
        "proposed_actions": proposed_actions if validated else [],
        "blast_radius": f"1 namespace ({quota_context.get('namespace')}) — future scheduling only, "
                         f"no running workloads affected",
    }


class AppAnalystAgent:
    """Domain-specific Analyst for behavioral app problems — a deployment
    that's technically running (no crashing pods, nothing the pod-domain
    watcher would ever notice) but degraded: elevated error rate, high
    latency, whatever a Prometheus alert caught. There are no crash logs to
    read here — the only signal is the alert itself, which is why this
    can't reuse the pod-domain Analyst's prompt."""

    @staticmethod
    async def analyze(alert_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes SRE analyzing a Prometheus alert about an
application that is running (not crashing) but behaving badly — e.g. high
error rate or high latency. There are no crash logs; reason from the alert
itself and the deployment's current state.
Respond in valid JSON format: {{"root_cause": "best-guess reason based on the alert", "severity": "low|medium|high|critical", "likely_recurring": true, "estimated_impact": "impact"}}

=== ALERT ===
Name: {alert_context.get('alert_name')}
Namespace: {alert_context.get('namespace')}
Deployment: {alert_context.get('deployment')}
Description: {alert_context.get('description')}
Labels: {json.dumps(alert_context.get('labels', {}), indent=2)}

=== DEPLOYMENT STATE ===
Current replicas: {alert_context.get('replicas')}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


class AppFixerAgent:
    """Proposes an action plan for a behavioral app problem. Deliberately
    limited to two blunt, well-understood instruments — restart the pods,
    or scale the deployment — rather than anything that requires guessing
    at application code. A wrong guess about *why* an app is slow is much
    more likely than a wrong guess about whether restarting or scaling it
    is a reasonable thing to try."""

    @staticmethod
    async def propose_fix(rca: dict, alert_context: dict, incident_id: str) -> dict:
        prompt = f"""You are an expert Kubernetes Infrastructure Engineer deciding how to
respond to a degraded (not crashing) application. You can ONLY choose from:
  - "rollout_restart": restarts all pods in the deployment (rolling, no downtime).
    Good for transient degradation, memory creep, stuck connections.
  - "scale_deployment": increases replica count. Good if the root cause looks
    like the deployment is simply under-provisioned for its current load.

Respond in valid JSON: {{"suggested_fix_description": "plain-language description",
"actions": [{{"type": "rollout_restart", "params": {{}}}}]}}
For scale_deployment, params must include a "replicas" integer.
If neither action is clearly warranted, return {{"actions": []}}.

=== ROOT CAUSE ANALYSIS ===
{json.dumps(rca, indent=2)}

=== ALERT CONTEXT ===
{json.dumps(alert_context, indent=2)}
"""
        raw = await _generate_content(prompt, incident_id)
        return _parse_json(raw)


async def diagnose_app_incident(alert_context: dict, incident_id: str) -> dict:
    """App-domain counterpart to the other diagnose_*_incident functions.
    Unlike node/cluster, this domain's actions target a real Deployment in
    a real namespace — so unlike those two, this one CAN plug into the
    existing outcome_checker health-observation loop for free (the caller
    in main.py sets observationStartTime when applying, same as the
    original pod/deployment path)."""
    logger.info("[%s] Starting App Remediation Pipeline (domain=app)", incident_id)

    try:
        rca = await AppAnalystAgent.analyze(alert_context, incident_id)
        logger.info("[%s] App Analyst RCA: %s", incident_id, rca.get("root_cause", "N/A"))
    except Exception as exc:
        logger.error("[%s] App Analyst Agent failed with exception: %s", incident_id, exc)
        rca = {}

    try:
        fix_result = await AppFixerAgent.propose_fix(rca, alert_context, incident_id)
    except Exception as exc:
        logger.error("[%s] App Fixer Agent failed with exception: %s", incident_id, exc)
        fix_result = {}

    proposed_actions = fix_result.get("actions", [])
    for action in proposed_actions:
        params = action.setdefault("params", {})
        params["namespace"] = alert_context.get("namespace")
        params["deployment_name"] = alert_context.get("deployment")

    validated = False
    if proposed_actions:
        logger.info("[%s] Validator Agent dry-running app action plan", incident_id)
        is_valid, error_msg = await ValidatorAgent.validate_actions(
            alert_context.get("namespace"), alert_context.get("deployment"), proposed_actions
        )
        if is_valid:
            logger.info("[%s] ✅ Validator Agent approved app action plan", incident_id)
            validated = True
        else:
            logger.warning("[%s] ❌ Validator Agent rejected app action plan: %s", incident_id, error_msg)

    return {
        "root_cause": rca.get("root_cause", "Analysis failed"),
        "severity": rca.get("severity", "high"),
        "suggested_fix": fix_result.get("suggested_fix_description", "Manual investigation required."),
        "auto_restart_safe": True,
        "config_suggestions": [],
        "likely_recurring": rca.get("likely_recurring", False),
        "estimated_impact": rca.get("estimated_impact", "Unknown"),
        "matches_past_incident": None,
        "confidence_boost": "high" if validated else "none",
        "proposed_patch": {},
        "proposed_actions": proposed_actions if validated else [],
        "blast_radius": f"{alert_context.get('replicas', '?')} replica(s) in "
                         f"{alert_context.get('namespace')}/{alert_context.get('deployment')}",
    }


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def diagnose_incident(
    pod_context: dict,
    deployment_spec: dict,
    cleaned_logs: str,
    events: str,
    incident_id: str,
    past_incidents: list[dict] | None = None,
) -> dict:
    logger.info("[%s] Starting Multi-Agent Remediation Pipeline", incident_id)
    
    # 1. Analyst Agent
    try:
        rca = await AnalystAgent.analyze(pod_context, cleaned_logs, events, past_incidents or [], incident_id)
        logger.info("[%s] Analyst RCA: %s", incident_id, rca.get("root_cause", "N/A"))
    except Exception as exc:
        logger.error("[%s] Analyst Agent failed with exception: %s", incident_id, exc)
        rca = {}

    # 2. Fixer & Validator Loop
    patch_result = {}
    validation_error = None
    max_retries = 3
    
    for attempt in range(max_retries):
        logger.info("[%s] Fixer Agent attempt %d/%d", incident_id, attempt + 1, max_retries)
        try:
            patch_result = await FixerAgent.propose_fix(rca, deployment_spec, validation_error, incident_id)
        except Exception as exc:
            logger.error("[%s] Fixer Agent failed with exception: %s", incident_id, exc)
            break
        
        patch = patch_result.get("patch", {})
        if not patch:
            logger.info("[%s] Fixer Agent determined no patch applies", incident_id)
            break

        # Wrap the Fixer's raw patch into the pluggable action-plan format.
        # patch_deployment is the only action type Phase 1 implements — this
        # is purely a shape change, the actual patch content the Fixer
        # produced is untouched, so existing pod/deployment behavior does
        # not change.
        action_list = [{"type": "patch_deployment", "params": {"patch": patch}}]

        logger.info("[%s] Validator Agent dry-running patch", incident_id)
        is_valid, error_msg = await ValidatorAgent.validate_actions(
            pod_context.get("namespace"), pod_context.get("deployment"), action_list
        )

        if is_valid:
            logger.info("[%s] ✅ Validator Agent approved patch", incident_id)
            patch_result["validation_passed"] = True
            break
        else:
            logger.warning("[%s] ❌ Validator Agent rejected patch: %s", incident_id, error_msg)
            validation_error = error_msg

    validated_patch = patch_result.get("patch", {}) if patch_result.get("validation_passed") else {}

    # Format output for PR creation.
    # proposed_patch is kept exactly as before (the executor and RAG reuse
    # path still read this field — no behavior change for pod/deployment
    # incidents). proposed_actions is the same fix expressed in the new
    # pluggable shape, ready for node/cluster/app action types to sit
    # alongside patch_deployment once those phases are built.
    return {
        "root_cause": rca.get("root_cause", "Analysis failed"),
        "severity": rca.get("severity", "high"),
        "suggested_fix": patch_result.get("suggested_fix_description", "Manual investigation required."),
        "auto_restart_safe": patch_result.get("auto_restart_safe", False),
        "config_suggestions": [],
        "likely_recurring": rca.get("likely_recurring", False),
        "estimated_impact": rca.get("estimated_impact", "Unknown"),
        "matches_past_incident": None,
        "confidence_boost": "high" if patch_result.get("validation_passed") else "none",
        "proposed_patch": validated_patch,
        "proposed_actions": [{"type": "patch_deployment", "params": {"patch": validated_patch}}] if validated_patch else [],
    }
