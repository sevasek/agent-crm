# MCP: the agent interface

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

Tools tell the agent to **search before create**, prefer `ingest_leads` for
inbound leads, default `update_partner` to fill-empty (`fill_empty_only=false`
to overwrite), and use `record_call_outcome` after a live call.

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
| `get_partner` | Full record: profile, parent company, people, deals and recent activities. |
| `list_deals` | Filter by `stage`, `owner` / `owner_key`, `service_slug`, `parent_deal_id`, and `tags` (one or more; the deal must carry every tag). `tag` is a single-tag alias, combined with `tags` the same way. Each deal includes `tags` (`[]` when none). |
| `list_tags` | Tags in use, each with a `count` of deals. |
| `get_deal` | One deal, including `tags` and a `child_deals` summary (`id`, service slug, stage, offer). |
| `list_activities` | The timeline for a partner or a deal, newest first. |
| `get_call_queue` | Qualified-pool deals with a dialable phone, ranked by the call score. Filters: `stage`, `service_slug`, `source_prefix`, `include_new=true`, `owner`. `limit` up to 50. |
| `list_due_followups` | Open deals whose `next_action_date` is today or earlier, oldest first. Filter `owner` / `owner_key`, `service_slug`. |
| `list_catalog` | Services, pipeline stages (with role flags), offers (`service_id`, `price`, `currency`, `description`), call-outcome keys and allowed activity types. Read it before `set_deal_stage` or `record_call_outcome`. |
| `ingest_leads` | Bulk lead ingest. Email is optional. Matching order: email, then phone + name, then website + name, then name within the parent. Rows with a company and phone create a company partner and a deal. Optional `tags` on a lead are merged; omitting `tags` leaves the existing set alone. |
| `create_partner` / `update_partner` | `update_partner` fills empty fields by default. |
| `create_deal` / `update_deal` | `offer_id`, `owner_key` / `owner`, `external_ref`, `parent_deal_id` (same partner, cycles rejected), and tags. `create_deal` takes `tags`. `update_deal` takes `tags` (replace; `[]` clears) or `add_tags` / `remove_tags` (merge; remove wins if a tag is in both). A duplicate open deal from `create_deal` is returned unchanged — use `add_tags` or `ingest_leads` to merge. |
| `set_deal_stage` | Move a deal to any configured stage. Entering a `triggers_nurture` stage fires the nurture webhook. Entering an `is_won` stage from a non-won stage fires the deal-won webhook. |
| `record_call_outcome` | One of `no_answer`, `interested`, `meeting_scheduled`, `won`, `not_interested`, with an optional note. Targets resolve by stage role, not by name; `no_answer` leaves the stage so the deal stays in today's queue. |
| `log_activity` | Add a `call`, `email`, `meeting` or `note` to the timeline. |
| `create_service` / `update_service` | Add or change a catalogue service (`slug` is idempotent on create; `active=false` hides it without deleting; `nurture_list_slug`). |
| `create_offer` / `update_offer` | Priced offers (`name`, `service_id` or `service_slug`, `price`, `currency`, `description`, `active`). Idempotent on name + service. Currency defaults to USD. |
| `set_deal_owner` | Assign an owner slug such as `alice`, `bob` or `sales-agent`. |
| `bulk_update_deals` | Set `set_stage` and/or `next_action` + `next_action_date`, and/or `add_tags` / `remove_tags`, for up to 50 `deal_ids`, or for a `service_slug` (optionally filtered by `stage`). |
| `create_delegated_task` | Deal-scoped work for another agent or a person. Needs `deal_id` and `title`. Owner defaults to the default owner and status to `delegated`. Idempotent on deal + title + owner while open. |
| `list_delegated_tasks` | Filter `owner`, `status`, `deal_id`, `due_only`. Rows include webhook delivery fields. |
| `get_delegated_task` | Full task row plus partner and deal, with webhook `notified` / `notified_at` / `last_attempt_at` / `last_error`. |
| `update_delegated_task` | Change `status`, `due_date`, `brief`, `result_notes`, `owner`. |
| `complete_delegated_task` | Sets `status=done`, stores `result_notes` and logs a note on the deal timeline. |

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

## Nurture webhook

Moving a deal into a stage with the `triggers_nurture` role POSTs
`{email, name, list_slug, partner_id, deal_id}` to `NURTURE_WEBHOOK_URL`
(Bearer `NURTURE_WEBHOOK_TOKEN` if set). `list_slug` is the service's
`nurture_list_slug`. The CRM does not run drip sequences: point the webhook at
your mailing tool or automation. An unset URL, a missing list slug or a missing
email is logged as a system activity and the stage change still succeeds.

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
  {"name": "Ada North", "email": "ada@example.com", "service_slug": "consulting",
   "tags": ["campaign:icp-hc-illawarra-2026-09", "illawarra"]}
]}}

{"name": "update_deal", "arguments": {
  "deal_id": 28, "add_tags": ["church"]
}}

{"name": "bulk_update_deals", "arguments": {
  "deal_ids": [28, 29],
  "add_tags": ["campaign:icp-hc-illawarra-2026-09"]
}}

{"name": "list_deals", "arguments": {
  "tags": ["campaign:icp-hc-illawarra-2026-09", "illawarra"],
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
`campaign:icp-hc-illawarra-2026-09` or `church`. Deals return `tags` (`[]`
when none). `list_deals` with `tags` (or a single `tag`) returns only deals
that carry every listed tag, and that filter combines with the others.
`list_tags` is the set of tags in use and how many deals have each one.

`ingest_leads` merges `tags` onto a new or already-open deal. Leaving `tags`
out of the lead leaves the set alone. `update_deal`'s `tags` replaces;
`add_tags` and `remove_tags` merge. `bulk_update_deals` merges across the
deals you already select with `deal_ids` or `service_slug`.
