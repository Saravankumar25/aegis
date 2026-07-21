#!/usr/bin/env bash
# Manual failure injection for Meridian services (ESD §18/§19 — deliberate incident generation).
# Usage:
#   inject-failure.sh error   <service> [rate]        # inject an error-rate spike (default 0.5)
#   inject-failure.sh latency <service> [latency_ms]  # inject a latency regression (default 800)
#   inject-failure.sh clear   <service>               # clear injected failure
#   inject-failure.sh status  <service>               # show applied state on every replica
#   inject-failure.sh killpod <service>               # delete one pod (crash-loop style incident)
#
# Injection is applied to every replica individually: failure mode is per-process in-memory
# state, so addressing the Service reaches exactly one pod and applies the fault to a fraction
# of traffic without saying so.
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NS="meridian"
ACTION="${1:-}"; SERVICE="${2:-}"; ARG="${3:-}"

usage() { grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ -z "$ACTION" ] || [ -z "$SERVICE" ] && usage

post() {
  # POST to /admin/failure on EVERY replica, addressed by pod IP.
  #
  # Addressing the Service instead load-balances to exactly one pod, because failure mode is
  # per-process in-memory state rather than anything shared. With the default 2 replicas that
  # made both injection and clearing silently partial: "inject 70%" produced a ~35% observed
  # error rate (70% of half the traffic), and "clear" left the other replica still failing.
  # Both were mistaken for metric-window lag during end-to-end validation before the pod-level
  # state was inspected directly.
  local body="$1"
  local pods
  pods="$("$KUBECTL" -n "$NS" get pods -l "app=${SERVICE}" \
            --field-selector=status.phase=Running \
            -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}')"
  [ -z "$pods" ] && { echo "no running pods for app=${SERVICE} in ${NS}" >&2; exit 1; }

  # One throwaway pod loops over every replica: a pod per replica is slow, and the per-pod
  # result matters — a partial application must be visible rather than assumed.
  local urls=""
  for ip in $pods; do urls="${urls} http://${ip}:8080/admin/failure"; done
  # shellcheck disable=SC2086 — word splitting over the URL list is intended.
  "$KUBECTL" -n "$NS" run curl-$RANDOM --rm -i --restart=Never --image=curlimages/curl:8.10.1 -- \
    -s -X POST -H 'Content-Type: application/json' -d "$body" $urls
  echo
}

verify() {
  # Read the applied state back from every replica. Injection that only half-applied is worse
  # than none: the environment looks healthy in aggregate while one replica misbehaves.
  echo "--- applied state per replica ---"
  for ip in $("$KUBECTL" -n "$NS" get pods -l "app=${SERVICE}" \
                --field-selector=status.phase=Running \
                -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}'); do
    "$KUBECTL" -n "$NS" run curl-$RANDOM --rm -i --restart=Never --image=curlimages/curl:8.10.1 -- \
      -s "http://${ip}:8080/admin/failure" 2>/dev/null | head -1
    echo
  done
}

case "$ACTION" in
  error)   post "{\"mode\":\"error\",\"rate\":${ARG:-0.5}}"; verify ;;
  latency) post "{\"mode\":\"latency\",\"rate\":1.0,\"latency_ms\":${ARG:-800}}"; verify ;;
  clear)   post "{\"mode\":\"none\",\"rate\":0}"; verify ;;
  status)  verify ;;
  killpod) "$KUBECTL" -n "$NS" delete pod -l "app=${SERVICE}" --grace-period=0 --force | head -1 ;;
  *) usage ;;
esac
