# Data model

Glossary, schema, the opportunity pipeline, nurture hand-off, deal-won
invoice hand-off and lead ingest. For what
the app does and does not attempt, see [`SCOPE.md`](SCOPE.md). For the agent
interface, see [`MCP.md`](MCP.md).

The schema lives in `app/database.py`. `PRAGMA user_version` is the schema
version; numbered migrations run inside a transaction on startup.
Migrations are additive-only except the reviewed schema 8 rebuild
(`migrate_008`, ADR 0001). A database newer than the running code refuses
to start. No ORM.

## Glossary

These terms replace the older "Entities" list. Do not use "deal" alone in
new documentation: use **lead**, **opportunity** or **deal record**.

| Term | Definition |
|---|---|
| **Partner** | A person or a company that the operator or the agent has confirmed as a real party. One row in `partners`. |
| **Company partner** | A partner with `is_company = 1`. |
| **Person partner** | A partner with `is_company = 0`. A person partner can have a parent company partner (`parent_id`). |
| **Deal record** | One row in `deals`. A deal record is a lead or an opportunity. |
| **Lead** | A deal record with `type = 'lead'`. An unqualified signal. It keeps its own contact data. It can exist without a partner and without a service. |
| **Opportunity** | A deal record with `type = 'opportunity'`. The qualified pursuit of one service for one partner. It must have a partner and a service. Only opportunities use pipeline stages. |
| **Conversion** | The operation that changes a lead into an opportunity. The record keeps its `id`. Conversion sets `type = 'opportunity'` and `date_conversion`. |
| **Open** | A deal record with `active = 1` that is not in an `is_won` stage. |
| **Won** | An opportunity in a stage with `is_won = 1`. |
| **Lost** | A deal record with `active = 0` and a `lost_reason_id`. Lost is not a stage. |
| **Lost reason** | One row in `lost_reasons`. Operator configuration. |
| **Merged** | A lost deal record with `merged_into_id` set. The CRM does not delete merged records. |
| **Research note** | An `activities` row with `type = 'research'`. |
| **Fit score** | The sum of weights of the matching `icp_criteria`. A lead scores from its own columns. An opportunity scores from the partner. |
| **Conversion readiness** | Missing data that conversion needs. Information, not a gate. |
| **Customer** | A partner with one or more won opportunities. Calculated, not stored. |

Other tables: `services` (catalogue), `deal_tags`, `activities`,
`pipeline_stages`, `offers`, `icp_criteria`, `delegated_tasks`,
`stage_automations`, `users`, `api_keys`, OAuth tables, `app_install`.

### Why one `partners` table

A company is a partner with `is_company=1`. Its employees are partners whose
`parent_id` points at it. One table answers "who", contacts roll up to their
company for free, and a solo lead with no company is just a partner with no
parent rather than a special case needing a join. The hierarchy is one level
deep by convention (only `is_company=1` rows are offered as parents).

### Why `deals` is separate from `partners`

A partner can have more than one deal over time (repeat business, several
services). The deal, not the partner, carries the pipeline state. A contact
existing is not the same fact as a sales process being underway.

## Schema

### `partners`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `is_company` | INTEGER NOT NULL DEFAULT 0 | 0/1. Company or person. |
| `parent_id` | INTEGER | FK to `partners(id)`. A person's employer. |
| `name` | TEXT NOT NULL | |
| `email`, `phone`, `website` | TEXT | |
| `title` | TEXT | Role at the company. Meaningful for people. |
| `address` | TEXT | |
| `social_url` | TEXT | Derived primary social link (first filled of the per-platform columns). |
| `linkedin_url`, `x_url`, `instagram_url`, `facebook_url`, `youtube_url` | TEXT | Per-platform profiles. |
| `preferred_channel` | TEXT | `email`, `phone` or `text`. Validated in `app/services/partners.py`, not by a `CHECK`; an unrecognised value becomes null. |
| `industry` | TEXT | Mostly for companies. |
| `team_size` | INTEGER | Validated non-negative; bad input becomes null. |
| `owner_key` | TEXT | Owner slug (for example `alice`). |
| `created_at`, `updated_at` | TEXT | ISO timestamps. |

