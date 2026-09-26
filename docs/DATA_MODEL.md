# Data model

Entities, schema, the deal pipeline, nurture hand-off, deal-won invoice
hand-off and lead ingest. For what
the app does and does not attempt, see [`SCOPE.md`](SCOPE.md). For the agent
interface, see [`MCP.md`](MCP.md).

The schema lives in `app/database.py`. `PRAGMA user_version` is the schema
version; numbered additive migrations run inside a transaction on startup.
A database newer than the running code refuses to start. No ORM.

## Entities

- `partners`: people and companies.
- `services`: what you sell (the catalogue).
- `deals`: one pursuit of a service by a partner. Carries pipeline state.
- `deal_tags`: labels on a deal (a campaign slug, a region, anything else).
- `activities`: the timeline.
- `pipeline_stages`, `offers`, `icp_criteria`: operator-defined configuration.
- `delegated_tasks`: deal-scoped work handed to another agent or a person.
- `users`, `api_keys`, `mcp_oauth_clients`: auth plumbing.

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
| `linkedin_url`, `x_url`, `instagram_url`, `facebook_url`, `youtube_url` | TEXT | Per-platform profiles. A legacy `social_url` is copied onto the matching column at startup. |
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

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER NOT NULL | FK to `partners(id)`. |
| `service_id` | INTEGER NOT NULL | FK to `services(id)`. |
| `stage` | TEXT NOT NULL DEFAULT 'new' | A `pipeline_stages.key`. Plain text, not a declared FK: deleting a stage that is in use is refused at the service layer. |
| `source` | TEXT | Free text (referral, cold outreach, website, ...). Feeds the call score. |
| `value_estimate` | REAL | An estimate, not money. |
| `pain_points`, `goals` | TEXT | Per deal, not per partner: the same partner may pursue a different service later with different aims. |
| `next_action` | TEXT | What to do next. |
| `next_action_date` | TEXT | ISO date. Drives due and overdue flags and the daily follow-up check. |
| `created_at`, `updated_at` | TEXT | |
| `closed_at` | TEXT | Set when the stage enters a won or lost stage. |
| `offer_id` | INTEGER | FK to `offers(id)`. Defaults to the default offer on create. |
| `owner_key` | TEXT | Owner slug. |
| `external_ref` | TEXT | Caller's own id, for example from a source system. A value that is a `campaign:` tag slug is copied onto a tag the first time `deal_tags` is created; the ref itself is left unchanged. |
| `parent_deal_id` | INTEGER | FK to `deals(id)`. Links a follow-on deal to its parent. Same partner required; cycles rejected at the service layer. |

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

Schema v2 (`migrate_002`) creates `deal_tags` and copies any `external_ref` whose lowercased form is a `campaign:` slug onto a tag. Later startups do not repeat that copy, so removing a tag sticks. `external_ref` is not rewritten.

### `activities`

The timeline, one row per interaction.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER NOT NULL | FK to `partners(id)`. A partner has a timeline before any deal exists. |
| `deal_id` | INTEGER | FK to `deals(id)`. Null for partner-level notes. |
| `type` | TEXT NOT NULL | `call`, `email`, `meeting`, `note` or `system`. `system` marks entries the app wrote (stage changes, webhook outcomes, follow-up flags). Anything else falls back to `note`. |
| `body` | TEXT | |
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
| `is_won`, `is_lost` | INTEGER NOT NULL DEFAULT 0 | Entering sets `closed_at` and marks the deal closed for follow-ups. Entering `is_won` from a non-won stage also fires the deal-won webhook. A stage cannot be both. |
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
| `partner_id` | INTEGER NOT NULL | FK to `partners(id)`. |
| `title` | TEXT NOT NULL | |
| `brief` | TEXT | |
| `owner` | TEXT NOT NULL DEFAULT 'agent' | Free-form slug. |
| `status` | TEXT NOT NULL DEFAULT 'delegated' | `proposed`, `delegated`, `done` or `cancelled`. |
| `due_date` | TEXT | ISO date. |
| `created_by`, `result_notes` | TEXT | |
| `webhook_notified_at`, `webhook_last_attempt_at`, `webhook_last_error` | TEXT | Webhook delivery state. |
| `created_at`, `updated_at` | TEXT | |

