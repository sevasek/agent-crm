#!/usr/bin/env bash
# Provision another CRM instance on this host.
# Usage: scripts/new-instance.sh [--dry-run] NAME TZ EMAIL
#
# Allocates a host port, writes instances/$NAME/.env (mode 600) with fresh
# secrets, starts compose (unless --dry-run), waits for /health, creates
# the admin via create_admin.py, and prints a Caddy site block.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

DRY_RUN=0
NAME=""
TZ_NAME=""
EMAIL=""

usage() {
  echo "Usage: scripts/new-instance.sh [--dry-run] NAME TZ EMAIL" >&2
  echo "  NAME   lowercase instance id (acme, clinic-west)" >&2
  echo "  TZ     IANA timezone (Australia/Sydney)" >&2
  echo "  EMAIL  first admin email" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage ;;
    -*) echo "Unknown flag: $1" >&2; usage ;;
    *)
      if [[ -z "$NAME" ]]; then NAME=$1
      elif [[ -z "$TZ_NAME" ]]; then TZ_NAME=$1
      elif [[ -z "$EMAIL" ]]; then EMAIL=$1
      else echo "Too many arguments" >&2; usage
      fi
      shift
      ;;
  esac
done

[[ -n "$NAME" && -n "$TZ_NAME" && -n "$EMAIL" ]] || usage

if [[ ! "$NAME" =~ ^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$ ]]; then
  echo "NAME must be lowercase alphanumeric plus internal hyphens, e.g. acme or clinic-west" >&2
  exit 1
fi

INSTANCE_ROOT="${INSTANCE_ROOT:-$REPO_ROOT/instances}"
REGISTRY="${INSTANCE_REGISTRY:-$INSTANCE_ROOT/ports.tsv}"
INSTANCE_DIR="$INSTANCE_ROOT/$NAME"
ENV_FILE="$INSTANCE_DIR/.env"
DATA_DIR="$INSTANCE_DIR/data"

if [[ -e "$ENV_FILE" ]]; then
  echo "Refusing to overwrite existing $ENV_FILE" >&2
  exit 1
fi

export INSTANCE_SCRIPT_DIR="$SCRIPT_DIR"
export INSTANCE_REGISTRY="$REGISTRY"
export INSTANCE_ROOT
export INSTANCE_NAME="$NAME"
export INSTANCE_TZ="$TZ_NAME"
export INSTANCE_EMAIL="$EMAIL"

