#!/usr/bin/env bash
# Idempotent container deploy on an EC2 Docker host.
# Usage: IMAGE=<ecr-uri>:<sha> AWS_REGION=... ./deploy.sh
set -Eeuo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
AWS_REGION="${AWS_REGION:?AWS_REGION is required}"
CONTAINER_NAME="${CONTAINER_NAME:-aegis}"
HOST_PORT="${HOST_PORT:-80}"
CONTAINER_PORT="${CONTAINER_PORT:-3000}"
ENV_FILE="${ENV_FILE:-/etc/aegis/app.env}"
SECRETS_DIR="${SECRETS_DIR:-/etc/aegis/secrets}"

REGISTRY="${IMAGE%%/*}"

log() { echo "[deploy] $*"; }

log "logging in to ECR: ${REGISTRY}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

log "pulling ${IMAGE}"
docker pull "${IMAGE}"

# Idempotent: remove any container occupying the name, whether running or exited.
if [ -n "$(docker ps -aq --filter "name=^/${CONTAINER_NAME}$")" ]; then
  log "removing existing container ${CONTAINER_NAME}"
  docker rm -f "${CONTAINER_NAME}"
fi

RUN_ARGS=(
  --name "${CONTAINER_NAME}"
  --detach
  --restart unless-stopped
  --publish "${HOST_PORT}:${CONTAINER_PORT}"
  --log-opt max-size=10m
  --log-opt max-file=3
)
if [ -f "${ENV_FILE}" ]; then
  log "using env file ${ENV_FILE}"
  RUN_ARGS+=(--env-file "${ENV_FILE}")
fi

# Credential *files* (currently the Firebase Admin service account) are mounted read-only
# rather than inlined into the env file: firebase-admin wants a path, and a multi-line PEM
# inside an env file is a reliable source of parsing bugs. Mounted only when the directory
# exists, so the dashboard container — which is given neither an env file nor secrets — is
# unaffected.
if [ -d "${SECRETS_DIR}" ]; then
  log "mounting ${SECRETS_DIR} read-only at /run/secrets"
  RUN_ARGS+=(--volume "${SECRETS_DIR}:/run/secrets:ro")
fi

log "starting ${CONTAINER_NAME} from ${IMAGE}"
docker run "${RUN_ARGS[@]}" "${IMAGE}"

# Wait for the container to report healthy (or at least stay up).
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Status}}' "${CONTAINER_NAME}")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER_NAME}")"
  if [ "${status}" != "running" ]; then
    log "container is ${status}; recent logs:"
    docker logs --tail 100 "${CONTAINER_NAME}" || true
    exit 1
  fi
  if [ "${health}" = "healthy" ] || [ "${health}" = "none" ]; then
    log "container running (health=${health})"
    break
  fi
  sleep 5
done

# Best-effort cleanup of images this deploy superseded.
docker image prune -f >/dev/null 2>&1 || true

log "deploy complete: ${CONTAINER_NAME} -> ${HOST_PORT}:${CONTAINER_PORT}"
