# SRE Agent — Capability Test Matrix

Run this top-to-bottom to validate what the system can and cannot handle.

## Tier Classification

| Tier | Description | Our System |
|------|-------------|------------|
| **T1** | Pod crashes / container failures | ✅ Full detection + remediation |
| **T2** | Config / secret / image failures | ✅ Full detection + remediation |
| **T3** | Service connectivity / network | ⚠️ Partial (detects underlying pod crash) |
| **T4** | Application-level (500s, latency) | ❌ Not detected without Prometheus webhook |
| **T5** | Scheduling / node-level | ❌ Not detected (no containerStatuses change) |

---

## Test Environment Setup

```bash
# Ensure controller is running
kubectl get pods -n monitoring | grep sre-controller

# Watch PRs in real time (separate terminal)
watch -n 3 kubectl get pr -A

# Stream controller logs (separate terminal)
kubectl logs -l app=sre-controller -n monitoring -f
```

---

## T1 — Pod Crash Failures

### T1.1 — CrashLoopBackOff (bad entrypoint)
**Simulates:** Dev pushed code with a broken startup command.

```bash
kubectl create deployment crash-test -n production \
  --image=busybox -- /bin/sh -c "exit 1"
```

**Expected:** Controller detects after 3 crashes (~30s), LLM diagnoses bad entrypoint, PatchRequest created.

**Cleanup:** `kubectl delete deployment crash-test -n production`

---

### T1.2 — OOMKilled
**Simulates:** Memory limit too low for the workload.

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oom-test
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oom-test
  template:
    metadata:
      labels:
        app: oom-test
    spec:
      containers:
      - name: oom-app
        image: polinux/stress
        command: ["stress"]
        args: ["--vm", "1", "--vm-bytes", "200M", "--vm-hang", "0"]
        resources:
          limits:
            memory: "50Mi"
YAML
```

**Expected:** Pod OOMKilled → CrashLoopBackOff, LLM proposes memory limit increase.

**Cleanup:** `kubectl delete deployment oom-test -n production`

---

### T1.3 — Init Container Failure ⚠️ GAP TEST
**Simulates:** DB migration init container fails.

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: init-fail-test
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: init-fail-test
  template:
    metadata:
      labels:
        app: init-fail-test
    spec:
      initContainers:
      - name: db-migrate
        image: busybox
        command: ["/bin/sh", "-c", "echo 'DB migration failed'; exit 1"]
      containers:
      - name: app
        image: nginx
YAML
```

**Expected:** Pod in `Init:CrashLoopBackOff` — agent likely misses this (watches `containerStatuses`, not `initContainerStatuses`).

**Cleanup:** `kubectl delete deployment init-fail-test -n production`

---

## T2 — Config / Secret / Image Failures

### T2.1 — Missing ConfigMap (CreateContainerConfigError)

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: missing-config-test
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: missing-config-test
  template:
    metadata:
      labels:
        app: missing-config-test
    spec:
      containers:
      - name: app
        image: nginx
        envFrom:
        - configMapRef:
            name: non-existent-config
YAML
```

**Expected:** `CreateContainerConfigError` detected, LLM diagnoses missing ConfigMap.

**Cleanup:** `kubectl delete deployment missing-config-test -n production`

---

### T2.2 — Missing Secret (CreateContainerConfigError)

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: missing-secret-test
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: missing-secret-test
  template:
    metadata:
      labels:
        app: missing-secret-test
    spec:
      containers:
      - name: app
        image: nginx
        env:
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: non-existent-secret
              key: password
YAML
```

**Expected:** `CreateContainerConfigError` detected.

**Cleanup:** `kubectl delete deployment missing-secret-test -n production`

---

### T2.3 — Wrong Image Tag (ImagePullBackOff)

```bash
kubectl create deployment bad-image-test -n production \
  --image=nginx:this-tag-does-not-exist-v999
```

**Expected:** `ImagePullBackOff` detected, LLM diagnoses bad image reference.

**Cleanup:** `kubectl delete deployment bad-image-test -n production`

---

### T2.4 — Wrong Config File Path (CoreDNS)

```bash
kubectl patch deployment coredns -n kube-system --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args/1","value":"/etc/coredns/wrongfile"}]'
```

**Expected:** CrashLoopBackOff, LLM fixes args back to `/etc/coredns/Corefile`. Already validated.

**Cleanup:** `kubectl patch deployment coredns -n kube-system --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args/1","value":"/etc/coredns/Corefile"}]'`

