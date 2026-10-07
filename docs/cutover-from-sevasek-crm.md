# Cutover from sevasek/crm

Replace the CRM at `crm.sevasek.com` (repo `sevasek/crm`, Paul's VPS) with
this app, in place, on Wednesday 14 October 2026. This file is the runbook.
Do not point two app processes at one sqlite file. Do not merge or deploy
from a review PR until the shadow test below has been done on a **copy** of
`crm.db`.

The Health Check kickoff and the follow-on child deals are two stage
automations you create after boot. The migration does not insert them, and
a new database starts with none. The seed script and the equivalent MCP
calls are below. Mustard Seed and any other client leave the table empty.

The private `sevasek/crm` tree was not available while this was written.
Behaviour was checked against the live MCP catalog and tool list on
7 October 2026, and against an unversioned fixture shaped like that catalog
(`tests/legacy_sevasek_fixture.py`). The shadow test on a real copy of
`crm.db` is still required. Anything that copy disagrees with is a stop,
not a surprise to paper over on the live host.

## Environment

Copy the live `.env` and change names. Keep the secret values.

| Live (`sevasek/crm`) | This app | Notes |
| --- | --- | --- |
| `WILLOW_TASK_WEBHOOK_URL` | `TASK_WEBHOOK_URL` | Same receiver. Drop the old name; it is not read. |
| `WILLOW_TASK_WEBHOOK_TOKEN` | `TASK_WEBHOOK_TOKEN` | Sent as `Authorization: Bearer`. |
| (Willow is the default task owner) | `DELEGATE_DEFAULT_OWNER=willow` | Default here is `agent`. The seeded kickoff task uses owner `willow`. The webhook fires only when the task owner equals `DELEGATE_DEFAULT_OWNER`, so this must be `willow` or Willow is not POSTed. |
| `BASE_URL` | `BASE_URL` | `https://crm.sevasek.com` on the real host. On the shadow host use `https://crm-next.sevasek.com` (the OAuth issuer is this value). |
| `SECRET_KEY` | `SECRET_KEY` | Keep the bytes. Rotating it logs every browser session out and invalidates MCP OAuth refresh tokens. |
| `CRM_MCP_API_KEY` | `CRM_MCP_API_KEY` | Keep it. Bearer auth does not read the database. |
| `CRM_API_KEY` | `CRM_API_KEY` | Keep it. n8n already sends this on `POST /api/v1/leads`. |
| `CRM_STAGES_API_KEY` | `CRM_STAGES_API_KEY` | Keep it. Separate from the leads key. |
| (proxy IP lists on leads and stages) | `LEADS_IP_ALLOWLIST`, `STAGES_IP_ALLOWLIST` | Copy the live allowlist CIDRs. Unset means no IP check. `/mcp` is not on these lists. |
| (Traefik in front) | `TRUSTED_PROXIES` | Docker network CIDR Traefik uses, so `X-Real-IP` is the client and the allowlists match. |
| (data owned by uid 1004) | `APP_UID=1004`, `APP_GID=1004` | Entrypoint default is 1000. A `0700` directory owned by 1004 will not start if these stay at 1000. |
| (compose memory limit) | already `mem_limit: 256m` on `app` | Do not raise it for the cutover. `staleness-cron` is 128m. |

Also set, so `list_catalog` still returns the owner list Joe and Willow read:

```env
DELEGATED_TASK_OWNERS=paul,bethany,willow,joe,unassigned
```

Unset, that key is absent. The live catalog always has it.

`NURTURE_WEBHOOK_URL` / `DEAL_WON_WEBHOOK_URL` keep the same names. Copy them if the live `.env` sets them.

### Owner strings `joe-bot` and `hermes`

This app does not rewrite owner slugs. `clean_owner_key` keeps `joe-bot` and
`hermes` as themselves. The live MCP owner enum is `paul`, `bethany`,
`willow`, `joe`, `unassigned`, and the live descriptions mention `joe-bot`
as an alias. There is no alias table here.

Rows already stored as `hermes` or `joe-bot` stay that way across the
upgrade. Willow polling `list_delegated_tasks(owner=willow)` will not see a
task owned by `hermes`. A bot that now sends `joe` will not match deals
stored as `joe-bot`.

Do not run an automatic rewrite on startup. On the **shadow copy**, decide
and then either teach the bots the stored slugs or update the copy:

```sql
UPDATE delegated_tasks SET owner = 'willow' WHERE owner = 'hermes';
UPDATE delegated_tasks SET owner = 'joe' WHERE owner = 'joe-bot';
UPDATE deals SET owner_key = 'joe' WHERE owner_key = 'joe-bot';
UPDATE partners SET owner_key = 'joe' WHERE owner_key = 'joe-bot';
```

Run the same statements on the live file only during the cutover window,
after the backup, and only if the shadow copy showed those rows. Take a
count first (`SELECT owner, COUNT(*) FROM delegated_tasks GROUP BY owner`,
and the same for `deals.owner_key` and `partners.owner_key`).

## What the upgrade does to sessions and connectors

Confirmed in `tests/test_legacy_upgrade.py` against this codebase:

- OAuth access and refresh tokens are signed with `SECRET_KEY` and the salts
  `mcp-oauth-access` and `mcp-oauth-refresh`. They are **not** mixed with
  `install_id`. Keeping `SECRET_KEY`, and keeping the `mcp_oauth_clients`
  row (the migration uses `CREATE TABLE IF NOT EXISTS`), lets
  `refresh_access` succeed. Access tokens last one hour; refresh tokens
  last 30 days.
- Browser session cookies **are** mixed with a per-database `install_id`.
  A legacy file has no `app_install` row, so the first boot inserts a new
  random id. Every existing session cookie fails signature check. People
  log in again. Password hashes are not touched. This logout is expected.
  Do not "fix" it by clearing `install_id` after boot; the new id is what
  later restarts keep using.
- `CRM_MCP_API_KEY` is only an environment variable. Keeping it keeps
  Bearer calls working even though browser sessions died.
- The live signer's salt was not diffed (private repo). The connector test
  on `crm-next` is the check that the salts match. If refresh returns
  `invalid_grant` while `SECRET_KEY` was kept, stop and re-connect the
  bots; do not rotate the key to "try something".

The upgrade also writes `pre-migrate-v0-to-v7-<utc>.db` under `./backups`
(mounted at `/app/backups`) before it changes the file. Schema version on
a legacy file is 0. A second boot does not migrate again and does not
rotate `install_id`.

Campaign slugs in `deals.external_ref` (`campaign:...`) are copied onto
`deal_tags` once. The `external_ref` text is left as stored, including a
value like `success-agent:mustard-seed`, which is not a tag. Child
`parent_deal_id` values and delegated-task webhook columns are kept.

## MCP tools the bots call

On 7 October 2026 the live MCP connection exposed **27** tools. This app
keeps those 27, with the same argument names (`tests/test_mcp_contract.py`),
and adds `list_tags` plus `list_stage_automations`, `create_stage_automation`,
`update_stage_automation`, and `delete_stage_automation`. Extra arguments
(`tags`, `add_tags`) are ignored by a bot that does not send them. A bot
that only calls the old names is unaffected. The earlier note of "29 = 28 +
list_tags" does not match the live connection: there was no 28th live tool.

`list_catalog` on this app also returns `delegated_task_default_owner`,
which the live payload does not. Extra keys are safe. The missing live key
is `delegated_task_owners`, which is why the env var above exists.

## Shadow test

Use a new compose project and a new hostname. Never bind-mount the live
`crm.db`.

1. On the VPS, as the user that owns the live data (uid 1004), checkpoint
   WAL and copy:

   ```bash
   sqlite3 /path/to/live/data/crm.db "PRAGMA wal_checkpoint(TRUNCATE);"
   install -d -o 1004 -g 1004 -m 700 /path/to/crm-next/data /path/to/crm-next/backups
   sqlite3 /path/to/live/data/crm.db ".backup /path/to/crm-next/data/crm.db"
   chown 1004:1004 /path/to/crm-next/data/crm.db
   chmod 600 /path/to/crm-next/data/crm.db
   ```

   If `sqlite3` is not on the host, run `scripts/backup.sh` against the
   live file and copy the snapshot. Do not `cp` a WAL database while the
   live app is writing.

2. Checkout this app beside the live checkout (a different directory).
   `.env` is a copy of live with the renames in the table, plus:

   ```env
   BASE_URL=https://crm-next.sevasek.com
   APP_UID=1004
   APP_GID=1004
   DELEGATE_DEFAULT_OWNER=willow
   DELEGATED_TASK_OWNERS=paul,bethany,willow,joe,unassigned
   COMPOSE_PROJECT_NAME=crm-next
   ```

   `TASK_WEBHOOK_URL` can be a request-bin or left empty for this pass.
   Do not point it at Willow's production webhook until you mean to.

3. Traefik host rule for `crm-next.sevasek.com` only. Same external network
   as n8n, but do not change n8n's production URL yet.

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     -f docker-compose.traefik.yml up -d --build
   ```

4. `GET https://crm-next.sevasek.com/health` returns 200. Confirm
   `./backups/pre-migrate-v0-to-v7-*.db` exists and `PRAGMA user_version`
   on the copy is 7. Confirm row counts for `partners`, `deals`,
   `delegated_tasks` match the snapshot you took before boot.

5. Log in in a browser. The old `crm.sevasek.com` session cookie must not
   work here (different host, and a new `install_id`). That is the logout
   side effect, not a failed boot.

6. Connector test: from a bot that already has a refresh token for the
   **live** issuer, this shadow host is a different `BASE_URL`, so that
   token is for the other issuer. To test "same key still verifies", use
   a throwaway client registered against `crm-next`, or temporarily point
   one non-production connector at `crm-next` and complete OAuth once.
   Then restart the container without changing `SECRET_KEY` and confirm
   `tools/list` still works with the refresh token. Also call `/mcp` with
   `Authorization: Bearer $CRM_MCP_API_KEY` and confirm `tools/list`
   contains `list_tags`, the 27 live names, and the four
   `*_stage_automation` tools.

7. Seed the two Health Check automations (the migration does not). From
   the shadow checkout:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     -f docker-compose.traefik.yml run --rm app \
     python scripts/seed_sevasek_automations.py
   ```

   Or the same two calls over MCP (`create_stage_automation`). Names are
   yours to change afterwards in Admin → Automations; the script matches
   on these names and will not overwrite an edit unless you pass
   `--replace`.

   ```json
   {"name": "create_stage_automation", "arguments": {
     "name": "HC paid: send kickoff",
     "stage_key": "hc_paid",
     "service_slug": "health-check",
     "actions": [{
       "type": "delegated_task",
       "owner": "willow",
       "title": "Send HC kickoff",
       "brief": "Paid Health Check. Send the kickoff."
     }]
   }}
   ```

   ```json
   {"name": "create_stage_automation", "arguments": {
     "name": "HC presented: spawn delivery and support",
     "stage_key": "hc_presented",
     "service_slug": "health-check",
     "actions": [{
       "type": "spawn_child_deals",
       "service_slugs": ["automation-delivery", "automations-support"]
     }]
   }}
   ```

   If a stage or service slug is missing, the script stops and does not
   insert catalog rows. IT Support is not in the second automation.

   Then, on the **copy**, move one Health Check deal you can afford to
   disturb from `hc_in_delivery` (or whatever is not already `hc_paid`) to
   `hc_paid`. Expect one delegated task titled `Send HC kickoff`, owner
   `willow`, status `delegated`. Move it to `hc_presented` only if that
   deal does not already have an open automation-delivery or
   automations-support child (an existing open child is left alone, even
   when its `source` is the old `hc-spawn:<deal id>`). New children use
   `source` `automation:<automation id>`, the same partner,
   `parent_deal_id` set, no `offer_id`, no `value_estimate`.
   `it-support` must not appear. Compare the kickoff `brief` with an
   existing live task of the same title. The brief shipped here is
   `Paid Health Check. Send the kickoff.` If the live rows use different
   wording, edit that automation in the UI (or change the string in
   `scripts/seed_sevasek_automations.py` and re-run with `--replace`)
   before cutover day. Until the seed has been run, moving a deal does
   not create the task or the children.

8. `POST /api/v1/leads` from the n8n host (or its IP) with the real
   `CRM_API_KEY`. A 403 `ip_not_allowlisted` means the allowlist or
   `TRUSTED_PROXIES` is wrong. Fix it here, not on the live host.

9. Leave `crm-next` up for a day if you can. Do not dual-write.

## Cutover (14 October)

1. Pause writers: n8n workflows that POST leads or stages, and tell Joe,
   Willow, and Riker to stop MCP calls. The web UI too, if anyone is in it.
2. Checkpoint WAL and copy `crm.db` somewhere that is not `./backups` and
   not the data directory (`crm.db.manual-<date>`). This is the rollback
   file you control. The app will write a second copy into `./backups` on
   first boot.
3. Put this app's compose files and image in the live checkout. Keep the
   live `.env` secrets. Apply the renames and the new variables. `BASE_URL`
   stays `https://crm.sevasek.com`. `APP_UID=1004`, `APP_GID=1004`.
4. Swap the containers in place. Same Traefik network, same alias `crm`,
   same host rule. n8n keeps calling `http://crm:8000`.

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     -f docker-compose.traefik.yml up -d --build
   ```

5. `GET /health` is 200. `PRAGMA user_version` is 7.
   `./backups/pre-migrate-v0-to-v7-*.db` is present. Run
   `python scripts/seed_sevasek_automations.py` once with the same compose
   files (or the two `create_stage_automation` calls in the shadow-test
   step). The live file does not have those rows until you do. Spot-check a known
   deal id, a child `parent_deal_id`, and a delegated task's
   `webhook_notified_at`.
6. Browser login works with the existing password (old cookies do not).
7. Connector test with the **kept** `SECRET_KEY` and `CRM_MCP_API_KEY`:
   `tools/list` via Bearer, and one OAuth refresh from a bot that was
   connected before the swap. If refresh fails, bots re-run OAuth; do not
   roll forward by changing `SECRET_KEY`.
8. Resume n8n, then the bots.

The deploy workflow (`.github/workflows/deploy.yml`) does not run until
repository variables `DEPLOY_ENABLED=true` and
`DEPLOY_REPOSITORY=sevasek/agent-crm` are set, and the SSH secrets exist.
Leave those unset until after this cutover. Forks do not have the
variables, so they do not deploy. The SSH user on the VPS is the uid 1004
account.

## Rollback

1. `docker compose ... stop` the new containers.
2. Move the migrated `data/crm.db` aside. Restore either the manual copy
   from step 2 of the cutover or `backups/pre-migrate-v0-to-v7-*.db` (that
   file is the database from before the migration, `user_version` 0).
   Restore with the sqlite backup API or by replacing the file while the
   app is stopped. Delete `crm.db-wal` and `crm.db-shm` next to it so a
   stale WAL is not replayed.
3. Start the previous `sevasek/crm` image and compose file against that
   restored file. Same `SECRET_KEY`. Browser sessions from before the
   cutover work again only if you restored the pre-migration file (no
   `install_id` yet) **and** the old app did not mix `install_id` into
   the cookie. OAuth tokens signed by the old app work again because the
   key did not change.
4. Do not boot this app against the restored file until you intend to
   migrate again. A boot will take another pre-migrate backup and migrate
   forward.

If `/health` never went green and the new process exited during migrate,
the live file may already be mid-migration. Prefer the manual copy over
guessing. The pre-migrate file is written **before** the schema change, so
it is the clean rollback when it exists.
