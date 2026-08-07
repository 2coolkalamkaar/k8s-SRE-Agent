# 🔍 Observability Stack Implementation Guide

This document provides a comprehensive overview of the Observability Stack (Prometheus, Grafana, and OpenTelemetry/Tempo) integrated into the Kubernetes AI SRE Agent. It details the infrastructure setup, the specific codebase changes made, and the troubleshooting of errors encountered during the deployment.

---

## 1. Architecture Overview

To provide deep visibility into the AI SRE Agent's performance, deduplication efficiency, and Vertex AI latency, we integrated a full observability stack:

1.  **Grafana Tempo (Port 3200 / 4317)**: Receives OpenTelemetry (OTLP) traces from the `sre-controller`.
2.  **Prometheus (Port 9090)**: Scrapes custom Prometheus metrics exposed by the `sre-controller`.
3.  **Grafana (Port 3000)**: Visualizes the metrics and correlates them seamlessly with Tempo traces.

---

## 2. Infrastructure Setup & Changes

We created the `observability` namespace and deployed the following manifests:

*   **`k8s/observability/tempo.yaml`**: A lightweight Tempo deployment for distributed tracing.
*   **`k8s/observability/prometheus.yaml`**: A Prometheus server configured to scrape `http://sre-controller.monitoring.svc.cluster.local:9090/metrics`.
*   **`k8s/observability/grafana.yaml`**: A Grafana dashboard server pre-provisioned with the Prometheus and Tempo data sources, along with two custom dashboards: `SRE AI Agent Overview` and `LLM Performance`.

### Controller Infrastructure Updates
We modified the `sre-controller` deployment (`k8s/controller-deployment.yaml`) to expose the metrics port:
```yaml
ports:
  - containerPort: 8080
    name: http-metrics
  - containerPort: 9090 # Added for Prometheus Scraping
    name: metrics
```

---

## 3. Codebase Instrumentation (OpenTelemetry)

We created a central telemetry module and instrumented the controller's main diagnosis pipeline and LLM client.

### A. New File: `controller/telemetry.py`
This file initializes the OpenTelemetry SDK, configures the OTLP exporter to send traces to Tempo, and sets up a Prometheus metrics server on port 9090.

```python
# Key implementation segment in telemetry.py
def setup_telemetry():
    # 1. Setup Distributed Tracing (Tempo)
    resource = Resource.create({"service.name": "sre-controller"})
    tracer_provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint="http://tempo.observability.svc.cluster.local:4317", insecure=True)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)
    
    # 2. Setup Metrics (Prometheus)
    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    
    # Start the Prometheus HTTP Server on port 9090 in a background thread
    start_http_server(port=9090)
    
    # Define custom metrics
    global incidents_counter, dedup_hits_counter, patchrequests_counter, llm_duration_histogram
    meter = metrics.get_meter("sre.agent.meter")
    incidents_counter = meter.create_counter("sre_agent_incidents_total")
    dedup_hits_counter = meter.create_counter("sre_agent_dedup_hits_total")
    # ...
```

### B. Changes to `controller/main.py`
We wrapped the `_run_diagnosis_pipeline` in OpenTelemetry spans and added metric increments for deduplication hits.

```python
# 1. Telemetry Initialization on Startup
@kopf.on.startup()
async def on_startup(logger: logging.Logger, **kwargs):
    telemetry.setup_telemetry()

# 2. Tracking Deduplication (L1/L2/L3)
if telemetry.dedup_hits_counter:
    telemetry.dedup_hits_counter.add(1, {"layer": "l1_dampening", "namespace": namespace})

# 3. Distributed Tracing for the Diagnosis Pipeline
telemetry_tracer = telemetry.get_tracer()
with telemetry_tracer.start_as_current_span(
    "sre.diagnosis.pipeline",
    attributes={"deployment": deployment_name, "error_state": error_state}
) as pipeline_span:
    # ... executes diagnosis ...
```

### C. Changes to `controller/llm_client.py`
We added traces to track LLM latency and reliability.