PORT=$(python3 -c '
import os, sys
sys.path.insert(0, os.environ["INSTANCE_SCRIPT_DIR"])
from instance_lib import allocate_port
print(allocate_port(os.environ["INSTANCE_REGISTRY"], os.environ["INSTANCE_NAME"]))
')

# Default CIDR: all Docker-ish private bridges. CIDR matching is shipped.
TRUSTED_PROXIES="${TRUSTED_PROXIES:-172.16.0.0/12}"
BASE_HINT="${BASE_URL_HINT:-https://${NAME}.example.com}"
PROJECT="crm-${NAME}"

export INSTANCE_PORT="$PORT"
export INSTANCE_TRUSTED_PROXIES="$TRUSTED_PROXIES"
export INSTANCE_BASE_URL="$BASE_HINT"
export INSTANCE_ENV_FILE="$ENV_FILE"
export INSTANCE_APP_UID="${APP_UID:-1000}"
export INSTANCE_APP_GID="${APP_GID:-1000}"

python3 -c '
import os, sys
sys.path.insert(0, os.environ["INSTANCE_SCRIPT_DIR"])
from instance_lib import (
    assert_unique_secret_key, load_secret_key, render_env, write_env_file,
)
text = render_env(
    name=os.environ["INSTANCE_NAME"],
    tz=os.environ["INSTANCE_TZ"],
    email=os.environ["INSTANCE_EMAIL"],
    port=int(os.environ["INSTANCE_PORT"]),
    trusted_proxies=os.environ["INSTANCE_TRUSTED_PROXIES"],
    base_url=os.environ["INSTANCE_BASE_URL"],
    app_uid=os.environ["INSTANCE_APP_UID"],
    app_gid=os.environ["INSTANCE_APP_GID"],
)
secret = load_secret_key(text)
assert_unique_secret_key(
    os.environ["INSTANCE_ROOT"], os.environ["INSTANCE_NAME"], secret or "",
)
write_env_file(os.environ["INSTANCE_ENV_FILE"], text)
'
chmod 600 "$ENV_FILE"
mkdir -p "$DATA_DIR"
chmod 700 "$INSTANCE_DIR" "$DATA_DIR" 2>/dev/null || true
# Own the bind-mount as APP_UID *before* the container starts. Production
# compose drops CAP_DAC_OVERRIDE, so the entrypoint cannot chown a 0700
# directory it does not already own (issue 70). The same ids are written
# into the instance .env so the process drops to the owner of the files.
app_uid="${APP_UID:-1000}"
app_gid="${APP_GID:-1000}"
if ! chown "${app_uid}:${app_gid}" "$DATA_DIR"; then
  echo "ERROR: could not chown $DATA_DIR to ${app_uid}:${app_gid}." >&2
  echo "Run this script as root, or export APP_UID and APP_GID to your own ids" >&2
  echo "(they are written into the instance .env and must match this directory)." >&2
  echo "  chown -R ${app_uid}:${app_gid} $DATA_DIR && chmod 700 $DATA_DIR" >&2
  exit 1
fi

# Relative paths from repo root for compose interpolation.
# A volume source with no leading ./ or / is a named volume. Compose then
# errors with "undefined volume instances/<name>/data" and the instance
# never starts. Keep the ./ prefix.
REL_ENV="instances/${NAME}/.env"
REL_DATA="./instances/${NAME}/data"
if [[ "$INSTANCE_ROOT" != "$REPO_ROOT/instances" ]]; then
  REL_ENV="$ENV_FILE"
  REL_DATA="$DATA_DIR"
fi

echo "Wrote $ENV_FILE (mode $(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 600))"
echo "Allocated CRM_PORT=$PORT  COMPOSE_PROJECT_NAME=$PROJECT"

print_caddy() {
  # BASE_URL_HINT is the public name. Fall back to <name>.example.com only
  # when the operator did not set one.
  local host="${BASE_HINT#https://}"
  host="${host#http://}"
  host="${host%%/*}"
  if [[ -z "$host" ]]; then
    host="${NAME}.example.com"
  fi
  cat <<EOF

# --- Caddy site block (paste into your Caddyfile) ---
${host} {
	reverse_proxy 127.0.0.1:${PORT}
}
# -------------------------------------------------------------------------
EOF
}

compose_cmd() {
  local files=(
    -f docker-compose.yml
    -f docker-compose.prod.yml
    -f docker-compose.port.yml
    -f docker-compose.instance.yml
  )
  docker compose \
    --project-name "$PROJECT" \
    --env-file "$ENV_FILE" \
    "${files[@]}" \
    "$@"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry-run: not starting Docker."
  echo "Would run: docker compose --project-name $PROJECT --env-file $ENV_FILE -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.port.yml -f docker-compose.instance.yml up -d"
  print_caddy
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not available. Re-run without --dry-run on the host, or use --dry-run." >&2
  print_caddy
  exit 1
fi

echo "Starting $PROJECT on 127.0.0.1:${PORT}…"
CRM_PORT="$PORT" CRM_DATA_DIR="$REL_DATA" CRM_ENV_FILE="$REL_ENV" \
  COMPOSE_PROJECT_NAME="$PROJECT" \
  compose_cmd up -d

echo "Waiting for http://127.0.0.1:${PORT}/health …"
ok=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [[ "$ok" -ne 1 ]]; then
  echo "Timed out waiting for /health on port $PORT" >&2
  compose_cmd logs --tail 80 || true
  exit 1
fi

OTP=$(INSTANCE_SCRIPT_DIR="$SCRIPT_DIR" python3 -c '
import os, sys
sys.path.insert(0, os.environ["INSTANCE_SCRIPT_DIR"])
from instance_lib import generate_password
print(generate_password())
')

# exec does not run the entrypoint, so it stays root. Root without
# CAP_DAC_OVERRIDE cannot write the 0700 data volume. Run as APP_UID.
CREATE_ADMIN_PASSWORD="$OTP" compose_cmd exec -T \
  -u "${app_uid}:${app_gid}" \
  -e CREATE_ADMIN_PASSWORD="$OTP" app \
  python scripts/create_admin.py "$EMAIL" "Admin"

cat <<EOF

Instance ${NAME} is up.
  URL (local):  http://127.0.0.1:${PORT}
  BASE_URL:     ${BASE_HINT}   (edit $ENV_FILE)
  Admin email:  ${EMAIL}
  One-time password (shown once): ${OTP}

Log in, then change the password. BOOTSTRAP_ADMIN_PASSWORD was not written
to the lasting .env.
EOF
print_caddy
