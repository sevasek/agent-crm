#!/bin/sh
# Build the image from a dirty context (.env + data/*.db) and fail if those
# files were copied in. Also confirm runtime files the container needs.
# CI only: it writes a fake .env and data/crm.db into the build context.
set -eu

if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
  echo "Refusing to plant a fake .env outside GitHub Actions." >&2
  exit 1
fi

IMAGE="${1:-agent-crm:ci}"

echo "SECRET_KEY=must-not-be-in-the-image" > .env
mkdir -p data
echo "not-a-real-database" > data/crm.db

docker build -t "$IMAGE" .

docker run --rm --entrypoint sh "$IMAGE" -c '
  set -eu
  if [ -e /app/.env ]; then
    echo "ERROR: /app/.env is in the image" >&2
    exit 1
  fi
  if [ -e /app/.git ]; then
    echo "ERROR: /app/.git is in the image" >&2
    exit 1
  fi
  if ls /app/data/*.db >/dev/null 2>&1; then
    echo "ERROR: /app/data/*.db is in the image" >&2
    ls -la /app/data >&2
    exit 1
  fi
  test -f /app/docker-entrypoint.sh
  test -f /app/scripts/create_admin.py
  test -f /app/scripts/seed_services.py
  test -f /app/scripts/staleness_cron.py
  test -d /app/app
  test -f /app/app/main.py
  echo "image hygiene ok"
'
