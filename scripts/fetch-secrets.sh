#!/usr/bin/env bash
# Pull runtime credentials from AWS Secrets Manager onto the host, using the instance profile.
#
# Runs on the EC2 host immediately before `deploy.sh`, so a container always starts against the
# current value of a secret and rotating one is a redeploy rather than a hand-edit over SSH.
#
# Nothing here is ever echoed. `set -x` is deliberately never enabled, and every value moves
# from the API straight into a 0600 file owned by the deploy user.
#
#   Usage:  AWS_REGION=ap-south-1 bash fetch-secrets.sh
set -Eeuo pipefail

AWS_REGION="${AWS_REGION:?AWS_REGION is required}"
ENV_FILE="${ENV_FILE:-/etc/aegis/app.env}"
SECRETS_DIR="${SECRETS_DIR:-/etc/aegis/secrets}"
LLM_SECRET="${LLM_SECRET:-aegis/llm-keys}"
FIREBASE_SECRET="${FIREBASE_SECRET:-aegis/firebase-service-account}"

log() { echo "[secrets] $*"; }

mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

# --- Firebase Admin service account --------------------------------------------------------
# Written as a file rather than an env var because firebase-admin loads a credential path, and
# because a multi-line PEM inside an env file is a reliable source of parsing bugs.
log "fetching ${FIREBASE_SECRET}"
umask 077
aws secretsmanager get-secret-value \
  --secret-id "${FIREBASE_SECRET}" \
  --region "${AWS_REGION}" \
  --query SecretString \
  --output text > "${SECRETS_DIR}/firebase-service-account.json"

# A truncated or error-shaped response would otherwise be written happily and only surface as a
# confusing auth failure at request time.
if ! python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('type')=='service_account' and d.get('private_key') else 1)" \
     "${SECRETS_DIR}/firebase-service-account.json"; then
  log "ERROR: fetched Firebase secret is not a usable service account"
  rm -f "${SECRETS_DIR}/firebase-service-account.json"
  exit 1
fi
log "firebase service account written"

# --- LLM provider keys ---------------------------------------------------------------------
# Merged into the env file rather than replacing it: the file also holds host-generated values
# (database password, JWT secret, webhook token) that exist nowhere else and must survive.
log "fetching ${LLM_SECRET}"
LLM_JSON="$(aws secretsmanager get-secret-value \
  --secret-id "${LLM_SECRET}" \
  --region "${AWS_REGION}" \
  --query SecretString --output text)"

# The merge runs from a temp script so the JSON can arrive on stdin. It is deliberately not
# passed as an argument: every argv is world-readable via `ps` for the life of the process.
MERGE_PY="$(mktemp)"
trap 'rm -f "${MERGE_PY}"' EXIT

cat > "${MERGE_PY}" <<'PY'
import json, pathlib, sys

env_path = pathlib.Path(sys.argv[1])
incoming = json.loads(sys.stdin.read())

# The container sees the host's SECRETS_DIR mounted at /run/secrets (see deploy.sh).
incoming["FIREBASE_SERVICE_ACCOUNT_FILE"] = "/run/secrets/firebase-service-account.json"
incoming = {k: v for k, v in incoming.items() if v}

lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
kept = [ln for ln in lines if not any(ln.startswith(f"{k}=") for k in incoming)]
merged = kept + [f"{k}={v}" for k, v in sorted(incoming.items())]
env_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
print(f"[secrets] merged {len(incoming)} value(s) into {env_path}")
PY

printf '%s' "${LLM_JSON}" | python3 "${MERGE_PY}" "${ENV_FILE}"

# Two different readers, so two different owners — this is not incidental.
#
#   app.env       is read by the docker CLI in the *deploy user's* process before the daemon is
#                 contacted, so it must be readable by ec2-user.
#   SECRETS_DIR   is read by the application *inside* the container, which runs as the
#                 unprivileged uid baked into the image (10001), not as ec2-user.
#
# Getting this wrong is silent at deploy time and only fails on the first request that needs
# the credential: the container started healthy, and `POST /auth/session` returned a 500 with
# `PermissionError: /run/secrets/firebase-service-account.json` behind it.
#
# Numeric ownership is used because uid 10001 has no passwd entry on the host. Nothing is
# conceded by taking it away from ec2-user, which is in the docker group and therefore already
# root-equivalent on this box.
APP_UID="${APP_UID:-10001}"
chown -R "${APP_UID}:${APP_UID}" "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"
chmod 600 "${SECRETS_DIR}/firebase-service-account.json"
chmod 600 "${ENV_FILE}"

log "runtime secrets in place"
