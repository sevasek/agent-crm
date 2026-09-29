# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0. Tagged releases publish a multi-arch image to
[`ghcr.io/sevasek/agent-crm`](https://github.com/sevasek/agent-crm/pkgs/container/agent-crm)
as `vX.Y.Z` and `latest`. Until the first tag, install from source with
`docker compose ... --build` as documented in the README.

## [Unreleased]

### Added

- `.dockerignore` so `.env`, `.git`, and `./data` (including sqlite files) are
  not copied into the image. CI builds a dirty context and fails if those paths
  exist in `/app`.
- Production compose log rotation (`json-file`, 10m × 5 files), `mem_limit`,
  and `pids_limit` on `app` and `staleness-cron`.
- Entrypoint `umask 077` plus ownership and mode-bit repair on `/app/data`
  (directories 0700, files 0600). Clear startup error if the database path is
  not writable.
- Dependabot for pip, Docker, and GitHub Actions.
- `pip-audit` (gating) and a Trivy image scan (HIGH/CRITICAL, report-only)
  in CI.
- Release workflow: pushing a `v*` tag builds and pushes
  `ghcr.io/sevasek/agent-crm:<tag>` and, for non-prerelease tags, `:latest`
  for `linux/amd64` and `linux/arm64`.
- Production reverse-proxy + TLS examples (Caddy and nginx) in
  [`docs/DEPLOY.md`](docs/DEPLOY.md).
- Deal tags (`deal_tags`): campaign slugs and other labels on deals, with
  MCP / ingest / UI filter and write support. Schema v2.
- Online sqlite backup and restore (`./scripts/backup.sh`,
  `./scripts/restore.sh`): backup API (safe under load), `PRAGMA
  integrity_check`, mode-0600 files in `./backups/`, `BACKUP_KEEP_DAYS`
  pruning, and an optional restic/rclone/S3 hook. Cron and systemd
  examples ship beside the scripts.
- Operator notes for cutting the first `v*` tag and what the GHCR
  workflow publishes, in [`docs/RELEASE.md`](docs/RELEASE.md).
- Optional `DEAL_WON_WEBHOOK_URL` / `DEAL_WON_WEBHOOK_TOKEN`: when
  `set_deal_stage` first moves a deal into an `is_won` stage, the CRM POSTs
  partner, service, offer and value to an invoicing tool. Unset is a no-op.
  Invoice status is not written back.
- Admin deal form can set or clear a parent deal and lists children on edit.
  Pipeline cards show follow-on linkage. Post-sale work is child deals plus
  delegated tasks (closes #10; no fulfilment module).
- Admin UI accessibility: skip link to `#main`, `aria-current="page"` on the
  matching top-nav item, visible focus styles, announced login/MCP authorize
  errors, and read-only deal tags on the call view.
- Vendored Pico.css 2.1.1 (`app/static/vendor/pico.min.css`) as the admin UI
  baseline: one `<link>` in `base.html`, `--pico-primary*` mapped to the
  existing brand blue, `data-theme="light"` locked so Pico's auto dark mode
  does not clash with hand-picked pill colors (closes #43).
- Vendored htmx 2.0.11. Pipeline stage-move returns the two affected
  columns as `hx-swap-oob` fragments when the request has `HX-Request`,
  so a card jumps columns without a full reload. No-JS still POSTs the
  same form.
- Call-outcome buttons on the queue and call view swap a fragment
  instead of reloading the page (closes #45). Plain POST still
  redirects.
- `DISABLE_DOCS` plus hiding `/docs`, `/redoc`, and `/openapi.json` when
  any `CRM_*_KEY` is set, not only when `BASE_URL` is https (issue 59).
- Per-database `install_id` (schema v5) mixed into the session cookie
  signer. `scripts/new-instance.sh` refuses a `SECRET_KEY` that matches
  a sibling instance or the `.env.example` default. Production intent
  (https `BASE_URL` or `SECURE_COOKIES=true`) refuses to boot on that
  default (issue 55).

### Removed

- Unused `rate_limit_hits` sqlite table (schema v3). Rate limiting stays
  in-memory (`app.services.auth`). Existing v2 databases drop the table on
  startup; new databases never create it.
- Unused helpers `get_user_by_id` and `list_clients` (no app callers).

### Changed

- `docs/UI_UPGRADE_PLAN.md` records that Pico/htmx phases 1–3 shipped
  (PRs #53, #62); SortableJS, Tom Select, Alpine, and real dark mode stay
  deferred (#46, #47).
- Custom admin CSS sits on Pico's primitives: drop duplicate input/`<button>`/
  `<details>` rules, keep `.btn` for link-buttons, make `.btn-secondary` a
  full outlined chip so deal-stage filters match Pico's scale (closes #44).
- Unset `NURTURE_WEBHOOK_URL` / `DEAL_WON_WEBHOOK_URL` no longer writes a
  `system` activity that looks like a webhook failure. Real send failures
  still log.
- Share `row_to_dict` from `app.database` instead of duplicate `_row`
  helpers in pipeline, ICP, and offers services.
- Production port bind is `127.0.0.1:${CRM_PORT:-8000}:8000` so a second
  instance on the same host can set `CRM_PORT` instead of a third compose file.
- Base image is `python:3.12-slim` pinned by its multi-arch index digest.
  Dependabot can bump the digest; a floating `3.12-slim` tag is no longer used.
- `python-multipart` 0.0.20 → 0.0.31 and `python-dotenv` 1.0.1 → 1.2.2 (safe
  pin bumps for known CVEs).
- FastAPI 0.115.0 → 0.133.0 and pin Starlette 1.3.1 (smallest release that
  clears current pip-audit; 0.115.0 requires `starlette<0.39`, and even
  0.115.12 only allows `<0.47`). uvicorn stays 0.30.6. Jinja2
  `TemplateResponse` calls use the Starlette 1.x `(request, name, context)`
  order. Offer name fields use `Form("")` so an empty HTML input still
  reaches handler validation (Starlette 1.x treats `name=` as missing).
- Image build upgrades `pip` before installing requirements, and sets
  `PYTHONDONTWRITEBYTECODE=1` so a read-only rootfs does not try to write
  `.pyc` (issue 58).
- Production compose: `cap_drop: ALL` with the four caps the entrypoint
  needs for chown + `gosu`, `no-new-privileges`, read-only rootfs, and
  `tmpfs` `/tmp` on `app` and `staleness-cron` (issue 60).
- Remaining admin HTML required fields (partner/service name, stage
  key/label, ICP field/operator, login email/password, new-deal
  partner/service) also use `Form("")` so an empty posted control hits
  the handler's HTML 4xx instead of Starlette 1.x JSON 422. Empty call
  outcome and call-view offer name no longer 422 (they still 303). CSRF
  tokens stay required (`Form(...)`).
- New-deal parent picker lists only that partner's candidates when a
  partner is selected (`/deals/new?partner_id=` or the posted partner);
  with no partner yet, options are grouped by partner name so the list
  is usable without JavaScript. Pipeline cards count every child deal,
  including children hidden by the current tag filter.
- Rate limits: failed login, lead/stages ingest, MCP, and OAuth
  register/token/authorize-POST count toward a per-IP guessing bucket. A
  valid key or OAuth token uses a larger bucket keyed on the key/token
  id (`env:CRM_*`, `userkey:{id}`, `oauth:{client_id}`). Successful
  dynamic client registration uses the larger authenticated bucket
  keyed on IP (each 201 mints a unique `client_id`). A flood of bad
  keys cannot lock out the agent. Authorize GET (the form) and
  well-known metadata are not counted.

### Security

- CSRF tokens are now bound to the session cookie they were minted for
  (`generate_csrf_token`/`validate_csrf_token` in `app/services/auth.py`).
  Previously the token only proved recency, not which session issued it, so
  a token minted from a logged-out `GET /auth/login` could be replayed
  against a separately-authenticated session. Cookie-authenticated
  POST/PUT/PATCH/DELETE requests are also now rejected with 403 if their
  `Origin`/`Referer` names a different host than the request's own `Host`
  (defense-in-depth; requests that send neither are unaffected).
  `GET /auth/logout` is now `POST /auth/logout`.
- Self-service password change (`/settings`, `POST /settings/password`) and
  session revocation. `users.session_version` (schema v5) is embedded in the
  session cookie and checked on every request; changing your password or
  using the new "Log out of all other sessions" button
  (`POST /settings/logout-everywhere`) bumps it, so a stolen or leaked
  session cookie stops working immediately instead of staying valid for the
  full 30-day cookie lifetime. The browser that made the change stays
  logged in; every other outstanding cookie is invalidated.
- OAuth authorization codes are single-use (OAuth 2.1). Each code carries a
  `jti`; the first presentation of a valid code persists it in
  `mcp_oauth_used_codes` (schema v4) and a second exchange returns
  `invalid_grant`. A failed PKCE or client check still burns the code.
- CI `pip-audit` is now gating (leftover from #22). The FastAPI bump clears
  Starlette findings that blocked the job: PYSEC-2026-1943 (multipart DoS,
  0.40.0), PYSEC-2026-1941 (large multipart files, 0.47.2), PYSEC-2026-161
  (Host header / `request.url.path`, 1.0.1), PYSEC-2026-2280 / 2281
  (HTTPEndpoint method dispatch, 1.1.0), PYSEC-2026-248 (path in authority,
  1.3.0), and PYSEC-2026-249 (urlencoded `request.form()` limits, 1.3.1).

[Unreleased]: https://github.com/sevasek/agent-crm/compare/main...HEAD
