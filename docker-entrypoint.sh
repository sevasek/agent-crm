#!/bin/sh
# A bind-mounted ./data is often root-owned, which leaves sqlite read-only for
# a non-root user. Start as root, fix ownership and mode bits, then drop to
# APP_UID/APP_GID. The long-running process is never root.
#
# umask 077 so new files in ./data are 0600 (not world-readable). A leftover
# chmod 444 on crm.db* used to crash-loop ("attempt to write a readonly
# database"); we repair that here and fail clearly if the path is still
# unwritable after dropping privileges.
set -eu

umask 077

DATA_DIR="/app/data"
DB_FILE="${DATA_DIR}/crm.db"

if [ "$(id -u)" = "0" ]; then
  uid="${APP_UID:-1000}"
  gid="${APP_GID:-1000}"
  mkdir -p "$DATA_DIR"
  chown -R "${uid}:${gid}" "$DATA_DIR"
  # Directories 0700, files 0600 — including WAL/SHM sidecars and backups.
  if command -v find >/dev/null 2>&1; then
    find "$DATA_DIR" -type d -exec chmod 700 {} +
    find "$DATA_DIR" -type f -exec chmod 600 {} +
  else
    chmod 700 "$DATA_DIR"
    for f in "$DATA_DIR"/*; do
      [ -e "$f" ] || continue
      if [ -d "$f" ]; then
        chmod 700 "$f"
      else
        chmod 600 "$f"
      fi
    done
  fi
  exec gosu "${uid}:${gid}" "$0" "$@"
fi

if [ ! -d "$DATA_DIR" ]; then
  echo "ERROR: ${DATA_DIR} does not exist or is not a directory." >&2
  exit 1
fi

if [ ! -w "$DATA_DIR" ] || ! touch "${DATA_DIR}/.write-test" 2>/dev/null; then
  echo "ERROR: ${DATA_DIR} is not writable by uid $(id -u) gid $(id -g)." >&2
  echo "The entrypoint could not make the volume writable. Check the bind-mount owner, mode bits, and ACLs." >&2
  exit 1
fi
rm -f "${DATA_DIR}/.write-test"

for f in "$DB_FILE" "${DB_FILE}-wal" "${DB_FILE}-shm"; do
  if [ -e "$f" ] && [ ! -w "$f" ]; then
    echo "ERROR: ${f} is not writable by uid $(id -u) (attempt to write a readonly database)." >&2
    echo "The entrypoint repairs 0444 leftovers when it starts as root. If you still see this, chmod u+w the file on the host and restart." >&2
    exit 1
  fi
done

exec "$@"
