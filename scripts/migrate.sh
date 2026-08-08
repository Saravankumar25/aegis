#!/usr/bin/env bash
# Idempotent database migration runner.
# Alembic's `upgrade head` is a no-op when the schema is already current, so re-running
# this script is safe. Runs inside the application image so the migration code and the
# runtime code are always the same revision.
# Usage: IMAGE=<ecr-uri>:<sha> AWS_REGION=... ./migrate.sh
set -Eeuo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
AWS_REGION="${AWS_REGION:?AWS_REGION is required}"
ENV_FILE="${ENV_FILE:-/etc/aegis/app.env}"

REGISTRY="${IMAGE%%/*}"

log() { echo "[migrate] $*"; }

log "logging in to ECR: ${REGISTRY}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

log "pulling ${IMAGE}"
docker pull "${IMAGE}"

RUN_ARGS=(--rm --network host)
if [ -f "${ENV_FILE}" ]; then
  log "using env file ${ENV_FILE}"
  RUN_ARGS+=(--env-file "${ENV_FILE}")
fi

log "current revision:"
docker run "${RUN_ARGS[@]}" "${IMAGE}" alembic current || true

log "applying migrations (alembic upgrade head)"
docker run "${RUN_ARGS[@]}" "${IMAGE}" alembic upgrade head

log "resulting revision:"
docker run "${RUN_ARGS[@]}" "${IMAGE}" alembic current

log "migrations up to date"