---

## T3 — Service Connectivity (Complex)

### T3.1 — Service Unreachable Because Pod is Crashing
**Root cause visible to agent — detects pod crash, fixing it restores service.**

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend-api
  namespace: production
spec:
  replicas: 2
  selector:
    matchLabels:
      app: backend-api
  template:
    metadata:
      labels:
        app: backend-api
    spec:
      containers:
      - name: api
        image: busybox
        command: ["/bin/sh", "-c", "echo startup error; exit 1"]
---
apiVersion: v1
kind: Service
metadata:
  name: backend-api-svc
  namespace: production
spec:
  selector:
    app: backend-api
  ports:
  - port: 8080
    targetPort: 8080
YAML
```

**Expected:** Agent detects pod crash (root cause), creates PR. Fixing the crash restores the service. ✅

**Cleanup:** `kubectl delete deployment backend-api -n production && kubectl delete svc backend-api-svc -n production`

---

### T3.2 — Wrong Label Selector ⚠️ GAP TEST
**Simulates:** Service selector doesn't match pod labels (pod is healthy but service has 0 endpoints).

```bash
kubectl apply -f - <<YAML
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
      - name: app
        image: nginx
---
apiVersion: v1
kind: Service
metadata:
  name: payment-svc
  namespace: production
spec:
  selector:
    app: payment-svc-WRONG
  ports:
  - port: 80
    targetPort: 80
YAML
```

**Expected:** Pod is `Running` — agent sees nothing. ❌ Known gap.

**Cleanup:** `kubectl delete deployment payment-service -n production && kubectl delete svc payment-svc -n production`

---

### T3.3 — Accidental Scale to Zero ⚠️ GAP TEST

```bash
kubectl scale deployment cart-service -n production --replicas=0
```

**Expected:** No crash events — agent sees nothing. ❌ Known gap.

**Cleanup:** `kubectl scale deployment cart-service -n production --replicas=1`

---

## T4 — Application Level (❌ Requires Prometheus Webhook)

### T4.1 — HTTP 500 Rate
Not detectable. Would need:
```yaml
- alert: HighErrorRate
  expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
  → POST /webhook/alertmanager
```

### T4.2 — High P99 Latency
Not detectable. Same — needs Prometheus alert.

---

## T5 — Scheduling / Node Level

### T5.1 — Pod Pending (Insufficient Resources) ⚠️ GAP TEST

```bash
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: resource-hog-test
  namespace: production
spec:
  replicas: 1
  selector:
    matchLabels:
      app: resource-hog
  template:
    metadata:
      labels:
        app: resource-hog
    spec:
      containers:
      - name: app
        image: nginx
        resources:
          requests:
            cpu: "100"
            memory: "500Gi"
YAML
```

**Expected:** Pod stays `Pending` forever — agent sees nothing. ❌ Known gap.

**Cleanup:** `kubectl delete deployment resource-hog-test -n production`

---

## Results Table (Fill in as you run)

| Test | Scenario | Detected | Diagnosed | Fix Proposed | Pass/Fail |
|------|----------|----------|-----------|--------------|-----------|
| T1.1 | CrashLoopBackOff | | | | |
| T1.2 | OOMKilled | | | | |
| T1.3 | Init container fail | | | | |
| T2.1 | Missing ConfigMap | | | | |
| T2.2 | Missing Secret | | | | |
| T2.3 | Wrong image tag | | | | |
| T2.4 | Wrong config path | | | | |
| T3.1 | Svc unreachable (pod crash) | | | | |
| T3.2 | Wrong label selector | | | | |
| T3.3 | Scale to zero | | | | |
| T4.1 | HTTP 500 errors | | | | |
| T5.1 | Pod Pending | | | | |

---

## Gaps & Fixes Roadmap

| Gap | Fix | Effort |
|-----|-----|--------|
| Init container failures | Add `kopf.on.field("pods", field="status.initContainerStatuses")` | 1 hour |
| Scale-to-zero | Add deployment replicas watcher | 2 hours |
| Wrong selector / 0 endpoints | Prometheus `kube_endpoint_address_available == 0` alert | 1 day |
| Pod Pending | Pod phase watcher or Prometheus alert | 1 day |
| HTTP 500 / latency | Prometheus Alertmanager webhook receiver | 2-3 days |

---

*SRE Agent Platform v1.0 — 2026-08-10*
