#!/usr/bin/env bash
# One-time host preparation for an Aegis EC2 target (Amazon Linux 2).
#
# Run as EC2 user-data at launch, or by hand on an existing instance. Idempotent: every step
# checks for its own result first, so re-running after a partial failure completes the rest
# rather than duplicating containers or regenerating secrets that are already in use.
#
# What it does NOT do: pull or run the application image. That is `deploy.sh`, driven by the
# pipeline, so that host preparation and application rollout stay independently re-runnable.
#
#   Usage:  ENVIRONMENT=production bash bootstrap-ec2.sh
#           ENVIRONMENT=staging    bash bootstrap-ec2.sh
#
# The instance must carry an instance profile granting ECR pull (AmazonEC2ContainerRegistryReadOnly).
# Without it `deploy.sh` cannot authenticate to ECR and every deploy fails at `docker pull`.
set -Eeuo pipefail

ENVIRONMENT="${ENVIRONMENT:?ENVIRONMENT is required (staging|production)}"
APP_USER="${APP_USER:-ec2-user}"
# Firebase supplies identity; this id is the token's required audience, so it is a security
# control and not a label (ESD §8). Auth refuses to initialise without it.
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-}"
# Role allowlists, evaluated on every sign-in. Any authenticated account absent from both is
# provisioned `viewer` and cannot approve a remediation — the deliberate fail-closed default
# (CLAUDE.md §12). Left empty here so an operator states who the approvers are, rather than
# a repository file naming them.
AEGIS_ADMIN_EMAILS="${AEGIS_ADMIN_EMAILS:-}"
AEGIS_APPROVER_EMAILS="${AEGIS_APPROVER_EMAILS:-}"
ENV_FILE="${ENV_FILE:-/etc/aegis/app.env}"
# Docker's default bridge gateway. The app runs in a bridge-networked container and the data
# stores publish to the host, so the gateway address reaches them from either network mode —
# unlike `localhost`, which inside a bridge container means the container itself.
GW="${GW:-172.17.0.1}"

log() { echo "[bootstrap] $*"; }

# --- docker -------------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing docker"
  yum update -y
  yum install -y unzip
  amazon-linux-extras install docker -y
fi
systemctl enable --now docker
usermod -a -G docker "${APP_USER}"

# --- aws cli v2 ---------------------------------------------------------------------------
# AL2 ships v1. v2 is installed for parity with CI, so a command that works in the pipeline
# behaves identically when an operator runs it on the box.
if ! aws --version 2>&1 | grep -q "aws-cli/2"; then
  log "installing aws cli v2"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  ( cd /tmp && unzip -q -o awscliv2.zip && ./aws/install --update )
  rm -rf /tmp/awscliv2.zip /tmp/aws
fi

# --- data plane ---------------------------------------------------------------------------
# Postgres is the single source of truth (CLAUDE.md §5); Redis is cache and rate limiting only
# and every path through it fails open, so losing it degrades rather than stops the system.
docker volume create aegis-pgdata >/dev/null

if [ -z "$(docker ps -aq --filter 'name=^/aegis-postgres$')" ]; then
  log "starting postgres (pgvector)"
  PGPASS="$(openssl rand -hex 16)"
  docker run -d --name aegis-postgres --restart unless-stopped \
    -e POSTGRES_USER=aegis \
    -e POSTGRES_PASSWORD="${PGPASS}" \
    -e POSTGRES_DB=aegis \
    -p 5432:5432 \
    -v aegis-pgdata:/var/lib/postgresql/data \
    pgvector/pgvector:pg16 >/dev/null
else
  log "postgres already present"
  # The generated password lives only in the env file after first run. Reusing it is the point:
  # regenerating one here would not change the password already inside the running database,
  # and the app would silently lose access to its own data.
  PGPASS="$(grep -E '^POSTGRES_PASSWORD=' "${ENV_FILE}" | cut -d= -f2-)"
fi

if [ -z "$(docker ps -aq --filter 'name=^/aegis-redis$')" ]; then
  log "starting redis"
  docker run -d --name aegis-redis --restart unless-stopped \
    -p 6379:6379 redis:7-alpine >/dev/null
else
  log "redis already present"
fi

# --- application environment ---------------------------------------------------------------
mkdir -p "$(dirname "${ENV_FILE}")"

if [ ! -f "${ENV_FILE}" ]; then
  log "writing ${ENV_FILE}"
  # Generated on the host and never transmitted: nothing outside this machine needs to know
  # them, so there is no reason for them to exist anywhere they could be captured.
  cat > "${ENV_FILE}" <<ENV
LOG_LEVEL=info
ENVIRONMENT=${ENVIRONMENT}
POSTGRES_USER=aegis
POSTGRES_PASSWORD=${PGPASS}
POSTGRES_DB=aegis
POSTGRES_HOST=${GW}
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://aegis:${PGPASS}@${GW}:5432/aegis
REDIS_URL=redis://${GW}:6379/0
JWT_SECRET=$(openssl rand -hex 32)
INGEST_WEBHOOK_TOKEN=$(openssl rand -hex 24)
FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}
AEGIS_ADMIN_EMAILS=${AEGIS_ADMIN_EMAILS}
AEGIS_APPROVER_EMAILS=${AEGIS_APPROVER_EMAILS}
CORS_ORIGINS=http://$(curl -fsS --max-time 5 http://169.254.169.254/latest/meta-data/public-ipv4 || echo localhost):3000
ENV
else
  log "${ENV_FILE} exists; leaving values untouched"
fi

# Owned by the deploy user, not root. `docker run --env-file` is read by the docker CLI in the
# caller's own process before the daemon is contacted, so a root-owned 0600 file makes every
# deploy fail with "permission denied" even though the daemon itself runs as root. Membership
# of the docker group is already root-equivalent, so this concedes nothing.
chown "${APP_USER}:${APP_USER}" "$(dirname "${ENV_FILE}")" "${ENV_FILE}"
chmod 700 "$(dirname "${ENV_FILE}")"
chmod 600 "${ENV_FILE}"

# --- readiness ------------------------------------------------------------------------------
log "waiting for postgres to accept connections"
for _ in $(seq 1 60); do
  if docker exec aegis-postgres pg_isready -U aegis -d aegis >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
# `alembic upgrade head` creates tables, not extensions; pgvector must exist before the first
# migration that declares a vector column.
docker exec aegis-postgres psql -U aegis -d aegis -c 'CREATE EXTENSION IF NOT EXISTS vector;' >/dev/null

touch /var/lib/cloud/aegis-ready
log "bootstrap complete for ENVIRONMENT=${ENVIRONMENT}"
