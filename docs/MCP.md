# MCP: the agent interface

Day-to-day use of the screens is [Using the CRM](USING.md). This page is the
tool list for an agent.

The admin UI is for a human operator. **`POST /mcp`** is for agents (Grok
custom connectors, xAI Remote MCP tools, Cursor, or any MCP client). It uses the
same sqlite database and service layer as the web app, with no second process.

Judgement stays with your agent (`docs/SCOPE.md`). This endpoint lets it read
and write the CRM's data.

## What an agent can do

Search and get partners, list and get deals and activities, get today's call
queue and due follow-ups, read the service/stage/offer catalogue, ingest leads
(same idempotent rules as `POST /api/v1/leads`), create and update partners and
deals, move stages, record call outcomes, log timeline activities, manage
services and offers, assign owners, bulk-update deals, and hand deal-scoped work
to another agent or a person.

It cannot delete records, reshape the pipeline (that is `CRM_STAGES_API_KEY`
and `/admin/stages`), manage users, or send email.

Tools tell the agent to **search before create**, prefer `create_lead` /
`ingest_leads` for research candidates (not `create_partner`), default
`update_partner` to fill-empty (`fill_empty_only=false` to overwrite),
call `get_lead` before `convert_lead`, and use `record_call_outcome`
after a live call.

## Auth

`CRM_MCP_API_KEY` is a **separate** secret from `CRM_API_KEY` (lead ingest) and
`CRM_STAGES_API_KEY` (pipeline config). Unset means fail closed.

Send it as `Authorization: Bearer <key>` or `X-API-Key: <key>`. Query-string
keys are ignored.

Hosted connectors that only speak OAuth 2.1 (PKCE, dynamic client registration,
for example Grok custom connectors) use:

1. Set `CRM_MCP_API_KEY` in `.env` and deploy.
2. Add a custom connector pointing at `https://your.domain/mcp`.
3. Complete `/oauth/authorize`. The page shows the redirect URL the code will
   be sent to. Check it is the client you intended, then paste the MCP key or
   confirm if you are already logged in.

Clients that accept a static header (xAI Remote MCP tools, Cursor) can skip
OAuth and pass the key directly.

`/mcp` must be reachable by the connector, and it is gated by its own key or
OAuth token, so it does not need an IP allowlist. Forward `/mcp`, `/oauth/*`
and `/.well-known/*` through your reverse proxy.

## Rate limit

Failed MCP auth is capped at 120 requests per 60 seconds per client IP (the
guessing bucket). A valid MCP key or OAuth token uses a larger bucket of 600
requests per 60 seconds, keyed on the token id (`env:CRM_MCP_API_KEY` or
`oauth:{client_id}`), not the client IP — so a flood of bad keys cannot lock
out the real agent. Over the limit returns `429` `{"error":"rate_limited"}`
with `Retry-After`. Set `TRUSTED_PROXIES` behind a proxy so the unauthenticated
cap keys on the real client.

## Handshake (for debugging)

