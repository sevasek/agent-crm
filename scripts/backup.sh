#!/usr/bin/env bash
# Online backup of data/crm.db. Safe under load (sqlite backup API).
# Usage: scripts/backup.sh
#
# Writes to $BACKUP_DIR (default ./backups, not ./data). Files are 0600.
# Prunes snapshots older than $BACKUP_KEEP_DAYS (default 14).
#
# Off-host (nothing leaves the machine until you set one of these):
#   BACKUP_REMOTE_CMD   command; the backup path is appended as $1
#                       (or substitute {} if present). Example:
#                       BACKUP_REMOTE_CMD='restic -r s3:s3.amazonaws.com/bucket backup'
#   BACKUP_RCLONE_DEST  rclone remote path, e.g. remote:crm-backups
#   BACKUP_S3_URI       s3://bucket/prefix  (uses aws s3 cp)
# Do not put cloud credentials in CI; these are operator-only.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
DB_PATH="${CRM_DB_PATH:-$REPO_ROOT/data/crm.db}"
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
DEST="${BACKUP_DEST:-$BACKUP_DIR/crm-${STAMP}.db}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

log() { printf '%s\n' "$*" >&2; }

compose_cmd() {
  # Honour an already-running project; extra -f files are fine for exec.
  local files=(-f docker-compose.yml)
  if [[ -f docker-compose.prod.yml ]]; then
    files+=(-f docker-compose.prod.yml)
  fi
  # Instance overlays only when targeting a named project.
  if [[ -n "${COMPOSE_PROJECT_NAME:-}${CRM_ENV_FILE:-}" ]]; then
    if [[ -f docker-compose.port.yml ]]; then
      files+=(-f docker-compose.port.yml)
    fi
    if [[ -f docker-compose.instance.yml ]]; then
      files+=(-f docker-compose.instance.yml)
    fi
  fi
  local args=(docker compose)
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    args+=(--project-name "$COMPOSE_PROJECT_NAME")
  fi
  if [[ -n "${CRM_ENV_FILE:-}" && -f "${CRM_ENV_FILE}" ]]; then
    args+=(--env-file "$CRM_ENV_FILE")
  fi
  "${args[@]}" "${files[@]}" "$@"
}

app_container_running() {
  command -v docker >/dev/null 2>&1 || return 1
  compose_cmd ps --status running --services 2>/dev/null | grep -qx app
}

backup_via_stdlib() {
  local src="$1" dest="$2"
  python3 - "$src" "$dest" <<'PY'
import os, sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
try:
    os.chmod(os.path.dirname(dest) or ".", 0o700)
except OSError:
    pass
fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
src_conn = sqlite3.connect(src)
dest_conn = sqlite3.connect(dest)
try:
    src_conn.backup(dest_conn)
finally:
    dest_conn.close()
    src_conn.close()
os.chmod(dest, 0o600)
check = sqlite3.connect(dest)
ok = check.execute("PRAGMA integrity_check").fetchone()[0]
check.close()
if ok != "ok":
    os.remove(dest)
    raise SystemExit("integrity_check: " + ok)
print(dest)
PY
}

backup_via_module() {
  local src="$1" dest="$2"
  PYTHONPATH="$REPO_ROOT" python3 -m app.backup --source "$src" --dest "$dest" --keep-days "$KEEP_DAYS"
}

try_local_python() {
  local src="$1" dest="$2"
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  if [[ ! -f "$src" ]]; then
    return 1
  fi
  if PYTHONPATH="$REPO_ROOT" python3 -c "from app.backup import online_backup" >/dev/null 2>&1; then
    backup_via_module "$src" "$dest"
  else
    backup_via_stdlib "$src" "$dest"
    PYTHONPATH="$REPO_ROOT" python3 -m app.backup --source "$src" --dest-dir "$BACKUP_DIR" --keep-days "$KEEP_DAYS" >/dev/null 2>&1 || true
    # prune even if -m is unavailable
    find "$BACKUP_DIR" -type f -name 'crm-*.db' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
  fi
}

backup_via_docker() {
  local dest="$1"
  local tmp="/tmp/crm-backup-$$.db"
  log "Backing up via docker compose exec (app container)"
  compose_cmd exec -T app python -c "
import os, sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
s = sqlite3.connect(src)
d = sqlite3.connect(dest)
s.backup(d)
d.close(); s.close()
c = sqlite3.connect(dest)
ok = c.execute('PRAGMA integrity_check').fetchone()[0]
c.close()
if ok != 'ok':
    os.remove(dest)
    raise SystemExit('integrity_check: ' + ok)
os.chmod(dest, 0o600)
print(dest)
" data/crm.db "$tmp"
  compose_cmd cp "app:${tmp}" "$dest"
  compose_cmd exec -T app rm -f "$tmp" || true
  chmod 600 "$dest"
  find "$BACKUP_DIR" -type f -name 'crm-*.db' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
}

if try_local_python "$DB_PATH" "$DEST"; then
  :
elif app_container_running; then
  backup_via_docker "$DEST"
else
  log "No live database at $DB_PATH and docker compose service 'app' is not running."
  exit 1
fi

if [[ ! -f "$DEST" ]]; then
  log "Backup was not written to $DEST"
  exit 1
fi
chmod 600 "$DEST" || true
log "Wrote $DEST (mode $(stat -c '%a' "$DEST" 2>/dev/null || echo 600))"

run_remote_hook() {
  local dest="$1"
  if [[ -n "${BACKUP_REMOTE_CMD:-}" ]]; then
    if [[ "$BACKUP_REMOTE_CMD" == *"{}"* ]]; then
      local cmd="${BACKUP_REMOTE_CMD//\{\}/$dest}"
      # Operator-supplied hook; they own the credentials.
      # shellcheck disable=SC2086
      eval $cmd
    else
      # shellcheck disable=SC2086
      eval $BACKUP_REMOTE_CMD "$(printf '%q' "$dest")"
    fi
    return
  fi
  if [[ -n "${BACKUP_RCLONE_DEST:-}" ]]; then
    command -v rclone >/dev/null 2>&1 || { log "rclone not installed"; return 1; }
    rclone copyto "$dest" "${BACKUP_RCLONE_DEST%/}/$(basename "$dest")"
    return
  fi
  if [[ -n "${BACKUP_S3_URI:-}" ]]; then
    command -v aws >/dev/null 2>&1 || { log "aws CLI not installed"; return 1; }
    aws s3 cp "$dest" "${BACKUP_S3_URI%/}/$(basename "$dest")"
    return
  fi
  return 0
}

if [[ -n "${BACKUP_REMOTE_CMD:-}${BACKUP_RCLONE_DEST:-}${BACKUP_S3_URI:-}" ]]; then
  run_remote_hook "$DEST"
  log "Off-host copy finished"
else
  log "Off-host copy skipped (set BACKUP_REMOTE_CMD, BACKUP_RCLONE_DEST, or BACKUP_S3_URI)."
fi