```python
with tracer.start_as_current_span("sre.llm.call", attributes={"provider": provider, "model": model}) as llm_span:
    # ... calls Vertex AI ...
    duration = time.time() - start
    llm_span.set_attribute("duration_seconds", round(duration, 2))
    if telemetry.llm_duration_histogram:
        telemetry.llm_duration_histogram.record(duration, {"provider": provider, "model": model})
```

---

## 4. Errors Encountered & Resolutions

During the implementation and testing of the observability stack, we encountered and resolved several critical issues.

### 🚫 Error 1: Prometheus Failing to Scrape Metrics (Missing Server)
**The Problem**: Prometheus targets showed the `sre-controller` as `DOWN` with connection refused. The Python OpenTelemetry SDK's `PrometheusMetricReader` registers metrics, but it **does not automatically start a web server**.
**The Fix**: We imported `start_http_server` from `prometheus_client` and added `start_http_server(port=9090)` inside `telemetry.py`. We used port 9090 because `kopf` already occupies port 8080 for its internal `/healthz` checks.

### 🚫 Error 2: RBAC `pods/status` Forbidden Error
**The Problem**: When the deduplication layer fired and the controller tried to patch the pod status with its processing annotations, the following error was thrown:
> `User "system:serviceaccount:monitoring:sre-observer-sa" cannot patch resource "pods/status" in API group ""`

**The Fix**: Kubernetes treats `pods` and `pods/status` as distinct RBAC resources. We updated `k8s/rbac.yaml` to explicitly grant `patch` permissions to `pods/status`:
```diff
  - apiGroups: [""]
-   resources: [pods, pods/log, events, namespaces, configmaps]
+   resources: [pods, pods/log, pods/status, events, namespaces, configmaps]
    verbs: [get, list, watch, patch, update]
```

### 🚫 Error 3: Python `NameError: name 'get_tracer' is not defined`
**The Problem**: When refactoring imports to solve a variable caching issue, we changed `from controller.telemetry import get_tracer` to `import controller.telemetry as telemetry` in `main.py`. However, one function call `tracer = get_tracer()` was missed during the refactor, causing the pipeline to crash right before calling the LLM.
**The Fix**: We ran a multi-line replacement across `main.py` to change all instances of `get_tracer()` to `telemetry.get_tracer()`.

### 🚫 Error 4: Pod CrashLoop on Startup (`catch_up_scan` Hang)
**The Problem**: After rebuilding the controller, the pod started crashing continuously. Logs showed it was stuck at `Listing pods in namespace production...` inside the `@kopf.on.startup()` hook `catch_up_scan`. Because the startup hook was blocking on a Kubernetes API call, the `kopf` web server couldn't start, causing the Kubernetes Liveness Probe to fail and kill the pod repeatedly.
**The Fix**: We completely removed the blocking `catch_up_scan` loop from `main.py`, allowing the controller to start up immediately and serve metrics without hanging.

### 🚫 Error 5: `ClientConnectorError` & Temporary failure in name resolution
**The Problem**: The `sre-controller` logs showed an error when trying to communicate with the Kubernetes API:
> `ClientConnectorError(..., gaierror(-3, 'Temporary failure in name resolution'))`
This occurred after the host machine was put to sleep and woken up, which caused Docker's internal networking and `kind` cluster's `CoreDNS` pods to cache stale or broken network interfaces.
**The Fix**: Flushed the DNS cache and restored networking by hard-restarting the cluster's DNS pods alongside the controller pod:
```bash
kubectl delete pods -n kube-system -l k8s-app=kube-dns
kubectl delete pod -n monitoring -l app=sre-controller
```

---

## 5. Current State & Verification

The observability stack is now **fully functional**:
1. **Metrics Flow**: Prometheus successfully scrapes `sre-controller:9090/metrics` every 15 seconds.
2. **Dashboards**: Grafana successfully loads data for MTTR, LLM calls, and Deduplication hits.
3. **Stability**: The controller handles incidents cleanly without crashing, and successfully calls the Google Cloud Vertex AI API to diagnose issues.
