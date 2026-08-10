#!/usr/bin/env bash
# ============================================================
# bootstrap.sh — Full SRE Agent Infrastructure Setup
# Run this from the project root:  bash bootstrap.sh
# ============================================================
set -euo pipefail

CLUSTER_NAME="sre-agent-cluster"
BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ── Step 0: Validate cluster is running ───────────────────────────────────────
log "Checking kind cluster..."
if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  fail "Cluster '${CLUSTER_NAME}' not found. Create it first: kind create cluster --name ${CLUSTER_NAME} --config kind-config.yaml"
fi
kubectl cluster-info --context "kind-${CLUSTER_NAME}" > /dev/null 2>&1 || fail "Cannot reach API server. Check kind cluster status."
ok "Cluster '${CLUSTER_NAME}' is reachable."

# ── Step 1: Create all namespaces first ───────────────────────────────────────
log "Creating namespaces..."
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/observability/namespace.yaml
kubectl create namespace production --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace db         --dry-run=client -o yaml | kubectl apply -f -
ok "Namespaces ready."

# ── Step 2: Priority Classes (must exist before pods) ─────────────────────────
log "Applying PriorityClasses..."
kubectl apply -f k8s/priority-classes.yaml
ok "PriorityClasses applied."

# ── Step 3: CRDs ──────────────────────────────────────────────────────────────
log "Applying CRDs..."
kubectl apply -f k8s/crd-patchrequest.yaml
kubectl apply -f k8s/crd-incidentrecord.yaml
# Wait for CRDs to be established
kubectl wait --for=condition=Established crd/patchrequests.sre.yourdomain.io --timeout=30s
kubectl wait --for=condition=Established crd/incidentrecords.sre.yourdomain.io --timeout=30s
ok "CRDs established."

# ── Step 4: RBAC ──────────────────────────────────────────────────────────────
log "Applying RBAC..."
kubectl apply -f k8s/rbac.yaml
ok "RBAC applied."

# ── Step 5: Build & load Docker images ───────────────────────────────────────
log "Building sre-controller Docker image..."
docker build -t sre-controller:latest . --quiet
log "Building sre-dashboard Docker image..."
docker build -t sre-dashboard:latest ./dashboard --quiet
log "Loading images into kind cluster (all nodes)..."
kind load docker-image sre-controller:latest --name ${CLUSTER_NAME}
kind load docker-image sre-dashboard:latest  --name ${CLUSTER_NAME}
ok "Docker images loaded."

# ── Step 6: GCP Credentials Secret (for Vertex AI) ───────────────────────────
log "Checking GCP credentials secret..."
if kubectl get secret gcp-credentials -n monitoring &>/dev/null; then
  warn "Secret 'gcp-credentials' already exists in monitoring — skipping."
else
  CREDS_FILE="${GOOGLE_APPLICATION_CREDENTIALS:-}"
  if [ -z "$CREDS_FILE" ] || [ ! -f "$CREDS_FILE" ]; then
    # Try well-known paths
    for p in "$HOME/.config/gcloud/application_default_credentials.json" "$HOME/credentials.json"; do
      [ -f "$p" ] && CREDS_FILE="$p" && break
    done
  fi
  if [ -n "$CREDS_FILE" ] && [ -f "$CREDS_FILE" ]; then
    kubectl create secret generic gcp-credentials \
      --from-file=credentials.json="$CREDS_FILE" \
      -n monitoring
    ok "GCP credentials secret created from ${CREDS_FILE}."
  else
    warn "No GCP credentials file found. Vertex AI will be unavailable."
    warn "Create it manually: kubectl create secret generic gcp-credentials --from-file=credentials.json=/path/to/key.json -n monitoring"
    # Create a dummy secret so the controller pod starts (it will fallback to Ollama)
    kubectl create secret generic gcp-credentials \
      --from-literal=credentials.json='{}' \
      -n monitoring
  fi
fi

# ── Step 7: Observability Stack ───────────────────────────────────────────────
log "Deploying Prometheus..."
kubectl apply -f k8s/observability/prometheus.yaml
log "Deploying SLO recording rules..."
kubectl apply -f k8s/observability/slo-rules.yaml
log "Deploying Grafana..."
kubectl apply -f k8s/observability/grafana.yaml
log "Deploying Tempo (distributed tracing)..."
kubectl apply -f k8s/observability/tempo.yaml
ok "Observability stack deployed."

# ── Step 8: SRE Controller ───────────────────────────────────────────────────
log "Deploying SRE Controller..."
kubectl apply -f k8s/controller-deployment.yaml
ok "SRE Controller deployed."

# ── Step 9: SRE Dashboard ─────────────────────────────────────────────────────
log "Deploying SRE Dashboard..."
kubectl apply -f k8s/dashboard-deployment.yaml
ok "SRE Dashboard deployed."

# ── Step 10: Wait for key pods ───────────────────────────────────────────────
log "Waiting for SRE Controller to be ready..."
kubectl rollout status deployment/sre-controller -n monitoring --timeout=120s

log "Waiting for SRE Dashboard to be ready..."
kubectl rollout status deployment/sre-dashboard -n monitoring --timeout=120s

log "Waiting for Prometheus to be ready..."
kubectl rollout status deployment/prometheus -n observability --timeout=120s || \
kubectl rollout status statefulset/prometheus -n observability --timeout=120s || \
warn "Prometheus not yet ready — may still be pulling image."

# ── Step 11: Final status ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  SRE Agent Infrastructure is UP!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo ""
kubectl get pods -n monitoring
echo ""
kubectl get pods -n observability
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo "  Port-forward Dashboard : kubectl port-forward svc/sre-dashboard 8081:8081 -n monitoring"
echo "  Port-forward Grafana   : kubectl port-forward svc/grafana 3000:3000 -n observability"
echo "  Port-forward Prometheus: kubectl port-forward svc/prometheus 9090:9090 -n observability"
echo ""
echo -e "${YELLOW}To trigger a test incident:${NC}"
echo "  kubectl create namespace production --dry-run=client -o yaml | kubectl apply -f -"
echo "  kubectl apply -f demo-apps/"
