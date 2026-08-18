import os
import httpx
import logging

logger = logging.getLogger(__name__)

# Webhook URL is loaded from the environment (defaulting to empty if not set)
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

async def send_incident_alert(pr_name: str, deployment: str, error_state: str, severity: str, root_cause: str, patch_preview: str) -> None:
    """
    Sends a generic JSON webhook alert containing incident details.
    Perfect for piping into webhook.site, PagerDuty events API, or Discord.
    """
    if not ALERT_WEBHOOK_URL:
        logger.debug("[webhook] ALERT_WEBHOOK_URL is not set. Skipping notification.")
        return

    # A structured, rich JSON payload that looks professional in any alerting system
    payload = {
        "title": f"🚨 Critical Incident: {deployment} is in {error_state}",
        "incident_id": pr_name,
        "severity": severity.upper(),
        "status": "AI Patch Proposed — Awaiting Approval",
        "details": {
            "deployment": deployment,
            "error": error_state,
            "root_cause_analysis": root_cause,
        },
        "remediation_patch": patch_preview,
        "action_required": f"kubectl patch pr {pr_name} -n production --type=merge --subresource=status -p '{{\"status\":{{\"approvalState\":\"Approved\"}}}}'"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                ALERT_WEBHOOK_URL,
                json=payload,
                timeout=5.0
            )
            if response.status_code in (200, 201, 202, 204):
                logger.info("[webhook] ✅ Successfully sent alert to webhook for %s", pr_name)
            else:
                logger.warning("[webhook] ⚠️ Webhook returned status %s: %s", response.status_code, response.text)
    except Exception as exc:
        logger.error("[webhook] ❌ Failed to send webhook alert for %s: %s", pr_name, exc)
