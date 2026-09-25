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
}
```

If you set `CRM_PORT` to something other than 8000, change the upstream to
match (`127.0.0.1:8001`, …). Recreate the CRM container after editing `.env`
so `BASE_URL` and `TRUSTED_PROXIES` take effect.

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
        # /mcp and /oauth/* use their own keys. Do not add auth or an
        # allow/deny IP list here — hosted MCP connectors have no stable CIDR.
    }
}
```

Change `proxy_pass` if `CRM_PORT` is not 8000. Reload nginx after edits:
`sudo nginx -t && sudo systemctl reload nginx`.