```bash
# initialize
curl -sS http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "X-API-Key: $CRM_MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"curl","version":"0"}}}'

# tools/list
curl -sS http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $CRM_MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Transport is Streamable HTTP with JSON responses (SSE if the client `Accept`s
only `text/event-stream`). It is stateless, so no session is required.

## Tools

| Tool | What it does |
| --- | --- |
| `search_partners` | Search name, email, phone, website. Returns nested people and the parent `company`. |
| `get_partner` | Full record: profile, parent company, people, opportunities, open `leads`, `is_customer`, and recent activities. |
| `list_deals` | Opportunities by default (`type=opportunity`, `include_lost=false`). Filter by `stage`, `owner` / `owner_key`, `service_slug`, `parent_deal_id`, `tags`, `type` (`lead` / `all`), `include_lost`. **Breaking:** lost records are hidden unless `include_lost=true`. Each row includes `type`, `active`, `lost_reason`, `tags`, `outcome` (`open` / `won` / `lost` / `disqualified`), and `is_disqualified`. |
| `list_tags` | Tags in use, each with a `count`. Lost records are not counted. |
| `get_deal` | One deal record (lead or opportunity), including `tags`, `child_deals`, `outcome` (`open` / `won` / `lost` / `disqualified`), and `is_disqualified`. A lead also returns its contact columns. |
| `list_activities` | The timeline for a partner or a deal record, newest first. Accepts a lead without a partner. |
| `get_call_queue` | Open opportunities in the qualified pool with a dialable phone. No leads. Filters: `stage`, `service_slug`, `source_prefix`, `include_new=true`, `owner`. `limit` up to 50. |
| `list_due_followups` | Open deal records whose `next_action_date` is today or earlier. Includes leads and opportunities. Filter `type`, `owner` / `owner_key`, `service_slug`. |
| `list_catalog` | Services, pipeline stages (with `is_lost` marked deprecated), offers, call-outcome keys, activity types (includes `research`), and `lost_reasons` (including `is_disqualification`). |
| `ingest_leads` | Creates **leads**, not partners. `type=opportunity` keeps the old partner + opportunity path. Strong-key matches fill-empty; weak-key-only matches create a new lead. Optional `tags` merge. |
| `create_lead` | Create one lead. Does not create a partner. Never refuses on duplicates; returns `possible_duplicates`. Do not use `create_partner` for a research candidate. |
| `update_lead` / `get_lead` / `list_leads` | Lead reads and writes. `get_lead` returns `fit`, `conversion_readiness`, `possible_duplicates`, `last_researched_at`, `research_note_count`. Lead payloads include `outcome` and `is_disqualified`. |
| `find_duplicates` | Strong and weak identity matches against leads, opportunities and partners. |
| `convert_lead` | Call `get_lead` first. Check `conversion_readiness` and `possible_duplicates`. Errors: `not_a_lead`, `lead_lost`, `missing_service`, `partner_ambiguous`, `duplicate_open_opportunity`. |
| `mark_lost` / `restore_deal` / `merge_leads` | Archive with a lost reason; restore a mistake; merge leads (max 10 sources). Merged records cannot be restored. `mark_lost` on an already-lost record with a different reason changes the reason and keeps `closed_at`. The same reason returns `already_lost`. The returned deal includes `outcome` and `is_disqualified`. |
| `list_lost_reasons` / `create_lost_reason` / `update_lost_reason` | Operator lost-reason configuration. `create_lost_reason` and `update_lost_reason` take `is_disqualification`. Outcome follows the current flag. |
| `create_partner` / `update_partner` | `update_partner` fills empty fields by default. Do not use `create_partner` for a research candidate. Use `create_lead`. |
| `create_deal` / `update_deal` | `create_deal` creates an **opportunity**. For an unqualified candidate, use `create_lead`. Accepts `probability`, `priority`, `expected_close`. `update_deal` rejects `type`. |
| `set_deal_stage` | Move an **opportunity** to any configured stage. Rejects a lead (`not_an_opportunity`) and a lost record (`deal_lost`). Entering `triggers_nurture` fires nurture. Entering `is_won` can fire the deal-won webhook. A move into an existing `is_lost` stage also runs `mark_lost`. |
| `record_call_outcome` | One of `no_answer`, `interested`, `meeting_scheduled`, `won`, `not_interested`. Rejects a lead or a lost record. `not_interested` calls `mark_lost` with reason "Not a fit" and does not move the stage. |
| `log_activity` | Add a `call`, `email`, `meeting`, `note` or `research` to the timeline. `partner_id` is optional when `deal_id` is given. `source_url` is http/https only. |
| `create_service` / `update_service` | Add or change a catalogue service (`slug` is idempotent on create; `active=false` hides it without deleting; `nurture_list_slug`). |
| `create_offer` / `update_offer` | Priced offers (`name`, `service_id` or `service_slug`, `price`, `currency`, `description`, `active`). Idempotent on name + service. Currency defaults to USD. |
| `set_deal_owner` | Assign an owner slug such as `alice`, `bob` or `sales-agent`. |
| `bulk_update_deals` | Selects opportunities only. Set `set_stage` and/or `next_action` + `next_action_date`, `add_tags` / `remove_tags`, or `mark_lost` with `lost_reason`, for up to 50 `deal_ids`, or for a `service_slug` (optionally filtered by `stage`). |
| `create_delegated_task` | Deal-scoped work for another agent or a person. Needs `deal_id` and `title`. Owner defaults to the default owner and status to `delegated`. Idempotent on deal + title + owner while open. |
| `list_delegated_tasks` | Filter `owner`, `status`, `deal_id`, `due_only`. Rows include webhook delivery fields. |
| `get_delegated_task` | Full task row plus partner and deal, with webhook `notified` / `notified_at` / `last_attempt_at` / `last_error`. |
| `update_delegated_task` | Change `status`, `due_date`, `brief`, `result_notes`, `owner`. |
| `complete_delegated_task` | Sets `status=done`, stores `result_notes` and logs a note on the deal timeline. |
| `list_stage_automations` | Every stage automation, including disabled ones. Empty until an operator adds one. |
| `create_stage_automation` | `name`, `stage_key`, optional `service_slug` / `service_id` / `offer_id`, `enabled`, and `actions`. |
| `update_stage_automation` | Rename, retarget, enable or disable, change scope, or replace `actions`. `service_slug` `""` or `service_id` null clears the service scope. `offer_id` null clears the offer scope. |
| `delete_stage_automation` | Remove one automation. Deals and tasks already created stay. |

## Stage automations

Operator data, stored in `stage_automations` / `stage_automation_actions`.
A new database has no rows. Nothing in the product hard-codes a service
slug or a stage key.

An automation has a name (unique, case-insensitive; the operator can rename
it), a stage, and an optional service and/or offer. Both scopes are AND.
If the service or offer row later disappears, that automation does not run.

Actions, at most one of each:

- `delegated_task`: `owner`, `title`, `brief`, optional `due_in_days`.
  Title and brief may contain `{partner_name}`, `{deal_id}`, `{stage}`,
  `{service_name}`, `{service_slug}`, `{automation_name}`. The task is
  created with status `delegated`. The webhook still follows
  `DELEGATE_DEFAULT_OWNER`.
- `spawn_child_deals`: `service_slugs` (or `service_ids`). Each child keeps
  the parent's partner, sets `parent_deal_id`, uses
  `source` `automation:<automation id>`, and copies no offer and no price.
  A missing service is skipped. The automation does not insert catalog rows.

`set_deal_stage` runs enabled automations whose stage matches the stage
just entered. Creating a deal that is already in the stage does not.
Moving the deal in again does not open a second task while one with the
same title and owner is still `proposed` or `delegated`, and does not open
a second child while one for that service is still open. A finished task
or a closed child does not block a new one.

Admin → Automations edits the same rows. The form shows one task and one
set of child services; saving replaces the action list.

`list_catalog` includes `delegated_task_owners` only when
`DELEGATED_TASK_OWNERS` is set (comma-separated slugs).

An optional script, `scripts/seed_example_automations.py`, seeds two named
example automations (requires the example services from
`scripts/seed_services.py`).

## Delegated tasks and webhooks

Owners are free-form slugs (letters, digits, hyphens). The **default owner** is
`DELEGATE_DEFAULT_OWNER` (default `agent`). When a task is owned by the default
owner and its status is `delegated`, the CRM POSTs to `TASK_WEBHOOK_URL` once,
with `Authorization: Bearer $TASK_WEBHOOK_TOKEN` if a token is set. The payload
includes `task_id`, so the receiver can acknowledge without creating duplicates.
Unset URL means poll only: the agent calls
`list_delegated_tasks(owner=agent, status=delegated)`.

Every attempt records `last_attempt_at` and `last_error` (cleared on a 2xx).
Inspect them with `get_delegated_task` or `list_delegated_tasks`, with no SQL.

## Research walkthrough

1. `find_duplicates` with a company name and website.
2. `create_lead` if there is no match. The partners table does not change.
3. `log_activity` with `type=research` and a `source_url` as findings arrive.
4. `update_lead` (`fill_empty_only` default true) to add contact fields.
5. `get_lead` and read `conversion_readiness` and `possible_duplicates`.
6. `convert_lead` when the agent decides the lead is worth pursuit, or
   `mark_lost` with a reason (for example "Not a fit", or "Not now" to
   start nurture).

## Nurture webhook

Moving an opportunity into a stage with the `triggers_nurture` role, or
`mark_lost` with a reason that has `triggers_nurture`, POSTs
`{email, name, list_slug, partner_id, deal_id}` to `NURTURE_WEBHOOK_URL`
(Bearer `NURTURE_WEBHOOK_TOKEN` if set). When fired from `mark_lost`, the
payload also has `type` and `lost_reason`. `partner_id` may be null; `email`
and `name` then come from the lead. `list_slug` is the service's
`nurture_list_slug`. A lead with no service logs a failure activity and
does not POST. The CRM does not run drip sequences.

## Deal-won webhook

Moving a deal into a stage with the `is_won` role (from a stage that is not
already won) POSTs `{deal_id, stage, partner_id, partner_name, partner_email,
service_id, service_name, service_slug, value_estimate, offer}` to
`DEAL_WON_WEBHOOK_URL` (Bearer `DEAL_WON_WEBHOOK_TOKEN` if set). Point it at
an invoicing tool or Zapier. An unset URL or a network failure is logged as
a system activity and the stage change still succeeds. Invoice status is
not written back into the CRM.

## Example calls

```json
{"name": "create_service", "arguments": {
  "name": "Consulting", "slug": "consulting"
}}

