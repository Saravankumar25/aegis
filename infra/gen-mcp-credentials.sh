#!/usr/bin/env bash
# Mint the k8s MCP server's credential files (ESD §16: per-MCP-server, least-privilege).
#
# Creates two GIT-IGNORED files consumed by mcp_servers/k8s via core.config:
#   infra/.k8s-mcp-token   — short-lived token for ServiceAccount aegis-system/aegis-k8s-mcp
#   infra/.k8s-mcp-ca.crt  — the cluster CA, so the client verifies TLS (no verify=False)
# and prints the API server URL to put in .env as K8S_API_URL.
#
# The token is bound to the read-only Role from infra/manifests/mcp-rbac.yaml (get/list/watch
# only, meridian namespace). Re-run whenever the token expires (default duration below).
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
CONTEXT="${CONTEXT:-kind-aegis}"
DURATION="${DURATION:-24h}"
HERE="$(cd "$(dirname "$0")" && pwd)"

"$KUBECTL" --context "$CONTEXT" -n aegis-system create token aegis-k8s-mcp \
  --duration "$DURATION" > "$HERE/.k8s-mcp-token"

"$KUBECTL" config view --raw --minify --context "$CONTEXT" \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > "$HERE/.k8s-mcp-ca.crt"

SERVER="$("$KUBECTL" config view --minify --context "$CONTEXT" \
  -o jsonpath='{.clusters[0].cluster.server}')"

echo "Wrote $HERE/.k8s-mcp-token (valid $DURATION) and $HERE/.k8s-mcp-ca.crt"
echo "Set in .env:  K8S_API_URL=$SERVER"
