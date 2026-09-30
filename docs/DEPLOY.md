# Production reverse proxy and TLS

The production compose file binds the app to `127.0.0.1:${CRM_PORT:-8000}` on
the host. Put a TLS-terminating reverse proxy in front of that loopback port.
This file is a minimal Caddy (auto-TLS) example and an nginx equivalent.

Forward **everything** to the app, including `/mcp`, `/oauth/*`, and
`/.well-known/*`. Those routes use their own keys (or the OAuth flow), so
**do not** add HTTP basic auth, an extra login, or an IP allowlist at the
proxy. Hosted MCP connectors have no stable CIDR; allowlisting `/mcp` will
break them.

Set `BASE_URL=https://your.domain` and `SECURE_COOKIES=true` in `.env`.
`SECRET_KEY` must be a unique random value per instance — not the
`.env.example` default, and not copied from another tenant's `.env`.
`scripts/new-instance.sh` generates a fresh key and refuses to start if
that key already belongs to a sibling instance. Session cookies are also
bound to a per-database `install_id`, so a copied key alone is not enough
to replay a session onto another sqlite file.

`/docs`, `/redoc`, and `/openapi.json` are hidden when `SECURE_COOKIES=true`,
`BASE_URL` is https, or any `CRM_*_KEY` is set. Force the hide with
`DISABLE_DOCS=true`; `DISABLE_DOCS=false` is an explicit show (including
on https).

Production compose drops all Linux capabilities except the four the
entrypoint needs to chown a still-root-owned `./data` and `gosu` to
`APP_UID` (`CHOWN`, `FOWNER`, `SETUID`, `SETGID`), sets
`no-new-privileges`, and runs a read-only root filesystem with `tmpfs`
on `/tmp`. The sqlite volume stays writable. `CAP_DAC_OVERRIDE` is not
granted.

The entrypoint only repairs ownership and mode of `/app/data` when it
can traverse that directory and it is not already owned by `APP_UID`.
After the first successful boot the volume is `APP_UID` mode `0700`;
later restarts skip the repair and drop privileges, so
`docker compose restart`, crash recovery (`restart: unless-stopped`),
and host reboots keep working.

If the bind-mount is already `0700` and owned by a *different* uid,
root cannot traverse it without `CAP_DAC_OVERRIDE`. The entrypoint
exits with an error instead of crash-looping on `chown: Permission
denied`. Fix the host directory, then start the container:

```bash
chown -R 1000:1000 ./data   # or whatever APP_UID:APP_GID you set
chmod 700 ./data
```

`scripts/new-instance.sh` chowns the instance data dir to
`APP_UID`/`APP_GID` (default 1000:1000) on the host before the
container starts, so first boot does not need `CAP_DAC_OVERRIDE`.

**Never set a cookie `Domain=` for the session cookie**, and never configure
the proxy to rewrite it in. The session cookie is host-only by default
(scoped to the exact hostname), which is what stops a session or CSRF token
from one customer's subdomain being replayable against a sibling subdomain
on a multi-tenant host. Widening it to a parent domain removes that
protection silently.

When `SECURE_COOKIES=true` (or https `BASE_URL`), the cookie is named
`__Host-session`: Secure, Path=/, no Domain. Browsers reject a `__Host-`
cookie that breaks any of those, so a proxy must not rename it, drop
Secure, change Path, or add Domain. Local http keeps the name `session`
so a browser and the TestClient can still store it.

The app sends a Content-Security-Policy that allows same-origin scripts
and styles only (vendored Pico and htmx under `/static/vendor/`). Do not
add a CDN `<script>` or `<link>`, and do not strip the CSP header at the
proxy.

## `TRUSTED_PROXIES`

Rate limits key on the client IP. Behind a proxy, uvicorn sees the proxy (or
the Docker gateway) as the TCP peer, so every caller would share one bucket
unless the app trusts forwarding headers.

`TRUSTED_PROXIES` is a comma-separated list. Empty (the default) ignores
`X-Forwarded-For` / `X-Real-IP`.

**Literal IP (works today):** the TCP peer the container actually sees. For a
proxy on the host talking to `127.0.0.1:8000`, that is usually the compose
network **gateway**, not `127.0.0.1`:

