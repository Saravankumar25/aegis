#!/usr/bin/env bash
# Bring up the Aegis local chaos-testing environment (ESD §18):
#   kind cluster -> kube-prometheus-stack -> Meridian Commerce services -> read-only MCP RBAC.
# Idempotent: safe to re-run. Tool binaries are taken from env vars so this works whether or not
# kind/kubectl/helm are on PATH.
set -euo pipefail

KIND="${KIND:-kind}"
KUBECTL="${KUBECTL:-kubectl}"
HELM="${HELM:-helm}"
CLUSTER_NAME="aegis"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Creating kind cluster '${CLUSTER_NAME}' (if absent)"
if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  "$KIND" create cluster --config "${HERE}/kind/cluster.yaml" --wait 120s
else
  echo "    cluster already exists"
fi

echo "==> Building and loading the Meridian service image"
docker build -t aegis/meridian-service:local "${HERE}/meridian"
"$KIND" load docker-image aegis/meridian-service:local --name "$CLUSTER_NAME"

echo "==> Installing kube-prometheus-stack (namespace: monitoring)"
"$HELM" repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
"$HELM" repo update >/dev/null
"$HELM" upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f "${HERE}/kube-prometheus-stack/values.yaml" \
  --wait --timeout 10m

echo "==> Applying Meridian namespace + read-only MCP RBAC"
"$KUBECTL" apply -f "${HERE}/manifests/meridian-namespace.yaml"
"$KUBECTL" apply -f "${HERE}/manifests/mcp-rbac.yaml"

echo "==> Deploying Meridian services + ServiceMonitor"
"$KUBECTL" apply -f "${HERE}/manifests/meridian-services.yaml"
"$KUBECTL" -n meridian rollout status deploy/checkout-service --timeout 120s
"$KUBECTL" -n meridian rollout status deploy/payment-service --timeout 120s
"$KUBECTL" -n meridian rollout status deploy/catalog-service --timeout 120s

echo "==> Done. Prometheus: http://localhost:9090  Alertmanager: http://localhost:9093"
