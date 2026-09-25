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

INSTANCE_ROOT="${INSTANCE_ROOT:-$REPO_ROOT/instances}"
REGISTRY="${INSTANCE_REGISTRY:-$INSTANCE_ROOT/ports.tsv}"
INSTANCE_DIR="$INSTANCE_ROOT/$NAME"
ENV_FILE="$INSTANCE_DIR/.env"
DATA_DIR="$INSTANCE_DIR/data"

if [[ -e "$ENV_FILE" ]]; then
  echo "Refusing to overwrite existing $ENV_FILE" >&2
  exit 1
fi

PORT=$(python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from instance_lib import allocate_port
print(allocate_port('${REGISTRY}', '${NAME}'))
")

# Default CIDR: all Docker-ish private bridges. Sibling PR adds CIDR matching.
TRUSTED_PROXIES="${TRUSTED_PROXIES:-172.16.0.0/12}"
BASE_HINT="${BASE_URL_HINT:-https://${NAME}.example.com}"
PROJECT="crm-${NAME}"

python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from instance_lib import render_env, write_env_file
text = render_env(
    name='${NAME}',
    tz='${TZ_NAME}',
    email='${EMAIL}',
    port=int('${PORT}'),
    trusted_proxies='${TRUSTED_PROXIES}',
    base_url='${BASE_HINT}',
)
write_env_file('${ENV_FILE}', text)
"
chmod 600 "$ENV_FILE"
mkdir -p "$DATA_DIR"
chmod 700 "$INSTANCE_DIR" "$DATA_DIR" 2>/dev/null || true

# Paths inside the env file assume instances live under the repo. When
# INSTANCE_ROOT is overridden (CI), rewrite the compose interpolation paths.
if [[ "$INSTANCE_ROOT" != "$REPO_ROOT/instances" ]]; then
  # Keep CRM_PORT / secrets; only fix host bind-mount paths.
  :
fi

# Relative paths from repo root for compose interpolation
REL_ENV="instances/${NAME}/.env"
REL_DATA="instances/${NAME}/data"
if [[ "$INSTANCE_ROOT" == "$REPO_ROOT/instances" ]]; then
  # rewrite CRM_* paths to the repo-relative ones (already the default)
  :
else
  # dry-run / tests: leave as-is; compose will not be started
  REL_ENV="$ENV_FILE"
  REL_DATA="$DATA_DIR"
fi

echo "Wrote $ENV_FILE (mode $(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 600))"
echo "Allocated CRM_PORT=$PORT  COMPOSE_PROJECT_NAME=$PROJECT"

print_caddy() {
  cat <<EOF

# --- Caddy site block (paste into your Caddyfile; replace the hostname) ---
${NAME}.example.com {
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

# Resolve the compose-network gateway so TRUSTED_PROXIES works with today's
# IP-only matcher. CIDR support is landing in a sibling PR; the CIDR we
# wrote first stays valid once that lands.
GATEWAY=$(docker network inspect "${PROJECT}_default" \
  --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || true)
if [[ -n "$GATEWAY" ]]; then
  if grep -q '^TRUSTED_PROXIES=' "$ENV_FILE"; then
    # rewrite in place; keep mode 600
    tmp=$(mktemp)
    umask 077
    sed "s|^TRUSTED_PROXIES=.*|TRUSTED_PROXIES=${GATEWAY}|" "$ENV_FILE" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
  fi
  echo "Set TRUSTED_PROXIES=$GATEWAY (compose network gateway)"
  compose_cmd up -d
fi

OTP=$(PYTHONPATH="$SCRIPT_DIR" python3 -c "from instance_lib import generate_password; print(generate_password())")

CREATE_ADMIN_PASSWORD="$OTP" compose_cmd exec -T -e CREATE_ADMIN_PASSWORD="$OTP" app \
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