```bash
docker inspect \
  "$(docker compose -f docker-compose.yml -f docker-compose.prod.yml ps -q app)" \
  --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

Put that address in `.env`:

```env
TRUSTED_PROXIES=172.18.0.1
```

If Caddy or nginx runs as a container on the same compose network and proxies
to `app:8000`, use that proxy container's IP instead of the gateway.

**CIDR or hostname (preferred for Docker):** name the whole compose network
instead of a single gateway IP that changes when the project is recreated,
for example `TRUSTED_PROXIES=172.18.0.0/16`. Hostnames are resolved once at
startup. A literal IP still works.

Only list addresses you control. A client that can connect from a listed
address can spoof the forwarding headers.

## Caddy (auto-TLS)

Install Caddy on the host. A five-line site block is enough; Caddy obtains
and renews Let's Encrypt certificates and sets `Host`, `X-Forwarded-For`, and
`X-Forwarded-Proto` on the upstream request.

```caddy
crm.example.com {
	reverse_proxy 127.0.0.1:8000
	# HSTS: Caddy's automatic HTTPS redirects HTTP to HTTPS but does not
	# send Strict-Transport-Security. The app sends it when cookies are
	# Secure or BASE_URL is https. Do not strip that response header.
}
```

If you set `CRM_PORT` to something other than 8000, change the upstream to
match (`127.0.0.1:8001`, …). Recreate the CRM container after editing `.env`
so `BASE_URL` and `TRUSTED_PROXIES` take effect.

**Recreate, not `restart`, after any `.env` change** — including rotating
`SECRET_KEY` or any `CRM_*_KEY`. Compose only interpolates `.env` into the
container at creation time; `docker restart <container>` reuses the already-
materialized environment and silently keeps the old value. Use
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`
(no `--build` needed for a config-only change) so the container is recreated.
Rotating `SECRET_KEY` this way immediately invalidates every existing session
cookie and CSRF token for that instance — expected, and the reason to warn
users first if you're doing it outside a compromise response.

Do not wrap `/mcp` or `/oauth/*` in `basicauth` or `@denied` remote-IP
matchers.

## nginx

Terminate TLS with certbot (or your own certificates) and proxy the whole
site. Header forwarding is not automatic; set it explicitly.

```nginx
server {
    listen 80;
    server_name crm.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name crm.example.com;

    ssl_certificate     /etc/letsencrypt/live/crm.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/crm.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        # The app already sends this when cookies are Secure. Repeat it here
        # so a browser still sees HSTS if something in front drops upstream
        # headers. includeSubDomains covers names under this server_name
        # only — do not put the CRM on an apex that still has HTTP-only
        # subdomains.
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        # /mcp and /oauth/* use their own keys. Do not add auth or an
        # allow/deny IP list here — hosted MCP connectors have no stable CIDR.
    }
}
```

Change `proxy_pass` if `CRM_PORT` is not 8000. Reload nginx after edits:
`sudo nginx -t && sudo systemctl reload nginx`.

## Offboarding a customer

There is no export or delete route in the app — only pipeline stages, offers,
ICP criteria, and a user's own API keys have delete endpoints. Deleting a
customer's data end to end is a manual operator checklist today:

1. Stop the instance's containers (`docker compose -p crm-<name> down`).
2. Delete the instance's data directory (`./instances/<name>/data`, or
   wherever `CRM_DB_PATH`/the bind mount points).
3. Delete the instance's local backups (`scripts/backup.sh`'s output
   directory for that instance).
4. Purge the instance's off-host backup destination — S3, `rclone`, `restic`,
   or whatever was configured. **The app has zero visibility into this
   step**; it is easy to forget and there is no in-app guardrail.
5. Delete the instance's `.env` (`./instances/<name>/.env`).
6. Remove the instance's reverse-proxy site block and any DNS/registry entry.

If any backup of the instance was ever taken with an older image build that
predates a `.dockerignore` (see the note on baked-in secrets), also check
whether that image was ever pushed anywhere retrievable — deleting the
running instance and its data does not delete a copy that shipped inside an
image layer.

## Monitoring (substantiating "99.9% uptime")

Nothing external watches an instance today. To make the uptime promise
defensible rather than aspirational, run an external probe (not on the same
host) against `https://<name>.../health` — the DB-aware check, not just a
TCP connect — every 60 seconds per customer, alerting on 2 consecutive
failures. Also alert on: backup age > 26h, off-host backup age (if tracked),
disk usage > 80%, container restart count, the staleness-cron gap (no run
logged in 25h), and TLS certificate expiry < 14 days. The probe's monthly
uptime report is the only evidence that would hold up if a customer asked
for one.
