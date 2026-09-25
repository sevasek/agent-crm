#!/usr/bin/env bash
# Stop-safe restore of a sqlite snapshot produced by scripts/backup.sh.
# Usage: scripts/restore.sh [--yes] [--no-start] SNAPSHOT [DEST]
#
# Matches the documented restore: compose down, drop WAL/SHM, copy the
# snapshot over the live DB, start. Writers must be stopped; a leftover
# WAL would replay against the restored file and corrupt it.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

YES=0
NO_START=0
SNAPSHOT=""
DEST=""

usage() {
  echo "Usage: scripts/restore.sh [--yes] [--no-start] SNAPSHOT [DEST]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --help|-h) usage ;;
    --) shift; break ;;
    -*) echo "Unknown flag: $1" >&2; usage ;;
    *)
      if [[ -z "$SNAPSHOT" ]]; then
        SNAPSHOT=$1
      elif [[ -z "$DEST" ]]; then
        DEST=$1
      else
        echo "Too many arguments" >&2
        usage
      fi
      shift
      ;;
  esac
done

DEST="${DEST:-${CRM_DB_PATH:-$REPO_ROOT/data/crm.db}}"

if [[ -z "$SNAPSHOT" ]]; then
  usage
fi
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "Snapshot not found: $SNAPSHOT" >&2
  exit 1
fi

if [[ "$YES" -ne 1 ]]; then
  echo "This will stop the stack (if running) and replace $DEST with $SNAPSHOT."
  echo "The current file is copied aside as ${DEST}.pre-restore-<stamp>.db first."
  echo "There is no other undo."
  read -r -p "Type 'restore' to continue: " reply
  if [[ "$reply" != "restore" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

compose_cmd() {
  local files=(-f docker-compose.yml)
  if [[ -f docker-compose.prod.yml ]]; then
    files+=(-f docker-compose.prod.yml)
  fi
  # Instance overlays only when targeting a named project; attaching them
  # to the default stack can down the wrong compose project.
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

WAS_RUNNING=0
if command -v docker >/dev/null 2>&1 && compose_cmd ps --status running --services >/dev/null 2>&1; then
  if compose_cmd ps --status running --services 2>/dev/null | grep -q .; then
    WAS_RUNNING=1
    echo "Stopping compose project…"
    compose_cmd down
  fi
fi

mkdir -p "$(dirname "$DEST")"
rm -f "${DEST}-wal" "${DEST}-shm"
if command -v python3 >/dev/null 2>&1 && \
   PYTHONPATH="$REPO_ROOT" python3 -c "from app.backup import restore_snapshot" >/dev/null 2>&1; then
  PYTHONPATH="$REPO_ROOT" python3 -m app.backup --restore "$SNAPSHOT" --restore-dest "$DEST"
else
  cp "$SNAPSHOT" "$DEST"
  chmod 600 "$DEST"
  rm -f "${DEST}-wal" "${DEST}-shm"
fi
chmod 600 "$DEST" || true
echo "Restored $SNAPSHOT -> $DEST"

if [[ "$WAS_RUNNING" -eq 1 && "$NO_START" -ne 1 ]]; then
  echo "Starting compose project…"
  compose_cmd up -d
  echo "Check: docker compose ps (healthy) and that you can log in."
fi
