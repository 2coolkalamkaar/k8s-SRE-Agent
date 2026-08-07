# Implementation Plan: Prometheus + Grafana + Tempo Observability Stack

## Background
SigNoz was evaluated but requires 4–6 GB RAM for ClickHouse. With the host at 16 GB total and ~6.3 GB already consumed by the Kind cluster and Supabase stack, SigNoz would push available memory to the limit. Instead, we will deploy the **Prometheus + Grafana + Grafana Tempo** stack which provides identical capabilities at ~500 MB total.

| Component | Purpose | RAM |
|---|---|---|
| **Prometheus** | Scrapes and stores metrics | ~150 MB |
| **Grafana** | Dashboards (metrics + traces + logs) | ~100 MB |
| **Grafana Tempo** | Stores distributed traces (replaces SigNoz traces) | ~150 MB |
| **Total** | | **~500 MB** |

---

## Architecture

```
sre-controller pod
    │
    ├── OpenTelemetry SDK
    │   ├── Traces  ──────────────→  Grafana Tempo (OTLP/gRPC :4317)
    │   └── Metrics ──────────────→  Prometheus scrapes /metrics endpoint
    │
    └── Structured Logs (stdout) → visible in Grafana via Loki (optional later)

Grafana
    ├── Data Source: Prometheus (metrics)
    ├── Data Source: Tempo (traces)
    └── Custom Dashboards:
        ├── SRE Agent Overview
        ├── LLM Performance
        └── Incident Trace Explorer
```

---

## Proposed Changes

---

### Phase 1: Deploy the Observability Stack

#### [NEW] `k8s/observability/namespace.yaml`
Create a dedicated `observability` namespace.

#### [NEW] `k8s/observability/prometheus.yaml`
- A `ConfigMap` with `prometheus.yml` scrape config targeting the `sre-controller` `/metrics` endpoint on port `8080`
- A `Deployment` for Prometheus with `200m` CPU / `256Mi` memory limits
- A `ClusterIP` Service

#### [NEW] `k8s/observability/tempo.yaml`
- A `ConfigMap` with a minimal Tempo config file (OTLP receiver enabled on `:4317`)
- A `Deployment` for `grafana/tempo:latest` with `100m` CPU / `256Mi` memory limits
- A `ClusterIP` Service exposing ports `4317` (OTLP ingest) and `3200` (Tempo query API)

#### [NEW] `k8s/observability/grafana.yaml`
- A `ConfigMap` with datasource provisioning (Prometheus + Tempo auto-wired)
- A `ConfigMap` with the custom dashboard JSON (SRE Agent Overview + LLM Performance)
- A `Deployment` for Grafana
- A `NodePort` Service exposing the UI on port `30300` → `http://localhost:30300`

---

### Phase 2: Instrument the Controller (OpenTelemetry)

#### [MODIFY] `requirements.txt`
Add three lightweight OTEL packages:
```text
opentelemetry-sdk>=1.25.0
opentelemetry-exporter-otlp-proto-grpc>=1.25.0
opentelemetry-instrumentation-httpx>=0.46b0
```

#### [NEW] `controller/telemetry.py`
A single module that wires up all three OTEL providers:

```python
def setup_telemetry():
    # 1. Tracer → sends spans to Grafana Tempo via OTLP/gRPC
    tracer_provider = TracerProvider(resource=Resource({SERVICE_NAME: "sre-controller"}))
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint="tempo.observability.svc:4317", insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    # 2. Meter → exposes /metrics for Prometheus to scrape
    meter_provider = MeterProvider(
        metric_readers=[PrometheusMetricReader()]
    )
    metrics.set_meter_provider(meter_provider)
```

Metrics defined here:
| Metric | Type | Labels |
|---|---|---|
| `sre_agent_incidents_total` | Counter | `namespace`, `deployment`, `error_state` |
| `sre_agent_dedup_hits_total` | Counter | `layer` (l1/l2/l3) |
| `sre_agent_llm_duration_seconds` | Histogram | `provider`, `model` |
| `sre_agent_llm_errors_total` | Counter | `provider`, `reason` |
| `sre_agent_patchrequests_total` | Counter | `namespace`, `outcome` |

#### [MODIFY] `controller/main.py`
- Call `setup_telemetry()` at startup
- Wrap `_run_diagnosis_pipeline()` with a root trace span `sre.diagnosis.pipeline`
- Add child spans for each dedup layer and CRD creation:
  ```
  sre.diagnosis.pipeline                ← root span per incident
    ├── sre.dedup.l1_dampening
    ├── sre.dedup.l2_fingerprint
    ├── sre.dedup.l3_pr_check
    ├── sre.llm.call                    ← added in llm_client.py
    └── sre.crd.create_patchrequest
  ```
- Increment the appropriate metrics counters on each code path

#### [MODIFY] `controller/llm_client.py`
- Wrap the main `call_llm()` function with a `sre.llm.call` span
- Record `provider`, `model`, `duration_seconds`, `success=true/false` as span attributes
- Record duration in the `sre_agent_llm_duration_seconds` histogram

---

### Phase 3: Custom Grafana Dashboards

Two dashboards will be provisioned automatically as Grafana ConfigMaps:

**Dashboard 1 — SRE Agent Overview:**
- Total incidents detected (counter over time, area chart)
- Incidents by error state (donut: CrashLoopBackOff / OOMKilled / Error)
- Dedup savings: L1 vs L2 vs L3 hit rate breakdown (bar chart)
- Active PatchRequests (stat panel)

**Dashboard 2 — LLM Performance:**
- Gemini (Vertex AI) p50 / p95 / p99 call latency (heatmap)
- LLM error rate over time
- Provider breakdown (Vertex AI vs Ollama fallback)
- Total LLM calls (stat panel)

**Dashboard 3 — Trace Explorer (via Tempo datasource):**
- Click any incident in Grafana → jump to its full distributed trace
- See exact waterfall: Dedup → LLM → CRD

---

### Phase 4: Rebuild & Deploy Updated Controller

```bash
docker build -t sre-controller:latest .
kind load docker-image sre-controller:latest --name sre-agent-cluster
kubectl rollout restart deployment sre-controller -n monitoring
```

---

## Verification Plan

### After Phase 1:
```bash
kubectl get pods -n observability  # All 3 pods Running
# Access Grafana at http://localhost:30300 (admin/admin)
```

### After Phase 2 & 4 (Instrumentation):
1. Trigger a crash-demo incident
2. Open Grafana → Explore → Tempo → search for `service.name = sre-controller`
3. Click the trace → verify waterfall shows all spans
4. Open Grafana → Dashboards → SRE Agent Overview → verify `incidents_total` incremented

### Success Criteria:
- ✅ All 3 observability pods running and healthy
- ✅ Grafana UI accessible at `http://localhost:30300`
- ✅ Prometheus scraping metrics from `sre-controller` 
- ✅ Traces appearing in Grafana Tempo for each incident
- ✅ Custom dashboards load with real data
