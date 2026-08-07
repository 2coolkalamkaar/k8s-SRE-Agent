# Observability Stack Implementation Tasks

## Phase 1: Deploy Observability Stack
- [/] Create `k8s/observability/namespace.yaml`
- [ ] Create `k8s/observability/prometheus.yaml`
- [ ] Create `k8s/observability/tempo.yaml`
- [ ] Create `k8s/observability/grafana.yaml`
- [ ] Apply all manifests to cluster
- [ ] Verify all pods Running in `observability` namespace

## Phase 2: Instrument the Controller
- [ ] Update `requirements.txt` with OTEL packages
- [ ] Create `controller/telemetry.py`
- [ ] Modify `controller/main.py` — setup_telemetry + trace spans
- [ ] Modify `controller/llm_client.py` — sre.llm.call span + histogram

## Phase 3: Rebuild & Deploy Controller
- [ ] `docker build`
- [ ] `kind load docker-image`
- [ ] `kubectl rollout restart`
- [ ] Verify metrics endpoint `/metrics` responding
- [x] Verify traces flowing to Tempo

## Phase 4: Verify Dashboards
- [ ] Grafana accessible at http://localhost:30300
- [ ] Prometheus datasource connected
- [x] Implement Grafana and Tempo configurations
- [x] Add OpenTelemetry tracing to the controller
- [x] Add Prometheus metrics for incident tracking
- [x] Verify full flow with simulated incident and verify trace appears in Tempo
