#!/bin/sh
# A bind-mounted ./data is often root-owned, which leaves sqlite read-only for
# a non-root user. Start as root, repair ownership and mode bits only when the
# volume is traversable and not already owned by APP_UID, then drop to
# APP_UID/APP_GID. The long-running process is never root.
#
# Production compose drops CAP_DAC_OVERRIDE. After the first successful boot
# /app/data is APP_UID mode 0700; blindly chown -R / chmod 700 on every start
# crash-loops the container (issue 70). Skip repair when ownership is already
# correct. If root cannot even stat/ls the volume, print a host chown command
# instead of retrying a repair that cannot succeed.
#
# umask 077 so new files in ./data are 0600 (not world-readable). A leftover
# chmod 444 on crm.db* used to crash-loop ("attempt to write a readonly
# database"); we repair that on first-boot when we can traverse, and fail
# clearly if the path is still unwritable after dropping privileges.
set -eu

umask 077

. "$(dirname "$0")/docker-entrypoint-lib.sh"

DATA_DIR="${DATA_DIR:-/app/data}"
DB_FILE="${DATA_DIR}/crm.db"

if [ "$(id -u)" = "0" ]; then
  uid="${APP_UID:-1000}"
  gid="${APP_GID:-1000}"
  prepare_data_volume_as_root "$DATA_DIR" "$uid" "$gid"
  # Pre-migrate backups go to /app/backups (or BACKUP_DIR). Prod compose
  # bind-mounts that path; a read-only root cannot create it. Only chown a
  # directory that already exists so a container started without the mount
  # (the CI `id` probe, local dev) still drops privileges. A 0700 directory
  # already owned by APP_UID is left alone — set APP_UID/APP_GID to the
  # host owner (1004 on the sevasek VPS) instead of the image default 1000.
  backup_dir="${BACKUP_DIR:-/app/backups}"
  if [ -d "$backup_dir" ]; then
    prepare_data_volume_as_root "$backup_dir" "$uid" "$gid"
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
  echo "On the host: chown -R $(id -u):$(id -g) ./data && chmod 700 ./data" >&2
  exit 1
fi
rm -f "${DATA_DIR}/.write-test"

for f in "$DB_FILE" "${DB_FILE}-wal" "${DB_FILE}-shm"; do
  if [ -e "$f" ] && [ ! -w "$f" ]; then
    echo "ERROR: ${f} is not writable by uid $(id -u) (attempt to write a readonly database)." >&2
    echo "The entrypoint repairs 0444 leftovers when it starts as root and can traverse the volume. If you still see this, chmod u+w the file on the host and restart." >&2
    exit 1
  fi
done

exec "$@"