{"name": "update_service", "arguments": {
  "service_id": 5, "description": "Monthly advisory retainer.",
  "nurture_list_slug": "consulting-interest"
}}

{"name": "create_offer", "arguments": {
  "name": "Discovery Session", "service_slug": "consulting",
  "price": 350, "currency": "USD",
  "description": "Paid discovery session"
}}

{"name": "ingest_leads", "arguments": {"leads": [
  {"company_name": "Northside Cafe", "phone": "0400 111 222",
   "service_slug": "consulting", "source": "scrape:cafes"}
]}}

{"name": "get_call_queue", "arguments": {
  "stage": "new", "service_slug": "consulting", "limit": 50
}}

{"name": "set_deal_owner", "arguments": {"deal_id": 12, "owner": "alice"}}

{"name": "bulk_update_deals", "arguments": {
  "service_slug": "consulting", "stage": "new",
  "set_stage": "contacted", "next_action": "Call",
  "next_action_date": "2027-01-15"
}}

{"name": "create_delegated_task", "arguments": {
  "deal_id": 28,
  "title": "Send meeting reminder",
  "owner": "agent",
  "due_date": "2027-01-16",
  "created_by": "sales-agent"
}}

{"name": "list_delegated_tasks", "arguments": {
  "owner": "agent", "status": "delegated"
}}

