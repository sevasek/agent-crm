# New client instance

One CRM process, one sqlite file, one set of secrets, one public hostname.
A second client on the same VPS is another copy of those four things. It does
not share a database, admin user, session cookie, or OAuth issuer with any
other instance, including one you already run for yourself.

Do not copy an `.env` from another instance. `scripts/new-instance.sh`
generates new secrets and refuses a `SECRET_KEY` that matches a sibling
under `instances/`. Session cookies are also tied to a per-database
`install_id`. The OAuth issuer is `BASE_URL`, so two hostnames are two
issuers. The MCP key is whatever is in that instance's `.env`; a copied
key would be accepted by both.

This deploy does not take backups. If the VPS disk or that instance's
`crm.db` is lost, the data is gone.

## What you need

- Docker, and Compose 2.24 or newer (`docker compose version`). The compose
  files use `!reset` / `!override`.
- This repo on the VPS, on the commit you intend to run.
- A DNS name that points at the VPS, for example `crm.client.example`.
- Caddy or nginx on the host, terminating TLS. Configs are in
  [`DEPLOY.md`](DEPLOY.md). Forward the whole site, including `/mcp`,
  `/oauth/*`, and `/.well-known/*`. Do not put basic auth or an IP
  allowlist on those paths. Hosted connectors have no stable address range.

Run the commands below from the repo root, as root, or export `APP_UID` and
`APP_GID` to your own numeric ids first. The script chowns the data
directory to those ids and writes the same values into the instance `.env`.
The container drops to them. If the chown fails, the script stops. The
image's default is `1000:1000`.

## Create the instance

Pick a short lowercase name (`black-diamond`), the client's timezone, and
the first admin's email. Set the public URL before you start so the OAuth
issuer is right from the first boot.

```bash
export BASE_URL_HINT=https://crm.client.example
sudo -E scripts/new-instance.sh black-diamond Australia/Sydney jem@client.example
```

`sudo -E` keeps `BASE_URL_HINT` and `APP_UID` if you set them. The script:

- writes `instances/black-diamond/.env` (mode 0600) with a new
  `SECRET_KEY`, `CRM_API_KEY`, `CRM_STAGES_API_KEY`, and `CRM_MCP_API_KEY`
- allocates the next free `CRM_PORT` (from 8000) and
  `COMPOSE_PROJECT_NAME=crm-black-diamond`
- chowns `instances/black-diamond/data` and starts compose against that
  directory only
- waits until `http://127.0.0.1:<port>/health` answers
- creates the admin and prints a one-time password once
- prints a Caddy site block

Migrations run inside the process on startup (`init_db`). A brand-new
database is created at the current schema. You do not run a migrate
command by hand.

The catalogue is empty apart from the starter pipeline (New, Contacted,
Qualified, Nurture, Proposal, Won, Lost). Those rows are editable. Do not
run `scripts/seed_services.py` unless you want three example services.
Offers default to currency USD; pass `AUD` when creating an offer for an
Australian client.

## TLS

Paste the printed Caddy block, with the real hostname, and reload Caddy.
Or use the nginx example in [`DEPLOY.md`](DEPLOY.md). Then:

```bash
curl -fsS https://crm.client.example/health
```

You want `{"status":"ok"}`. `503` with `unavailable` means sqlite is not
writable. `503` with `low_disk_space` means free space on the database
filesystem is under `HEALTH_MIN_FREE_MB` (default 200).

Log in at `https://crm.client.example`, not at the raw `http://127.0.0.1`
port. `SECURE_COOKIES=true`, so the browser will not keep a session cookie
on plain HTTP. Change the one-time password under Settings.

The session cookie on HTTPS is named `__Host-session` (Secure, Path=/, no
Domain). Do not set a cookie `Domain` in the proxy. That would let one
customer's subdomain send its cookie to a sibling. The app sends HSTS
(`max-age=31536000; includeSubDomains`) when cookies are Secure. That
covers names under the CRM hostname only. Do not serve the CRM on an apex
domain that still has HTTP-only subdomains.

## Connect the Grok bot

The connector talks to this instance only. Nothing in the app is pinned to
one operator's domain.

1. The MCP secret is `CRM_MCP_API_KEY` in `instances/black-diamond/.env`.
   Leave it there. Do not reuse another instance's key.
2. In Grok, add a custom connector whose URL is
   `https://crm.client.example/mcp`.
3. Grok opens `/oauth/authorize` on that host. The page shows the redirect
   URL the code will be sent to. Check it is the client you meant.
4. Approve it while logged in as this instance's admin, or paste this
   instance's `CRM_MCP_API_KEY`.
5. A successful handshake can call MCP. A key or token from another
   instance is rejected.

`/.well-known/oauth-authorization-server` on that hostname must show
`"issuer": "https://crm.client.example"`. If it shows a different host,
`BASE_URL` is wrong. Edit the instance `.env` and recreate the container
(see Updates). Do not `docker restart` after an `.env` change: restart
keeps the old environment.

## Updates

From the repo root, after `git pull`:

```bash
docker compose --project-name crm-black-diamond \
  --env-file instances/black-diamond/.env \
  -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.port.yml -f docker-compose.instance.yml \
  up -d --build
```

That rebuilds only this project's containers. Startup applies any pending
migrations. `docker compose ps` should show the app `healthy`. Check
`/health` again.

Same command without `--build` after an `.env`-only edit, so the container
is recreated with the new values.

`docker compose exec` does not run the entrypoint, so the command is root.
Root in this container cannot read the data directory. One-off commands
need `-u 1000:1000` (or whatever `APP_UID`/`APP_GID` are in that `.env`):

```bash
docker compose --project-name crm-black-diamond \
  --env-file instances/black-diamond/.env \
  -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.port.yml -f docker-compose.instance.yml \
  exec -u 1000:1000 app python scripts/create_admin.py jem@client.example "Jem"
```

## One instance on an empty VPS

If this host will only ever run one CRM, you can skip `instances/` and use
a single `.env` plus:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Generate `SECRET_KEY` and `CRM_MCP_API_KEY` with
`python3 -c "import secrets; print(secrets.token_hex(32))"`. Set
`BASE_URL`, `SECURE_COOKIES=true`, `TZ`, and `TRUSTED_PROXIES` as in
[`.env.example`](../.env.example). Create the admin with
`BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD_FILE` (the file is
not visible in `docker inspect`), then remove the password file and
recreate the container. The rest of this page (TLS, health, Grok, updates)
is the same. A later second client should use `scripts/new-instance.sh`,
not a second copy of this `.env`.
