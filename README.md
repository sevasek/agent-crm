# agent-crm

A small CRM built to be operated by an AI agent. It exposes an MCP endpoint
so an agent can search contacts, work the call queue, log calls and move deals,
with guardrails: it cannot delete records or send email. A plain web app gives
the human operator a view of everything the agent did.

Built for one operator (a solo consultant or a small business that sells by
conversation). FastAPI, raw sqlite3 (WAL), Jinja2, Docker. MIT licensed.

**Docs:** [`docs/SCOPE.md`](docs/SCOPE.md) (what's in and out, and why) ·
[`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) (entities and schema) ·
[`docs/MCP.md`](docs/MCP.md) (the agent interface)

## What it does

- Contacts and companies in one table; people link to their company.
- Deals with a pipeline you define: stages, order and roles are data, editable
  in the app, over the stages API, or by nothing at all if you keep the defaults.
- A next action and date on every deal, with due and overdue flags and a daily
  check that records each due follow-up on the contact's timeline.
- A daily call list ranked out of 100 by a fixed formula (value, source,
  contact completeness, recency), a phone-first call view, and one-tap outcomes.
- Configurable fit rules (weighted, deterministic), services and priced offers.
- Lead ingest over an API or CLI, with duplicate matching that never overwrites
  fields you edited.
- An MCP endpoint (Streamable HTTP, static key or OAuth 2.1 + PKCE).

It ships empty: services, offers, stages beyond the starter pipeline and fit
rules are yours to define.

## What it does not do

No email send/receive, no reports or dashboards, no roles or permissions, no
native mobile app, no built-in AI scoring (judgement lives in your agent). See
[`docs/SCOPE.md`](docs/SCOPE.md).

## Quick start

```bash
git clone https://github.com/sevasek/agent-crm.git && cd agent-crm
cp .env.example .env        # set a real SECRET_KEY; see the file for the rest
docker compose up --build -d
docker compose exec app python scripts/create_admin.py you@example.com "Your Name"
docker compose exec app python scripts/seed_services.py   # optional example services
```

Open http://localhost:8000 and log in. `docker-compose.yml` is a development
setup (live reload, `./app` mounted).

## Production

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The app binds to `127.0.0.1:8000`. Put a TLS-terminating reverse proxy in front
and forward everything, including `/mcp` and `/oauth/*`. Set in `.env`:

- `SECRET_KEY`: a real one (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `SECURE_COOKIES=true` and `BASE_URL=https://your.domain`.
- `TRUSTED_PROXIES`: your proxy's address, so rate limits key on the real client.
- `TZ`: used for "today" in follow-up checks.

To create the first admin without shell access, set `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_PASSWORD` once, start the app, log in, then remove the password
from `.env` and recreate the container.

The `staleness-cron` service runs the follow-up check on start and daily at 08:00.

## Connect an agent

Set `CRM_MCP_API_KEY`, then point an MCP client at `https://your.domain/mcp`
with `Authorization: Bearer <key>`. Hosted connectors that need OAuth can use
`/oauth/authorize`. Tools, limits and auth are in [`docs/MCP.md`](docs/MCP.md).

## Ingest leads

`POST /api/v1/leads` with `X-API-Key: $CRM_API_KEY` (header only; fails closed
when unset), or from the CLI:

```bash
python scripts/inject_leads.py tests/fixtures/leads/example.json
python scripts/inject_leads.py --dry-run tests/fixtures/leads/example.json
```

Field meanings and ingest rules are in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).
`service_slug` must already exist in your catalog.

## Import contacts from markdown

`scripts/import_clients.py <dir>` reads markdown files with frontmatter
(`company`, `name`, `email`, optional `service_slug`) and turns the body into a
timeline note. Re-runs are safe. Use `--dry-run` to preview.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## Managed and supported

Prefer not to run it yourself? A managed and supported option is available on
request: https://sevasek.com/crm

## License

[MIT](LICENSE)
