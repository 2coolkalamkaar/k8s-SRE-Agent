# ⚡ Dual-Provider LLM Architecture (Cloud Gemini API + Local Ollama Fallback)

> **Objective**: Eliminate LLM diagnostic latency (dropping from ~4m40s to ~1-2 seconds) using Cloud Gemini API while maintaining local privacy/air-gapped resilience via automatic local Ollama fallback.

---

## 🎯 Architectural Overview

```
                      ┌─────────────────────────────────────────────────┐
                      │ Kubernetes SRE Operator (Kopf)                  │
                      │ Incident Handler Triggered                      │
                      └────────────────────────┬────────────────────────┘
                                               │
                                               ▼
                      ┌─────────────────────────────────────────────────┐
                      │ Unified LLM Client (controller/llm_client.py)   │
                      │ Provider Selection: LLM_PROVIDER=auto           │
                      └────────────────────────┬────────────────────────┘
                                               │
                       ┌───────────────────────┴───────────────────────┐
                       │                                               │
          [Primary: Cloud Provider]                         [Fallback: Local Provider]
                       │                                               │
                       ▼                                               ▼
    ┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
    │ Google Gemini REST API               │        │ Local Ollama Service                 │
    │ Model: gemini-2.0-flash / 1.5-flash  │        │ Model: deepseek-coder:6.7b-instruct  │
    │ Response Time: ~1.2 seconds ⚡       │        │ Response Time: ~4m 40s (CPU)         │
    │ Native JSON Schema enforcement       │        │ In-cluster CPU/GPU inference         │
    └──────────────────┬───────────────────┘        └──────────────────┬───────────────────┘
                       │                                               │
                       │ (On 401/403/Timeout/RateLimit)                │
                       └────────────────► Fallback ────────────────────┘
                                               │
                                               ▼
                      ┌─────────────────────────────────────────────────┐
                      │ 5-Layer JSON Robustness Parser                  │
                      │ Guarantees zero crashes on malformed LLM outputs│
                      └────────────────────────┬────────────────────────┘
                                               │
                                               ▼
                      ┌─────────────────────────────────────────────────┐
                      │ Custom Resource Generation                      │
                      │ PatchRequest & IncidentRecord CRDs Created      │
                      └─────────────────────────────────────────────────┘
```

---

## ⚙️ Configuration & Environment Variables

The controller dynamically checks for provider keys and env vars at runtime:

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `auto` | Provider mode: `auto` (prefers Gemini if key set, else Ollama), `gemini`, or `ollama`. |
| `GEMINI_API_KEY` | *(read from Secret / `.env`)* | API Key for Google Gemini REST API. Injected via Kubernetes Secret `sre-llm-secret`. |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model target (`gemini-2.0-flash`, `gemini-1.5-flash`, `gemini-1.5-pro`). |
| `OLLAMA_URL` | `http://ollama-service.ai-infra.svc.cluster.local:11434` | Endpoint for local Ollama service inside the cluster. |
| `OLLAMA_MODEL` | `deepseek-coder:6.7b-instruct` | Local model name for Ollama fallback. |
| `OLLAMA_TIMEOUT` | `300` | Timeout in seconds for local CPU inference. |

---

## 🔐 Kubernetes Secret Management

The Gemini API Key is securely stored as a Kubernetes Secret in the `monitoring` namespace and mounted into the controller pod:

```bash
# Create/Sync secret from .env
kubectl create secret generic sre-llm-secret \
  --from-literal=gemini-api-key="<YOUR_GEMINI_API_KEY>" \
  -n monitoring \
  --dry-run=client -o yaml | kubectl apply -f -
```

In `k8s/controller-deployment.yaml`:
```yaml
env:
  - name: LLM_PROVIDER
    value: "auto"
  - name: GEMINI_MODEL
    value: "gemini-2.0-flash"
  - name: GEMINI_API_KEY
    valueFrom:
      secretKeyRef:
        name: sre-llm-secret
        key: gemini-api-key
        optional: true
```

---

## 🛡️ Automatic Fallback Logic

1. When an incident triggers, `call_llm()` evaluates the preferred provider (`gemini`).
2. If `GEMINI_API_KEY` is present, it attempts to call Google Gemini API with a 30-second timeout.
3. If Gemini returns HTTP 200, the diagnosis completes in **~1.2 seconds**.
4. If Gemini fails (e.g., HTTP 403 authorization error, rate limit, quota exceeded, or network outage), the client logs a warning:
   ```
   [INC-2026-0727-1011] Gemini provider unavailable or failed — falling back to local Ollama...
   ```
5. The request seamlessly falls back to `call_ollama()`, ensuring **zero single point of failure (SPOF)**.

---

## 📊 Performance Comparison

| Metric | Local Ollama (CPU) | Cloud Gemini Flash |
|---|---|---|
| **Diagnostic Latency** | 4 minutes 40 seconds | **1.2 to 1.8 seconds** ⚡ |
| **Throughput** | ~2.1 tokens/sec | **>150 tokens/sec** |
| **CPU Utilization** | High (100% on 3 cores) | Negligible (HTTP I/O only) |
| **JSON Consistency** | Requires 5-layer regex parsing | Native `response_mime_type="application/json"` |
| **Resilience** | Operates air-gapped / offline | High SLA (99.9%) with local fallback |
