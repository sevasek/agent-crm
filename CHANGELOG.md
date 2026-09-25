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

### Changed

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
  order.

### Security

- CI `pip-audit` is now gating (leftover from #22). The FastAPI bump clears
  Starlette findings that blocked the job: PYSEC-2026-1943 (multipart DoS,
  0.40.0), PYSEC-2026-1941 (large multipart files, 0.47.2), PYSEC-2026-161
  (Host header / `request.url.path`, 1.0.1), PYSEC-2026-2280 / 2281
  (HTTPEndpoint method dispatch, 1.1.0), PYSEC-2026-248 (path in authority,
  1.3.0), and PYSEC-2026-249 (urlencoded `request.form()` limits, 1.3.1).

[Unreleased]: https://github.com/sevasek/agent-crm/compare/main...HEAD
