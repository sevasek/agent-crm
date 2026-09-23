FROM python:3.12-slim

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

ENTRYPOINT ["/app/docker-entrypoint.sh"]
# One worker: sqlite likes a single writer. Rate-limit hits live in sqlite so
# a second worker would share buckets, but the rest of the app still serializes
# writes through one connection-at-a-time on this file.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
