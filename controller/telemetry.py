"""
telemetry.py — OpenTelemetry setup for the K8s AI SRE Agent.

Sets up:
  - TracerProvider  → sends spans to Grafana Tempo via OTLP/gRPC
  - MeterProvider   → exposes /metrics on port 8080 for Prometheus to scrape
  - Instrumentation → auto-instruments httpx HTTP calls as child spans

Usage:
    from controller.telemetry import setup_telemetry, get_tracer, get_meter
    setup_telemetry()          # call once at startup
    tracer = get_tracer()      # use in any module for tracing
    meter  = get_meter()       # use for custom metrics
"""

from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

# ── OTEL environment config ────────────────────────────────────────────────────
TEMPO_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://tempo.observability.svc.cluster.local:4317"
)
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "sre-controller")

# Module-level tracer / meter singletons
_tracer = None
_meter = None

# ── Metric instruments (populated during setup_telemetry) ─────────────────────
incidents_counter = None       # sre_agent_incidents_total
dedup_hits_counter = None      # sre_agent_dedup_hits_total
llm_duration_histogram = None  # sre_agent_llm_duration_seconds
llm_errors_counter = None      # sre_agent_llm_errors_total
patchrequests_counter = None   # sre_agent_patchrequests_total
outcome_counter = None         # sre_agent_patch_outcomes_total
mttr_histogram = None          # sre_agent_mttr_seconds
mttd_histogram = None          # sre_agent_mttd_seconds


def setup_telemetry() -> None:
    """
    Initialise all OpenTelemetry providers.
    Safe to call multiple times (idempotent via global flag).
    """
    global _tracer, _meter
    global incidents_counter, dedup_hits_counter
    global llm_duration_histogram, llm_errors_counter, patchrequests_counter
    global outcome_counter, mttr_histogram, mttd_histogram

    if _tracer is not None:
        return  # already initialised

    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME as OTEL_SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from prometheus_client import start_http_server

        resource = Resource(attributes={OTEL_SERVICE_NAME: SERVICE_NAME})

        # ── Tracer: sends spans to Grafana Tempo ─────────────────────────────
        tracer_provider = TracerProvider(resource=resource)
        otlp_exporter = OTLPSpanExporter(
            endpoint=TEMPO_ENDPOINT,
            insecure=True,
        )
        tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        trace.set_tracer_provider(tracer_provider)
        _tracer = trace.get_tracer(SERVICE_NAME)
        logger.info("✅ OTEL Tracer configured → %s", TEMPO_ENDPOINT)

        # ── Meter: exposes /metrics for Prometheus via prometheus_client ──────
        prometheus_reader = PrometheusMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
        metrics.set_meter_provider(meter_provider)
        _meter = metrics.get_meter(SERVICE_NAME)

        # Start the HTTP server that serves /metrics on port 9090
        # (kopf already owns port 8080 for /healthz)
        start_http_server(port=9090)
        logger.info("✅ OTEL Meter configured — metrics available at :9090/metrics")

        # ── Define all metric instruments ─────────────────────────────────────
        incidents_counter = _meter.create_counter(
            name="sre_agent_incidents_total",
            description="Total number of incidents detected by the SRE agent",
            unit="1",
        )
        dedup_hits_counter = _meter.create_counter(
            name="sre_agent_dedup_hits_total",
            description="Number of events suppressed by each deduplication layer",
            unit="1",
        )
        llm_duration_histogram = _meter.create_histogram(
            name="sre_agent_llm_duration_seconds",
            description="LLM call duration in seconds",
            unit="s",
        )
        llm_errors_counter = _meter.create_counter(
            name="sre_agent_llm_errors_total",
            description="Total number of LLM call failures",
            unit="1",
        )
        patchrequests_counter = _meter.create_counter(
            name="sre_agent_patchrequests_total",
            description="Total PatchRequest CRDs created by the SRE agent",
            unit="1",
        )
        outcome_counter = _meter.create_counter(
            name="sre_agent_patch_outcomes_total",
            description="Patch outcome: success or rollback",
            unit="1",
        )
        mttr_histogram = _meter.create_histogram(
            name="sre_agent_mttr_seconds",
            description="Mean Time to Resolution per incident in seconds",
            unit="s",
        )
        mttd_histogram = _meter.create_histogram(
            name="sre_agent_mttd_seconds",
            description="Mean Time to Detection — seconds from pod failure start to pipeline trigger",
            unit="s",
        )

        logger.info("✅ OTEL Meter configured — metrics available on /metrics port 8080")

        # ── Auto-instrument httpx (captures Vertex AI calls as child spans) ───
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
            logger.info("✅ httpx auto-instrumentation active (Vertex AI calls will be traced)")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-httpx not installed — httpx spans disabled")

    except ImportError as exc:
        logger.warning("OpenTelemetry SDK not available (%s) — running without observability", exc)
        # Provide no-op stubs so the rest of the code doesn't need try/except
        _tracer = _NoOpTracer()
        _meter = None


def get_tracer():
    """Return the configured tracer (or a no-op stub if OTEL is unavailable)."""
    global _tracer
    if _tracer is None:
        setup_telemetry()
    return _tracer


def get_meter():
    """Return the configured meter."""
    return _meter


# ── No-op tracer stub (used if OTEL SDK is missing) ──────────────────────────
class _NoOpSpan:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def set_attribute(self, *_): pass
    def set_status(self, *_): pass
    def record_exception(self, *_): pass


class _NoOpTracer:
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()
