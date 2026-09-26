# agent-crm

**A small CRM your AI agent can work.** It exposes an MCP endpoint so an agent
can search contacts, run the call queue, log calls and move deals, without being
able to delete anything or send email. A plain web app shows you everything it did.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/sevasek/agent-crm/actions/workflows/ci.yml/badge.svg)](https://github.com/sevasek/agent-crm/actions/workflows/ci.yml)

![Pipeline board with deals in New, Contacted, Qualified, Nurture and Proposal stages](docs/images/pipeline.png)

FastAPI, sqlite, Jinja2, Docker. One container, one database file, no external services.

> **Status: pre-1.0.** The MCP tool set and the schema may change between
> releases. Back up `data/crm.db` before upgrading.

## Who it's for

**A good fit** if you are one person (a solo consultant, a small clinic, a
tradie) who sells through calls and follow-ups, and you want an agent to do the
CRM busywork while you keep a clear view of the pipeline.

**Not a fit** if you need multiple users with roles and permissions, email sent
and received inside the CRM, reports and dashboards, or a native mobile app.
These are left out on purpose; see [`docs/SCOPE.md`](docs/SCOPE.md).

## Quick start

For trying it out. `docker-compose.yml` is a development setup with live reload;
see [Production](#production) for a real deployment.

```bash
git clone https://github.com/sevasek/agent-crm.git && cd agent-crm
cp .env.example .env
docker compose up --build -d
docker compose exec app python scripts/create_admin.py you@example.com "Your Name"
docker compose exec app python scripts/seed_services.py   # optional example services
```

Open http://localhost:8000 and log in.

## Connect an agent

Generate a key, put it in `.env` as `CRM_MCP_API_KEY`, and recreate the container
(a plain restart does not reload `.env`):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d
```

Then point any MCP client at `http://localhost:8000/mcp` (Streamable HTTP) with
`Authorization: Bearer <key>`. Check it works:

```bash
curl -sS http://localhost:8000/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -H "Authorization: Bearer $CRM_MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

An agent can search contacts (`search_partners`), pull today's queue
(`get_call_queue`), log what happened (`record_call_outcome`, `log_activity`),
and move deals (`set_deal_stage`). It cannot delete records, send email, manage
users, or reshape the pipeline. Tools tell the agent to search before it creates,
and updates fill empty fields instead of overwriting yours.

Hosted connectors that need OAuth 2.1 with PKCE can use `/oauth/authorize`.
Full tool list, auth and limits: [`docs/MCP.md`](docs/MCP.md).

## What's inside

- Companies and people in one table; people link to their company.
- Deals on a pipeline you define: stage names, order and roles are data.
- A next action and date on every deal, with due and overdue flags and a daily
  check that logs each due follow-up on the contact's timeline.
- A daily call list ranked out of 100 by a fixed formula you can read (deal
  value, source, contact completeness, recency).
- A phone-first call view with tap-to-call, the offer to pitch, and one-tap outcomes.
- Weighted fit rules, a service catalogue and priced offers you configure.
- Lead ingest over an API or CLI, with duplicate matching.
- Tags on deals (a campaign, a region, anything else you want to filter by),
  set from the deals page or from the agent.
- Delegated tasks to hand work to another agent or a person, with an optional webhook.
- An optional webhook when a deal enters a nurture stage.

It starts empty apart from a starter pipeline: services, offers and fit rules
are yours to define. Judgement lives in your agent, not in the CRM; scores are
plain formulas, not a model.

## Production

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The app binds to `127.0.0.1:${CRM_PORT:-8000}` (default 8000). Put a
TLS-terminating reverse proxy in front and forward everything, including
`/mcp` and `/oauth/*`. Working Caddy and nginx configs, header forwarding, and
`TRUSTED_PROXIES` notes are in [`docs/DEPLOY.md`](docs/DEPLOY.md). In `.env`:

- `SECRET_KEY`: a real one, not the placeholder.
- `SECURE_COOKIES=true` and `BASE_URL=https://your.domain`.
- `TRUSTED_PROXIES`: your proxy's IP, a CIDR (e.g. the compose network `172.18.0.0/16`), or a hostname, so rate limits use the real client IP.
- `TZ`: decides what "today" means for follow-ups.
- `CRM_PORT` and `COMPOSE_PROJECT_NAME`: set these when more than one
  instance shares a host (see [Multiple instances on one host](#multiple-instances-on-one-host)).

Security defaults: login uses expiring session cookies with CSRF protection;
login and API endpoints are rate limited (failed auth is counted per IP;
authenticated MCP/API traffic has a larger per-key bucket); the lead-ingest,
stages and MCP keys are separate, and each endpoint refuses all requests until
its key is set. Set `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD_FILE`
(preferred; wins over `BOOTSTRAP_ADMIN_PASSWORD`) once to create the first
admin without shell access — the file is not visible in `docker inspect`.
`/health` checks that sqlite is writable and returns 503 if it is not.

The `staleness-cron` service runs the follow-up check on start and daily at 08:00.

Production compose rotates json-file logs at 10 MB × 5 files and caps the app
at 256 MB RAM. `docker stop` / `docker kill` leave the container stopped under
`restart: unless-stopped` — a maintenance stop is not a crash, so start it
again with `docker compose ... up -d` when you are done.

Once a `v*` tag exists, you can run a published multi-arch image instead of
building from the working copy (`build: .` stays the default). How to cut
the first tag (`v0.1.0`) and what the workflow publishes:
[`docs/RELEASE.md`](docs/RELEASE.md).

```yaml
# compose override (a third -f file, or docker-compose.override.yml)
services:
  app:
    image: ghcr.io/sevasek/agent-crm:vX.Y.Z
    build: !reset
    pull_policy: always
  staleness-cron:
    image: ghcr.io/sevasek/agent-crm:vX.Y.Z
    build: !reset
    pull_policy: always
```

Then `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`
(no `--build`) so Compose pulls the published image instead of building locally.

See [`CHANGELOG.md`](CHANGELOG.md) for what changed between tags.

### Backup, restore and upgrade

All data is one sqlite file, `./data/crm.db`, in WAL mode. Do not `cp` it while
the app runs: you can copy a torn database. Use the shipped online-backup
script (sqlite's backup API, safe under load; files are mode 0600 and land
in `./backups/`, not `./data/`):

```bash
./scripts/backup.sh
```

It prefers host Python against the bind-mounted file, and falls back to
`docker compose exec` if the database is only reachable inside the container.
It verifies `PRAGMA integrity_check` and prunes snapshots older than
`BACKUP_KEEP_DAYS` (default 14).

Nothing leaves the host until you set an off-host hook — pick one:

```bash
# restic / any command; the backup path is appended (or substitute {})
export BACKUP_REMOTE_CMD='restic -r s3:s3.amazonaws.com/your-bucket backup'
# or rclone
export BACKUP_RCLONE_DEST='remote:crm-backups'
# or aws s3
export BACKUP_S3_URI='s3://your-bucket/crm'
```

Do not put cloud credentials in CI; these are operator-only. Install a
schedule from the examples (not just this README):

- `scripts/backup.cron.example` — crontab / `/etc/cron.d`
- `scripts/crm-backup.service.example` + `scripts/crm-backup.timer.example` — systemd, 03:00

Restore (stops the stack, drops WAL/SHM, copies the snapshot, starts):

```bash
./scripts/restore.sh backups/crm-YYYY-MM-DDTHHMMSSZ.db
```

The entrypoint fixes ownership of `./data` on start. Check `docker compose ps`
shows `healthy` and you can log in.

Optional sub-minute RPO: run [Litestream](https://litestream.io/) beside the
app to replicate `data/crm.db` to S3/GCS. It is not bundled and not required.

Upgrade: take a backup (`./scripts/backup.sh`), then `git pull` and re-run
the `up -d --build` command above. `init_db()` stores `PRAGMA user_version`
and applies numbered additive migrations inside a transaction. A process
holding an exclusive migrate lock (file lock + `BEGIN IMMEDIATE`) is the
only one that migrates, so `app` and `staleness-cron` cannot race. If the
on-disk version is *newer* than this code, startup refuses with a clear
error — an older container will not mutate a newer schema. An online backup
is taken automatically before any migration of an existing database
(`backups/pre-migrate-vN-to-vM-*.db`). Schema v2 adds `deal_tags` and, once,
copies any `external_ref` that is a `campaign:` slug onto a tag (the ref is
left unchanged; later startups do not restore a removed tag).

To roll back: `down`, check out the previous version (or previous image),
and restore the pre-migrate backup. Serving a published GHCR image shrinks
the outage to a container swap instead of a rebuild.

### Multiple instances on one host

`scripts/new-instance.sh NAME TZ EMAIL` allocates the next free `CRM_PORT`
(from `instances/ports.tsv`, starting at 8000), writes a mode-600
`instances/$NAME/.env` with fresh `secrets.token_hex(32)` values, sets
`COMPOSE_PROJECT_NAME=crm-$NAME`, starts the stack with
`docker-compose.port.yml` + `docker-compose.instance.yml`, waits for
`/health`, creates the admin via `create_admin.py` (one-time password
printed once — `BOOTSTRAP_ADMIN_PASSWORD` is not left in the env), and
prints a Caddy site block.

`CRM_PORT` is interpolated as `127.0.0.1:${CRM_PORT:-8000}:8000`.
`COMPOSE_PROJECT_NAME` keeps container and network names from colliding
when two instances share a host. `TRUSTED_PROXIES` is set to the compose
network CIDR (`172.16.0.0/12`). Use `--dry-run` to generate the env
without Docker.

Backup and restore default to `./data/crm.db` and the default compose
project. For an instance, point them at that instance or they will copy
or `down` the wrong stack:

```bash
export CRM_DB_PATH=instances/acme/data/crm.db
export COMPOSE_PROJECT_NAME=crm-acme
export CRM_ENV_FILE=instances/acme/.env
export BACKUP_DIR=instances/acme/backups
./scripts/backup.sh
./scripts/restore.sh instances/acme/backups/crm-YYYY-MM-DDTHHMMSSZ.db
```

## Ingest leads

`POST /api/v1/leads` with `X-API-Key: $CRM_API_KEY` (header only), or from the CLI:

```bash
python scripts/inject_leads.py --dry-run tests/fixtures/leads/example.json
python scripts/inject_leads.py tests/fixtures/leads/example.json
```

`service_slug` must already exist in your catalogue. Field meanings and matching
rules are in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md). To load contacts from
markdown files, see `scripts/import_clients.py`.

## Docs

[`SCOPE.md`](docs/SCOPE.md) what's in and out and why ·
[`DATA_MODEL.md`](docs/DATA_MODEL.md) entities, schema and ingest rules ·
[`MCP.md`](docs/MCP.md) the agent interface ·
[`DEPLOY.md`](docs/DEPLOY.md) reverse proxy and TLS ·
[`RELEASE.md`](docs/RELEASE.md) how to cut a version tag

## Contributing

Planned work, known gaps and deferred ideas are tracked in
[GitHub Issues](https://github.com/sevasek/agent-crm/issues); there is no separate
TODO file. The scope is deliberately narrow, so please open an issue to discuss a feature
before sending a pull request. Bug fixes are welcome directly. Run the tests
first:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## Support and security

Questions and bugs: open an issue. To report a vulnerability, use the
[private security report form](https://github.com/sevasek/agent-crm/security/advisories/new)
rather than a public issue (see [`SECURITY.md`](SECURITY.md)).

Want it run for you? Managed hosting is $60 AUD per month and includes secure
hosting, backups, 99.9% uptime, new features deployed frequently, and responsive
support for feature requests: https://sevasek.com/crm

## License

[MIT](LICENSE)