Enumerations are validated in the service layer rather than with `CHECK`
constraints, so a bad value degrades to null or a default instead of raising.

### `services`

The catalogue. Operator-defined and empty on a fresh install.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL | |
| `slug` | TEXT UNIQUE NOT NULL | |
| `description` | TEXT | |
| `nurture_list_slug` | TEXT | Sent as `list_slug` in the nurture webhook when a deal for this service enters a nurture stage. Nullable; a service without one skips the hand-off. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive services drop out of pickers but keep their history. |
| `created_at` | TEXT | |

### `deals`

One table holds leads and opportunities (`type`). Schema 8 made
`partner_id` and `service_id` nullable so a lead can exist without either.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER | FK to `partners(id)`. Required when `type = 'opportunity'`. Null on a research lead. |
| `service_id` | INTEGER | FK to `services(id)`. Required when `type = 'opportunity'`. |
| `type` | TEXT NOT NULL DEFAULT 'opportunity' | `lead` or `opportunity`. |
| `stage` | TEXT NOT NULL DEFAULT 'new' | A `pipeline_stages.key`. Meaningful on opportunities only. |
| `source` | TEXT | Free text (referral, cold outreach, website, ...). Feeds the call score. |
| `value_estimate` | REAL | An estimate, not money. |
| `pain_points`, `goals` | TEXT | Per deal, not per partner: the same partner may pursue a different service later with different aims. |
| `next_action` | TEXT | What to do next. |
| `next_action_date` | TEXT | ISO date. Drives due and overdue flags and the daily follow-up check. |
| `created_at`, `updated_at` | TEXT | |
| `closed_at` | TEXT | Set when an opportunity enters `is_won`, or when any deal record is marked lost. Cleared on restore. |
| `offer_id` | INTEGER | FK to `offers(id)`. Defaults to the default offer on create. |
| `owner_key` | TEXT | Owner slug. |
| `external_ref` | TEXT | Caller's own id, for example from a source system. A value that is a `campaign:` tag slug is copied onto a tag the first time `deal_tags` is created; the ref itself is left unchanged. |
| `parent_deal_id` | INTEGER | FK to `deals(id)`. Follow-on opportunity only (not a lead). Same partner required; cycles rejected at the service layer. |
| `name`, `contact_name`, `company_name` | TEXT | Lead contact / title. A linked partner does not fill these for scoring. |
| `email`, `phone`, `website`, `title`, `address` | TEXT | Lead contact columns. |
| `linkedin_url`, `x_url`, `instagram_url`, `facebook_url`, `youtube_url` | TEXT | Lead social URLs. |
| `industry`, `preferred_channel` | TEXT | Lead profile. `preferred_channel` is `email`, `phone` or `text`. |
| `team_size` | INTEGER | Non-negative. Bad input becomes null. |
| `probability` | INTEGER | 0–100. Set by the agent or operator, not calculated. |
| `priority` | INTEGER NOT NULL DEFAULT 0 | 0–3. |
| `expected_close` | TEXT | ISO date. |
| `date_conversion` | TEXT | Set once by `convert_lead`. |
| `active` | INTEGER NOT NULL DEFAULT 1 | 0 = lost. |
| `lost_reason_id` | INTEGER | FK to `lost_reasons(id)`. Required when `active = 0`. |
| `lost_note` | TEXT | Free text for the loss. |
| `merged_into_id` | INTEGER | FK to `deals(id)`. Set by `merge_leads` on the source. |
| `board_position` | INTEGER NOT NULL DEFAULT 0 | 0-based order within `stage` on the pipeline board. Schema v9. Drag-and-drop and `set_deal_stage(..., position=)` rewrite the column; a stage change without a position appends. The deals list still sorts by `updated_at`. |

`list_deals()` defaults to `type = 'opportunity'` and `include_lost = false`.
Leads never appear in a pipeline stage column.

### `deal_tags`

