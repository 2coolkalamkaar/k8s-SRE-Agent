"""
webhook_receiver.py — Receives Prometheus Alertmanager webhooks (App domain).

Why this exists (in plain words):
    Every other domain (pod, node, cluster) is detected by watching a
    Kubernetes object change. There's no Kubernetes object for "this
    deployment's error rate is elevated" — that's a metrics-level fact
    Prometheus knows about, not something visible in any object's status.
    So instead of watching, this domain listens: Alertmanager is configured
    to POST here whenever a relevant alert starts or stops firing, and this
    module turns that POST into the same internal "something is wrong"
    signal every other domain already produces.

    This runs as a small aiohttp server inside the same process as the
    kopf operator, started once at controller startup.
"""

from __future__ import annotations
import logging
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

AlertCallback = Callable[[dict], Awaitable[None]]

_runner: web.AppRunner | None = None


def _parse_alert(raw_alert: dict) -> dict | None:
    """
    Turn one entry from Alertmanager's webhook payload into the flat shape
    the rest of the pipeline expects. Returns None for alerts that aren't
    actionable (missing the labels needed to know which deployment they're
    about, or not currently firing).

    Alertmanager's payload shape (relevant parts):
        {"status": "firing"|"resolved",
         "labels": {"alertname": ..., "namespace": ..., "deployment": ..., "severity": ...},
         "annotations": {"description": ..., "summary": ...}}
    """
    if raw_alert.get("status") != "firing":
        return None

    labels = raw_alert.get("labels", {})
    annotations = raw_alert.get("annotations", {})

    namespace = labels.get("namespace")
    deployment = labels.get("deployment") or labels.get("deployment_name")
    if not namespace or not deployment:
        logger.warning(
            "[webhook] Alert %r missing namespace/deployment label — cannot route, skipping",
            labels.get("alertname"),
        )
        return None

    return {
        "alert_name": labels.get("alertname", "UnknownAlert"),
        "namespace": namespace,
        "deployment": deployment,
        "severity": labels.get("severity", "warning"),
        "description": annotations.get("description") or annotations.get("summary", ""),
        "labels": labels,
    }


def _make_app(on_alert: AlertCallback) -> web.Application:
    async def handle_alerts(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception as exc:
            logger.warning("[webhook] Failed to parse request body as JSON: %s", exc)
            return web.json_response({"error": "invalid JSON"}, status=400)

        alerts = payload.get("alerts", [])
        routed = 0
        for raw_alert in alerts:
            alert_context = _parse_alert(raw_alert)
            if alert_context is None:
                continue
            # Fire-and-forget: Alertmanager expects a fast response, the
            # actual diagnosis pipeline runs independently.
            import asyncio
            asyncio.create_task(on_alert(alert_context))
            routed += 1

        return web.json_response({"received": len(alerts), "routed": routed})

    app = web.Application()
    app.router.add_post("/alerts", handle_alerts)
    return app


async def start(on_alert: AlertCallback, port: int = 8090) -> None:
    """Start the webhook server. Call once from the controller's startup
    activity; the returned runner is kept module-level so it isn't garbage
    collected and stops serving."""
    global _runner
    if _runner is not None:
        return
    app = _make_app(on_alert)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "0.0.0.0", port)
    await site.start()
    logger.info("✅ Alertmanager webhook receiver listening on :%d/alerts", port)
