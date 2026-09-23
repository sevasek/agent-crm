#!/bin/sh
# A bind-mounted ./data is often root-owned, which leaves sqlite read-only for
# a non-root user. Start as root, fix ownership, then drop to APP_UID/APP_GID.
# The long-running process is never root.
set -eu

if [ "$(id -u)" = "0" ]; then
  uid="${APP_UID:-1000}"
  gid="${APP_GID:-1000}"
  mkdir -p /app/data
  chown -R "${uid}:${gid}" /app/data
  exec gosu "${uid}:${gid}" "$@"
fi

exec "$@"