Labels on a deal. A campaign is a tag, not a separate table. A deal has many tags and a tag covers many deals.

| Column | Type | Notes |
|---|---|---|
| `deal_id` | INTEGER NOT NULL | FK to `deals(id)`. With `tag`, the primary key. |
| `tag` | TEXT NOT NULL | Lowercase slug of letters, digits, hyphens and colons. Examples: `campaign:icp-hc-illawarra-2026-09`, `church`, `illawarra`. At most 80 characters. |

`get_deal` and `list_deals` always include `tags`, an empty list when the deal has none. `list_deals` accepts `tags` (one or more). A deal must carry every listed tag, and the filter combines with stage, owner, service and parent. `list_tags` returns each tag in use with a `count` of deals.

Writes:

- `create_deal` takes `tags` and sets them on the new row.
- `update_deal` takes `tags` to replace the set (`[]` clears it). `add_tags` and `remove_tags` merge, and run after a replace when both are sent (`remove_tags` wins on overlap). Omitting all three leaves the set alone.
- `bulk_update_deals` takes `add_tags` / `remove_tags` for the same selection it already uses (`deal_ids`, or `service_slug` plus optional stage and owner).
- Lead ingest (`POST /api/v1/leads` and MCP `ingest_leads`) takes an optional `tags` array. A re-run merges those tags onto the open deal. Leaving `tags` off the payload leaves the existing set alone. Entries that are not valid slugs are dropped so one bad label cannot fail the lead.

### `lost_reasons`

Operator configuration, seeded once like pipeline stages.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE NOT NULL | |
| `triggers_nurture` | INTEGER NOT NULL DEFAULT 0 | `mark_lost` with this reason posts the nurture webhook. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive reasons stay on old records but leave pickers. |
| `created_at`, `updated_at` | TEXT | |

Seed names: "Not a fit", "No budget", "Not now" (`triggers_nurture = 1`),
"No response", "Lost to competitor", "Duplicate", "Lost stage (migrated)".
Managed at `/lost-reasons` (and `/admin/lost-reasons`) and via MCP.

### `activities`

The timeline, one row per interaction.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER | FK to `partners(id)`. Nullable so a lead can have research notes before a partner exists. |
| `deal_id` | INTEGER | FK to `deals(id)`. Null for partner-level notes. |
| `type` | TEXT NOT NULL | `call`, `email`, `meeting`, `note`, `system` or `research`. Anything else falls back to `note`. |
| `body` | TEXT | |
| `source_url` | TEXT | http/https only, max 2,000 characters. Used on research notes. |
| `occurred_at`, `created_at` | TEXT | |

### `pipeline_stages`

The pipeline itself, in board-column order. `deals.stage` stores the `key`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `key` | TEXT UNIQUE NOT NULL | Immutable identifier used by deals and the API. Renaming a stage changes only `label`. |
| `label` | TEXT NOT NULL | Display name. |
| `position` | INTEGER NOT NULL DEFAULT 0 | Column and dropdown order. |
| `is_default` | INTEGER NOT NULL DEFAULT 0 | Where a new deal starts. Setting it clears the flag elsewhere. |
| `is_qualified_pool` | INTEGER NOT NULL DEFAULT 0 | Deals here are candidates for the call queue. |
| `triggers_nurture` | INTEGER NOT NULL DEFAULT 0 | Entering this stage fires the nurture webhook. |
| `is_won`, `is_lost` | INTEGER NOT NULL DEFAULT 0 | `is_won` still closes an opportunity and can fire the deal-won webhook. `is_lost` is deprecated: lost is `active = 0` plus a lost reason. Creating a new lost stage is refused. A move into an existing lost stage also runs `mark_lost`. |
| `created_at`, `updated_at` | TEXT | |

Seeded once on first start, then just data. Edit it in `/admin/stages`, or with
`GET / POST / PATCH / DELETE /api/v1/stages` and `POST /api/v1/stages/reorder`
(gated by `CRM_STAGES_API_KEY`, deliberately separate from the ingest key
because reshaping the pipeline has a larger blast radius than posting a lead).