See [`MCP.md`](MCP.md) for the webhook behaviour.

### Indexes

`partners.parent_id`, `deals.partner_id`, `deals.stage`, `deals.owner_key`,
`deals.parent_deal_id`, `activities.partner_id`, `activities.deal_id`,
`pipeline_stages.position`, `api_keys.user_id`, `delegated_tasks.deal_id`,
`delegated_tasks(owner, status)`, `deal_tags(tag)` (the primary key on
`(deal_id, tag)` covers lookups by deal). `offers` and `icp_criteria` are
small and scanned in full.

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

Open (not won or lost) deals whose `next_action_date` is today or earlier are
due. The deals list shows Due Today and Overdue pills and has a Due filter.
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
automation platform or an agent. Skips are logged as a `system` activity and
never block the stage change: `NURTURE_WEBHOOK_URL` unset, the service has no
valid `nurture_list_slug`, or the partner has no email. A network failure or a
non-2xx response is caught the same way, since this is a network boundary.
Leaving the stage does not undo anything on the receiving side.

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
and does not ingest invoice status back. Skips are logged as a `system`
activity and never block the stage change: `DEAL_WON_WEBHOOK_URL` unset, or
a network failure / non-2xx. Re-saving an already-won deal, moving between
two `is_won` stages, or entering `is_lost` does not fire.

## Lead ingest

**`POST /api/v1/leads`** takes up to 100 leads per call (`X-API-Key:
$CRM_API_KEY`, header only, fail-closed when unset, rate-limited, body capped).
The CLI `scripts/inject_leads.py` runs the same logic in-process. Per lead
(`app/services/leads.py::ingest_lead`):

1. `service_slug` must exist, else `invalid_service`.
2. `name` is required, else `invalid`. Email or phone is also required.
3. `company_name`, if given, finds or creates a company partner by exact name,
   and the person's `parent_id` points at it.
4. `is_company`, if truthy, stores the lead itself as a company. Use it for
   sources that only have a business name. It is different from `company_name`,
   which creates a separate parent record.
5. The existing partner is matched by email, then phone + name, then website +
   name, then exact name within the same parent and company flag. Otherwise a
   new partner is created.
6. If the partner already has an open deal for that service, nothing new is
   created (`duplicate_open_deal`), so a job can re-run over the same source
   data safely. Otherwise the deal is created (`created` for a new partner,
   `existing_partner_new_deal` for an existing one).

Re-runs fill empty partner and deal fields and never overwrite values an
operator edited. Only these keys are read; everything else is dropped:
`name`, `email`, `company_name`, `service_slug`, `phone`, `title`, `website`,
`address`, `social_url`, `linkedin_url`, `x_url`, `instagram_url`,
`facebook_url`, `youtube_url`, `preferred_channel`, `industry`, `team_size`,
`source`, `value_estimate`, `pain_points`, `goals`, `next_action`,
`next_action_date`, `is_company`, `owner_key`, `offer_id`, `external_ref`,
`tags`. `tags`, when present, is an array of slugs merged onto the deal.
Omitting it leaves any tags the deal already has. Invalid slugs in the array
are dropped.
Numbers are coerced and bad types become `invalid` or null, never a traceback.

The CLI exits 0 only when every lead is `created`, `duplicate_open_deal` or
`existing_partner_new_deal`.

**Why an API rather than direct database access:** the database is one sqlite
file with a single-writer assumption. A second process touching it would break
that, so integrations go through the API or MCP.

## Agent interface

`POST /mcp` is Streamable HTTP MCP over the same service functions as the admin
UI. Auth is `CRM_MCP_API_KEY` (fail-closed, header only, separate from the
ingest and stages keys) or an OAuth 2.1 access token. See [`MCP.md`](MCP.md).
