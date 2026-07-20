#!/usr/bin/env bash
# Manual failure injection for Meridian services (ESD §18/§19 — deliberate incident generation).
# Usage:
#   inject-failure.sh error   <service> [rate]        # inject an error-rate spike (default 0.5)
#   inject-failure.sh latency <service> [latency_ms]  # inject a latency regression (default 800)
#   inject-failure.sh clear   <service>               # clear injected failure
#   inject-failure.sh killpod <service>               # delete one pod (crash-loop style incident)
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NS="meridian"
ACTION="${1:-}"; SERVICE="${2:-}"; ARG="${3:-}"

usage() { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ -z "$ACTION" ] || [ -z "$SERVICE" ] && usage

post() {
  # POST to the service's /admin/failure via a throwaway curl pod inside the cluster.
  local body="$1"
  "$KUBECTL" -n "$NS" run curl-$RANDOM --rm -i --restart=Never --image=curlimages/curl:8.10.1 -- \
    -s -X POST "http://${SERVICE}.${NS}.svc.cluster.local:8080/admin/failure" \
    -H 'Content-Type: application/json' -d "$body"
}

case "$ACTION" in
  error)   post "{\"mode\":\"error\",\"rate\":${ARG:-0.5}}" ;;
  latency) post "{\"mode\":\"latency\",\"rate\":1.0,\"latency_ms\":${ARG:-800}}" ;;
  clear)   post "{\"mode\":\"none\",\"rate\":0}" ;;
  killpod) "$KUBECTL" -n "$NS" delete pod -l "app=${SERVICE}" --grace-period=0 --force | head -1 ;;
  *) usage ;;
esac