Default stages: `new` (default) → `contacted` → `qualified` (call-queue pool) →
`nurture` (triggers nurture) → `proposal` → `won` / `lost`.

`nurture` is a branch, not a forced step. `set_deal_stage` accepts a move to any
configured stage, because one operator moving their own deals does not need a
transition table. Call outcomes resolve their target by role (`is_won`,
`is_lost`, `triggers_nurture`), so renaming those stages or moving the role to
another stage keeps working. `meeting_scheduled` targets the literal key
`proposal`; if that stage is gone, the outcome is logged and says the stage was
left unchanged. If several stages share a role, the call queue unions the
`is_qualified_pool` stages, while an outcome acts on the first matching stage in
pipeline order.

### `offers`

What you pitch on a call.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL | |
| `is_default` | INTEGER NOT NULL DEFAULT 0 | New deals start on it. Setting it clears the flag elsewhere. |
| `pitch` | TEXT | The 30-second open. |
| `proof_point` | TEXT | One concrete result to cite. |
| `price_anchor` | TEXT | Free text said aloud on a call ("$500/mo"), not computed against. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive offers stay on old deals but leave the pickers. |
| `service_id` | INTEGER | FK to `services(id)`. |
| `price` | REAL | Numeric price. |
| `currency` | TEXT | Defaults to `USD`. |
| `description` | TEXT | |
| `created_at`, `updated_at` | TEXT | |

Deleting an offer that a deal uses is refused, as with stages and services. The
call view can switch a deal's offer or create a new one and assign it in one
step.

### `icp_criteria`

Configurable fit rules. A lead's fit score is the sum of `weight` over every
active criterion that matches, so order does not matter.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `label` | TEXT | Name shown beside the score. |
| `field` | TEXT NOT NULL | One of a fixed allowlist (`icp.MATCHABLE_FIELDS`): industry, team size, is-a-company, preferred channel, presence of email / phone / website / social URL, source, value estimate, pain points, goals. Not an arbitrary column, so a typo cannot create a rule that never matches. |
| `operator` | TEXT NOT NULL | `is_not_null`, `boolean`, `fuzzy_match` (case-insensitive substring, or a `difflib` ratio of at least 0.6), or `eq` / `gt` / `gte` / `lt` / `lte` for numbers. |
| `value` | TEXT | Ignored for `is_not_null`; `true` or `false` for `boolean`; a number for numeric operators. Validated against the operator at write time. |
| `weight` | INTEGER NOT NULL DEFAULT 1 | Points added on a match. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive rules are kept but not scored. |
| `created_at`, `updated_at` | TEXT | |

It is a deterministic weighted-rules formula, not a learned model, and it has no
pass/fail threshold. The call view shows it beside the call-priority score.

### `delegated_tasks`

Deal-scoped work handed from one agent to another agent or a person. Not a
project manager: a title, a brief, an owner and a status tied to a deal.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `deal_id` | INTEGER NOT NULL | FK to `deals(id)`. |
| `partner_id` | INTEGER | FK to `partners(id)`. Null when the deal record is a lead without a partner. |
| `title` | TEXT NOT NULL | |
| `brief` | TEXT | |
| `owner` | TEXT NOT NULL DEFAULT 'agent' | Free-form slug. |
| `status` | TEXT NOT NULL DEFAULT 'delegated' | `proposed`, `delegated`, `done` or `cancelled`. |
| `due_date` | TEXT | ISO date. |
| `created_by`, `result_notes` | TEXT | |
| `webhook_notified_at`, `webhook_last_attempt_at`, `webhook_last_error` | TEXT | Webhook delivery state. |
| `created_at`, `updated_at` | TEXT | |

See [`MCP.md`](MCP.md) for the webhook behaviour.

### `stage_automations` and `stage_automation_actions`

