# Live Simulation Report: Pod Failure & Automated Diagnosis

This document captures a complete end-to-end trace of a live pod failure simulation to verify that the Kubernetes AI SRE Agent is working correctly. 

## Scenario Details
* **Target Deployment**: `crash-demo` in the `production` namespace.
* **Failure Mode**: The application code crashes repeatedly on startup due to a missing configuration file, placing the pod into a `CrashLoopBackOff` state.
* **Objective**: Verify that the SRE Controller correctly detects the failure, dampens the event noise, diagnoses the crash using an LLM, and creates the corresponding CRDs (`PatchRequest` and `IncidentRecord`) without human intervention until the final approval step.

---

## 1. Complete Incident Timeline

Here is the exact real-time sequence of events traced from the controller logs:

| Timestamp (UTC) | Action / Event |
|:---|:---|
| **07:43:17** | **Detection**: `crash-demo` pod enters `CrashLoopBackOff`. Controller Watch detects it. |
| **07:43:17** | **L1 Dedup**: Controller logs `1/3 events in window`. Event dampened. |
| **07:43:19** | **L1 Dedup**: Second pod restart. Controller logs `2/3 events in window`. Event dampened. |
| **07:43:20** | **L1 Dedup Trigger**: Third pod restart. `3/3 events` threshold crossed. Diagnosis pipeline queued. |
| **07:43:20** | **L2 & L3 Dedup**: Cache checks pass cleanly (no existing duplicates). |
| **07:43:20** | **LLM Handoff**: Incident `INC-2026-0806-D92A` raised. Semaphore acquired, sending context to Ollama. |
| **07:46:48** | **LLM Return**: Ollama CPU inference completes (~3.5 minutes). JSON successfully parsed. |
| **07:46:48** | **CRD Creation**: `PatchRequest` (`crash-demo-pr-2026-0806-d92a`) and `IncidentRecord` created in K8s. |
| **07:49:17** | **Dedup Fix Verification**: Fourth pod restart. **L2 Dedup** correctly catches the duplicate fingerprint. |
| **07:49:17** | **L3 Escalation**: Controller correctly increments `seenCount` on the existing PR to **4** without errors. |

---

## 2. The LLM Diagnosis Phase

### The Input Context
When the controller called the LLM, it constructed a memory-augmented prompt containing the following injected environment context:
* **Pod**: `crash-demo-59465cff56-nn9lj` (Namespace: `production`)
* **Error State**: `CrashLoopBackOff`
* **Restart Count**: `3`
* **Preprocessed Logs**: (Extracted by `log_preprocessor.py` stripping out noise)
  ```text
  FileNotFoundError: [Errno 2] No such file or directory: '/etc/app/config.yaml'
  ```
* **K8s Events**: Recent backoff events fetched directly from the K8s API.

### The Ollama Response
The local Ollama model (`deepseek-coder:6.7b-instruct`) analyzed the context and returned the following JSON diagnosis:

```json
{
  "root_cause": "The application container cannot find the configuration file /etc/app/config.yaml.",
  "severity": "low",
  "suggested_fix": "Ensure that the config.yaml file is present in the expected location and accessible by the container.",
  "auto_restart_safe": false,
  "config_suggestions": [
    "CONFIG_PATH=/etc/app/config.yaml"
  ],
  "likely_recurring": true,
  "estimated_impact": "The application will not start until the configuration file is available.",
  "matches_past_incident": null,
  "confidence_boost": "high"
}
```

---

## 3. Final Output (CRDs Generated)

The controller parsed the LLM response and successfully manifested it into the cluster as a `PatchRequest`. 

Running `kubectl get pr crash-demo-pr-2026-0806-d92a -n production` confirms the state:

```yaml
apiVersion: sre.yourdomain.io/v1alpha1
kind: PatchRequest
metadata:
  name: crash-demo-pr-2026-0806-d92a
  namespace: production
  labels:
    incident-id: INC-2026-0806-D92A
    target-deployment: crash-demo
spec:
  incidentId: INC-2026-0806-D92A
  errorState: CrashLoopBackOff
  severity: low
  confidence: high
  rootCause: The application container cannot find the configuration file /etc/app/config.yaml.
  llmSummary: Ensure that the config.yaml file is present in the expected location and accessible by the container.
  humanNote: The application will not start until the configuration file is available.
  autoRestartSafe: false
  likelyRecurring: true
  seenCount: 4 
```

**Status**: The bug in `dedup.py` causing the HTTP `400 Bad Request` during `seenCount` incrementing was successfully fixed (as evidenced by `seenCount: 4` above).

## Conclusion
The AI SRE Agent system works flawlessly end-to-end. It successfully intercepts noise, builds robust prompts, executes local CPU inference without timing out, creates trackable K8s CRDs, and safely increments the seen count for ongoing active incidents.
