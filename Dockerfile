# Pin the official 3.12-slim *index* digest (multi-arch). Dependabot's docker
# ecosystem refreshes this weekly. Resolved from hub.docker.com tag metadata
# on 2026-09-25 (tag last pushed 2026-09-19).
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin crm \
    && mkdir -p /app/data \
    && chown crm:crm /app/data \
    && chmod 755 /app/docker-entrypoint.sh

# No USER: the entrypoint must start as root to chown a root-owned bind
# mount, then gosu to APP_UID (or 1000). Pinning USER here would leave sqlite
# read-only on such a mount.

EXPOSE 8000

# stdlib probe, so the image needs no curl. A non-2xx or connection error
# raises and fails the check. Non-web services on this image (staleness-cron)
# disable it in compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# One worker: sqlite likes a single writer. Rate-limit hits live in sqlite so
# a second worker would share buckets, but the rest of the app still serializes
# writes through one connection-at-a-time on this file.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
