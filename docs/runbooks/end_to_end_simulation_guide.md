# 🚀 End-to-End Incident Simulation Guide

This guide provides a comprehensive step-by-step playbook for simulating a brand new incident in your cluster. By following these steps, you will trigger a new error state, watch the controller deduplicate events, observe the Vertex AI LLM diagnosis, and see the exact results populate live in your Grafana dashboards.

---

## Step 1: Clear the Previous State
To ensure the LLM is actually called (and not blocked by the Layer 3 deduplication check that looks for active `PatchRequests`), we need to clear out any old patches.

Run this in your terminal:
```bash
# Delete all existing PatchRequests in production
kubectl delete pr --all -n production
```

---

## Step 2: Trigger a Brand New Failure
Instead of reusing the `crash-demo` pod, let's simulate a brand new microservice failure: a deployment that fails to pull an image, resulting in an `ImagePullBackOff`.

Copy and paste this entire block into your terminal:
```bash
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-service
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: payment-service
  template:
    metadata:
      labels:
        app: payment-service
    spec:
      containers:
      - name: payment-app
        image: nginx:this-tag-does-not-exist-123
EOF
```
*Because the image tag `this-tag-does-not-exist-123` is invalid, the pod will immediately fail to pull the image and enter an `ErrImagePull` / `ImagePullBackOff` state.*

---

## Step 3: Watch the SRE Controller Logs (Live)
Now, let's watch the brain of the operation. The controller will wait for 3 failure events (Layer 1 Dampening) before acting.

Run this to tail the logs live:
```bash
kubectl logs -n monitoring -l app=sre-controller -f
```

**What you will see in the logs:**
1. **[dedup-L1]**: You will see it counting events: `1/3 events in window`, then `2/3`, and finally `3/3 events in window (need 3 to trigger)`.
2. **Diagnosis Queued**: `✅ production/payment-service... dampening threshold crossed — queuing diagnosis pipeline`.
3. **Vertex AI Call**: You will see OpenTelemetry starting the `sre.llm.call` span, and a log indicating the prompt was sent to Vertex AI.
4. **Diagnosis Generated**: Within ~4-6 seconds, the LLM will return a JSON response diagnosing the missing image tag.
5. **CRD Created**: The controller will print that a new `PatchRequest` (e.g., `payment-service-pr-...`) was successfully created.

*(Press `Ctrl+C` to exit the log stream once the PatchRequest is created).*

---

## Step 4: Inspect the LLM's Diagnosis
Let's see what the LLM actually recommended. 

Find the name of the new `PatchRequest`:
```bash
kubectl get pr -n production
```

Then describe it to read the LLM's Root Cause Analysis (Replace `<name>` with your actual PR name):
```bash
kubectl describe pr <name> -n production
```
**What to look for**: Look at the `Root Cause` and `LLM Summary` fields. The AI should accurately state that the image tag is invalid and recommend fixing the deployment manifest to use a valid tag (like `nginx:latest`).

---

## Step 5: Check the Grafana Dashboards
Now for the best part. Open your browser and navigate to your local Grafana instance: [http://localhost:3000](http://localhost:3000).

### Dashboard 1: SRE Agent — Overview
*   **Total Incidents Detected**: This should now increment by +1 (for the new `payment-service` incident).
*   **Active PatchRequests**: This should increment by +1.
*   **Incidents by Error State (Pie Chart)**: You will now see the pie chart split into two colors! A new slice for `ImagePullBackOff` will appear alongside the old `CrashLoopBackOff` slice.
*   **Deduplication Savings**: As the `payment-service` continues to try and pull the image and fail over the next few minutes, watch this chart grow as the L3 Layer successfully blocks duplicate LLM calls!

### Dashboard 2: SRE Agent — LLM Performance
*   **Total LLM Calls**: This should increment by +1 (because we cleared the old PR, allowing the LLM to be called for this new incident).
*   **LLM Latency Heatmap**: A new vertical bar will appear, showing exactly how fast Vertex AI processed this specific `ImagePullBackOff` prompt.
*   **Gemini p50/p95 Latency**: If Prometheus has gathered enough data points, these top panels will switch from "No data" to actual millisecond averages (e.g., `4.82s`).

---

## Step 6: Resolve the Incident (Human-in-the-loop)
Once you are done observing the metrics, you can resolve the incident by either approving the patch or simply deleting the broken deployment to clean up your cluster:

```bash
kubectl delete deployment payment-service -n production
```