Operator configuration. Schema version 7 creates the tables and inserts no
rows. A stage change runs nothing extra until someone adds an automation
in the admin UI or over MCP. Schema version 9 adds `deals.board_position`
for pipeline card order after the lead-model rebuild (schema 8); it does
not rebuild `deals`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL UNIQUE COLLATE NOCASE | The operator's label. Rename does not change `id`. |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | `0` skips the row. |
| `stage_key` | TEXT NOT NULL | Fires when a deal enters this stage. |
| `service_id` | INTEGER | Null matches every service. A missing service row does not fire. |
| `offer_id` | INTEGER | Null matches every offer. Combined with `service_id` as AND. |
| `created_at`, `updated_at` | TEXT | |

Actions are child rows (`position`, `action_type`, `config` JSON).

| `action_type` | Config | Effect |
|---|---|---|
| `delegated_task` | `owner`, `title`, `brief`, `due_in_days` | One task. Title and brief may use `{partner_name}`, `{deal_id}`, `{stage}`, `{service_name}`, `{service_slug}`, `{automation_name}`. |
| `spawn_child_deals` | `service_ids` | Child deals, same partner, `parent_deal_id` set, `source` `automation:<automation id>`, no offer and no price. |

At most one action of each type. An open task with the same title and owner,
or an open child of that service, is not duplicated when the deal enters the
stage again.

### Post-sale / follow-on deals

Issue #10 asked whether the CRM should grow a fulfilment module (invoice
status, `delivery_status`, a second pipeline). It does not. A won sale that
still needs work is another deal whose `parent_deal_id` points at the original,
plus `delegated_tasks` for hand-offs. Same partner; cycles are rejected. The
admin deal form sets or clears the link and lists children; pipeline cards
show "follow-on of #N" and a child count; `get_deal` returns `child_deals`.
That is the post-sale model.

### Indexes

`partners.parent_id`, `deals.partner_id`, `deals.stage`, `deals.owner_key`,
`deals.parent_deal_id`, `deals(stage, board_position)`, `activities.partner_id`, `activities.deal_id`,
`pipeline_stages.position`, `api_keys.user_id`, `delegated_tasks.deal_id`,
`delegated_tasks(owner, status)`, `deal_tags(tag)` (the primary key on
`(deal_id, tag)` covers lookups by deal),
`stage_automations(stage_key, enabled)`,
`stage_automation_actions(automation_id, position)`.
`offers` and `icp_criteria` are small and scanned in full.

## Scoring

Both scores are formulas computed at query time, not stored columns.

**Call priority**, out of 100 (`app/services/call_queue.py`), for deals in a
`is_qualified_pool` stage whose partner has a dialable phone number:

| Component | Max | How |
|---|---|---|
| Value estimate | 30 | $5k+ = 30, $1k to $5k = 20, under $1k = 10, none = 0. |
| Source | 25 | referral / inbound / existing / repeat = 25, website / form = 15, cold / outreach / linkedin = 10, other = 5, blank = 0. |
| Contact completeness | 25 | 5 points each for email, preferred channel, title, social URL, website. Phone is the admission ticket, not extra points. |
| Recency | 20 | Days since the deal last entered the stage: 7 or fewer = 20, up to 14 = 15, up to 30 = 10, up to 60 = 5, older or unknown = 0. |

The queue shows the top 10. **Fit score** is the `icp_criteria` sum above.

## Follow-ups

Open opportunities (`active = 1`, stage not `is_won`) whose
`next_action_date` is today or earlier are due. Lost records are skipped.
The deals list shows Due Today and Overdue pills and has a Due filter. Leads
stay out of the staleness gate.
`scripts/staleness_gate.py` exits 1 when any are due and logs one `system`
activity per due date (idempotent on re-run). "Today" uses `TZ`. The
`staleness-cron` service runs the gate on start and daily at 08:00.

## Nurture hand-off

When `set_deal_stage` moves a deal **into** a stage with `triggers_nurture`,
`app/services/nurture.py::enroll_partner_in_nurture` POSTs to
`NURTURE_WEBHOOK_URL`:

```
POST {NURTURE_WEBHOOK_URL}
Authorization: Bearer {NURTURE_WEBHOOK_TOKEN}      (only if set)
{"email": ..., "name": ..., "list_slug": ..., "partner_id": ..., "deal_id": ...}
```