{"name": "complete_delegated_task", "arguments": {
  "task_id": 1,
  "result_notes": "Reminder emailed."
}}

{"name": "set_deal_stage", "arguments": {"deal_id": 28, "stage": "proposal"}}

{"name": "list_deals", "arguments": {"parent_deal_id": 28}}

{"name": "ingest_leads", "arguments": {"leads": [
  {"name": "Ada Stone", "email": "ada@example.com", "service_slug": "consulting",
   "tags": ["campaign:example-2026-09", "referral"]}
]}}

{"name": "update_deal", "arguments": {
  "deal_id": 28, "add_tags": ["clinic"]
}}

{"name": "bulk_update_deals", "arguments": {
  "deal_ids": [28, 29],
  "add_tags": ["campaign:example-2026-09"]
}}

{"name": "list_deals", "arguments": {
  "tags": ["campaign:example-2026-09", "referral"],
  "stage": "new"
}}

{"name": "list_tags", "arguments": {}}
```

## Follow-on deals

`parent_deal_id` links a follow-on deal to its parent. The child must share the
parent's `partner_id`, and cycles are rejected. It appears on `get_deal`
(`child_deals`), as a `list_deals` filter, and on `create_deal` / `update_deal`.

## Deal tags

A tag is a lowercase slug of letters, digits, hyphens and colons, such as
`campaign:example-2026-09` or `clinic`. Deals return `tags` (`[]`
when none). `list_deals` with `tags` (or a single `tag`) returns only deals
that carry every listed tag, and that filter combines with the others.
`list_tags` is the set of tags in use and how many deals have each one.

`ingest_leads` merges `tags` onto a new or already-open deal. Leaving `tags`
out of the lead leaves the set alone. `update_deal`'s `tags` replaces;
`add_tags` and `remove_tags` merge. `bulk_update_deals` merges across the
deals you already select with `deal_ids` or `service_slug`.