The CRM does not run drip sequences. Point the webhook at a mailing tool, an
automation platform or an agent. An unset `NURTURE_WEBHOOK_URL` is a silent
no-op (no timeline note). Missing or invalid `nurture_list_slug`, a partner
with no email, a network failure, or a non-2xx response is logged as a
`system` activity and never blocks the stage change. Leaving the stage does
not undo anything on the receiving side.

## Deal-won invoice hand-off

When `set_deal_stage` moves a deal **into** a stage with `is_won` from a
stage that is not already won, `app/services/won_webhook.py::notify_deal_won`
POSTs to `DEAL_WON_WEBHOOK_URL`:

```
POST {DEAL_WON_WEBHOOK_URL}
Authorization: Bearer {DEAL_WON_WEBHOOK_TOKEN}      (only if set)
{
  "deal_id": ...,
  "stage": ...,
  "partner_id": ...,
  "partner_name": ...,
  "partner_email": ...,
  "service_id": ...,
  "service_name": ...,
  "service_slug": ...,
  "value_estimate": ...,
  "offer": {"id": ..., "name": ..., "price": ..., "currency": ...} | null
}
```

The payload is small and stable: enough for an invoicing tool or Zapier to
raise an invoice, and no secrets. `offer` and `partner_email` are null when
absent; a missing email still fires. The CRM does not create invoices itself
and does not ingest invoice status back. An unset `DEAL_WON_WEBHOOK_URL` is a
silent no-op (no timeline note). A network failure or non-2xx is logged as a
`system` activity and never blocks the stage change. Re-saving an already-won
deal, moving between two `is_won` stages, or entering `is_lost` does not fire.

## Lead ingest

**`POST /api/v1/leads`** takes up to 100 items per call (`X-API-Key:
$CRM_API_KEY`, header only, fail-closed when unset, rate-limited, body capped).
The CLI `scripts/inject_leads.py` runs the same logic in-process.

Default `type` is **lead**. That creates a deal record only — no partner.
A job that still needs a partner and an opportunity must send
`"type": "opportunity"` (CLI: `--type opportunity`). When REST omits
`type`, the response includes `type_defaulted` and the header
`Deprecation: type omitted; default is now lead`.

Per item (`app/services/leads.py::ingest_lead`):

1. `service_slug` must exist when a service is required, else `invalid_service`.
2. Identity is required (company, contact, email, phone, website or LinkedIn).
3. **Lead (default):** match an open lead or opportunity on strong keys only
   (email; phone + name; website + name). A strong match fill-empties that
   record. Weak-key-only matches create a new lead. The partners table is
   unchanged.
4. **Opportunity (`type=opportunity`):** the previous path. Match or create a
   partner, then create or fill-empty an open opportunity for that partner
   and service (`created`, `existing_partner_new_deal`,
   `duplicate_open_deal`).

`scripts/import_clients.py` does **not** inherit this default. It still
creates a partner and an opportunity. `--type lead` is opt-in.

Re-runs fill empty fields and never overwrite values an operator edited.
Allowed keys include the previous contact fields plus `type`,
`probability`, `priority` and `expected_close`. `tags`, when present, is
an array of slugs merged onto the deal record.

The CLI exits 0 when every item is a success status, including
`duplicate_open_lead` and `duplicate_open_opportunity`.

An optional cleanup script `scripts/opportunities_to_leads.py` can move
early-stage opportunities back to leads (`--stage` repeatable, dry-run
by default, `--apply` to write). The migration never runs it.

**Why an API rather than direct database access:** the database is one sqlite
file with a single-writer assumption. A second process touching it would break
that, so integrations go through the API or MCP.

## Agent interface

`POST /mcp` is Streamable HTTP MCP over the same service functions as the admin
UI. Auth is `CRM_MCP_API_KEY` (fail-closed, header only, separate from the
ingest and stages keys) or an OAuth 2.1 access token. See [`MCP.md`](MCP.md).
