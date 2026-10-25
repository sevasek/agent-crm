# Data model

Every field in the database, plus the ingest rules and the two outbound notifications.
Day-to-day use is [Using the CRM](USING.md). What the product includes is [Scope](SCOPE.md).
The agent tools are [MCP: the agent interface](MCP.md).

The tables are created in `app/database.py`.

## Glossary

Say **lead**, **opportunity**, or **deal record**. "Deal" on its own is ambiguous: the deals table holds both leads and opportunities.

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
| **Lost** | A deal record with `active = 0` whose lost reason is not a disqualification. Lost is not a stage. |
| **Disqualified** | A deal record with `active = 0` whose lost reason has `is_disqualification = 1`. Reported as disqualified, not lost. |
| **Outcome** | Derived, not stored. `deal_outcome` in `app/services/deal_outcome.py` returns `open`, `won`, `lost`, or `disqualified` from `active`, the stage's `is_won` flag, and the lost reason's current `is_disqualification` flag. |
| **Lost reason** | One row in `lost_reasons`. Operator configuration. |
| **Campaign** | One row in `campaigns`. The shared pitch for a set of deals. A deal is in it when the deal carries the tag `campaign:<slug>`. |
| **Merged** | A lost deal record with `merged_into_id` set. The CRM does not delete merged records. |
| **Research note** | An `activities` row with `type = 'research'`. |
| **Fit score** | The sum of weights of the matching `icp_criteria`. A lead's profile fields come from the lead. An opportunity's profile fields come from the partner. Tag rules are separate: `deal_tag` reads that record's tags, and `company_tag` reads the linked company partner (or the parent company when the linked partner is a person). |
| **Conversion readiness** | Missing data that conversion needs. Information, not a gate. |
| **Customer** | A partner with one or more won opportunities. Calculated, not stored. |

Other tables: `services` (catalogue), `campaigns`, `deal_tags`, `partner_tags`, `activities`,
`pipeline_stages`, `offers`, `icp_criteria`, `delegated_tasks`,
`stage_automations`, `users`, `api_keys`, OAuth tables, `app_install`.

### Why one `partners` table

A company is a partner with `is_company=1`. Its people are partners whose
`parent_id` points at that company. A person with no company is a partner with
no parent. Only a company can be chosen as a parent.

A lead is not a partner. A lead is a row in `deals` that can exist before
anyone is confirmed. Turning it into an opportunity is what creates or links
the partner.

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

One table holds leads and opportunities (`type`). A lead does not need a
partner or a service, so `partner_id` and `service_id` can be empty. An
opportunity needs both.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER | FK to `partners(id)`. Required when `type = 'opportunity'`. Null on a research lead. |
| `service_id` | INTEGER | FK to `services(id)`. Required when `type = 'opportunity'`. `update_deal` and `bulk_update_deals` (`set_service_id` / `set_service_slug` with `deal_ids`) can move it to another active service. |
| `type` | TEXT NOT NULL DEFAULT 'opportunity' | `lead` or `opportunity`. |
| `stage` | TEXT NOT NULL DEFAULT 'new' | A `pipeline_stages.key`. Meaningful on opportunities only. |
| `source` | TEXT | Free text (referral, cold outreach, website, ...). Feeds the call score. |
| `value_estimate` | REAL | An estimate, not money. |
| `pain_points`, `goals` | TEXT | Per deal, not per partner: the same partner may pursue a different service later with different aims. |
| `next_action` | TEXT | What to do next. |
| `next_action_date` | TEXT | ISO date. Drives due and overdue flags and the daily follow-up check. |
| `created_at`, `updated_at` | TEXT | |
| `closed_at` | TEXT | Set when an opportunity enters `is_won`, or when any deal record is first marked lost. Cleared on restore. Left unchanged when `mark_lost` only changes the lost reason. |
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
| `active` | INTEGER NOT NULL DEFAULT 1 | 0 = closed without a win (lost or disqualified). Outcome says which. |
| `lost_reason_id` | INTEGER | FK to `lost_reasons(id)`. Required when `active = 0`. |
| `lost_note` | TEXT | Free text for the loss. |
| `merged_into_id` | INTEGER | FK to `deals(id)`. Set by `merge_leads` on the source. |
| `board_position` | INTEGER NOT NULL DEFAULT 0 | 0-based order within `stage` on the pipeline board. Drag-and-drop and `set_deal_stage(..., position=)` rewrite the column; a stage change without a position appends. The deals list still sorts by `updated_at`. |

`list_deals()` defaults to `type = 'opportunity'` and `include_lost = false`.
Leads never appear in a pipeline stage column.

### `campaigns`

The shared pitch for a planned outbound push: who it is for, which service or offer it leads to, the angle, and the opener. Lead-specific notes stay on the deal timeline. A new install has no campaigns. Rows appear when someone creates one, or when migration 13 backfills a row for each distinct `campaign:` tag already on a deal. Migration 12 is the disqualification flag on lost reasons.

Membership is the tag, not a `campaign_id` column. A deal can be in more than one campaign, and the call queue, ingest, and deal list already filter on `campaign:<slug>`. A second id would be another list that can drift from those tags. The slug is the part after `campaign:`. Renaming the slug rewrites that tag on every linked deal. A tag with no campaign row is just a tag until a campaign with that slug exists; creating the row links the deals without a further write.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT NOT NULL | |
| `slug` | TEXT UNIQUE NOT NULL | Lowercase tag body. The deal tag is `campaign:<slug>`. |
| `audience` | TEXT | Who the campaign is for. |
| `service_id` | INTEGER | FK to `services(id)`. Optional. |
| `offer_id` | INTEGER | FK to `offers(id)`. Optional. If the offer belongs to a service and `service_id` is empty, the service is filled in. An offer for a different service is rejected. |
| `angle` | TEXT | Shared angle. Not a note about one lead. |
| `opener` | TEXT | Shared opener or call script. |
| `channel` | TEXT | `phone`, `email`, or `text`. Blank is allowed. |
| `status` | TEXT NOT NULL DEFAULT 'draft' | `draft`, `active`, `paused`, or `completed`. Backfilled rows are `active`. |
| `start_date`, `deadline` | TEXT | ISO dates (`YYYY-MM-DD`). Both optional. Deadline before the start date is rejected. |
| `target_leads`, `target_contacts`, `target_conversations`, `target_advanced`, `target_won` | INTEGER | Optional counts. Blank means the target is not set. |
| `target_won_value` | REAL | Optional. |
| `created_at`, `updated_at` | TEXT | ISO timestamps. |

Results, win/loss, pacing, and the period breakdown are calculated, not stored. The page and the MCP tools call the same function. Disqualified deals are left out of win/loss when a deal's outcome is `disqualified`. Until that outcome exists, a lost deal counts as lost.

### `deal_tags`

Labels on a deal. A campaign link is the tag `campaign:<slug>` (see `campaigns`). A deal has many tags and a tag covers many deals.

| Column | Type | Notes |
|---|---|---|
| `deal_id` | INTEGER NOT NULL | FK to `deals(id)`. With `tag`, the primary key. |
| `tag` | TEXT NOT NULL | Lowercase slug of letters, digits, hyphens and colons. Examples: `campaign:example-2026-09`, `clinic`, `referral`. At most 80 characters. |

`get_deal` and `list_deals` always include `tags`, an empty list when the deal has none. `list_deals` accepts `tags` (one or more). A deal must carry every listed tag, and the filter combines with stage, owner, service and parent. `list_tags` returns each tag in use with a `count` of deals.

Writes:

- `create_deal` takes `tags` and sets them on the new row.
- `update_deal` takes `tags` to replace the set (`[]` clears it). `add_tags` and `remove_tags` merge, and run after a replace when both are sent (`remove_tags` wins on overlap). Omitting all three leaves the set alone.
- `update_deal` takes `service_id` or `service_slug` (not both) to move the deal onto an active service. If the current offer is tied to a different service, the call is rejected unless `offer_id` is an offer for the new service, or null to clear the offer. An offer with no service is kept when `offer_id` is omitted. The timeline gets `Service changed: {old slug} -> {new slug}`. Stage, tags, and next action are not changed unless those fields are also sent.
- `bulk_update_deals` takes `add_tags` / `remove_tags` for the same selection it already uses (`deal_ids`, or `service_slug` plus optional stage and owner).
- `bulk_update_deals` changes service for up to 50 open opportunities when `deal_ids` is passed with `set_service_id` or `set_service_slug` (not both), plus an optional `offer_id` under the same offer rule. `service_slug` only filters. A mismatch writes nothing; the batch is one transaction. Each deal whose service id changes gets the same timeline line.
- Lead ingest (`POST /api/v1/leads` and MCP `ingest_leads`) takes an optional `tags` array. A re-run merges those tags onto the open deal. Leaving `tags` off the payload leaves the existing set alone. Entries that are not valid slugs are dropped so one bad label cannot fail the lead.

### `partner_tags`

Labels on a partner. Independent of `deal_tags`: a company can be tagged `google-ads` without that slug appearing on its deals. Same slug rules as deal tags (lowercase letters, digits, hyphens and colons, at most 80 characters). Added in migration 10. No later migration changes the table.

| Column | Type | Notes |
|---|---|---|
| `partner_id` | INTEGER NOT NULL | FK to `partners(id)`. With `tag`, the primary key. |
| `tag` | TEXT NOT NULL | The slug. Examples: `google-ads`, `meta-pixel`, `gtm`, `analytics`, `call-tracking`, `online-booking`. Those names are not special; any valid slug is stored the same way. |

The partners list (`/contacts/partners?tag=`) and MCP `search_partners` (`tag` / `tags`) keep a partner only when it carries every listed tag. That filter uses the partner's own tags. It does not walk up to a parent company. `list_partner_tags` reads one partner's tags. `update_partner_tags` writes them: `tags` replaces (`[]` clears), then `add_tags`, then `remove_tags`. `get_partner` and `search_partners` include `tags` on each partner (and on the parent company, when that row is included). There is no separate HTTP API for partner tags; the admin partner form and MCP are the write paths, same as the rest of partner editing.

An ICP `company_tag` rule does walk up: a company partner matches on its own tags, and a person partner matches on the parent company's tags. A person's own tags do not satisfy `company_tag`.

### `lost_reasons`

Operator configuration, seeded once like pipeline stages.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE NOT NULL | |
| `triggers_nurture` | INTEGER NOT NULL DEFAULT 0 | `mark_lost` with this reason posts the nurture webhook. A later reason change does not post it again. |
| `is_disqualification` | INTEGER NOT NULL DEFAULT 0 | Deals lost with this reason are reported as disqualified. The flag is read when outcome is derived, so changing it changes those deals' outcome without rewriting them. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive reasons stay on old records but leave pickers. |
| `created_at`, `updated_at` | TEXT | |

Seed names: "Not a fit", "No budget", "Not now" (`triggers_nurture = 1`),
"No response", "Lost to competitor", "Duplicate", "Lost stage (migrated)",
and "Disqualified" (`is_disqualification = 1`). The Disqualified row is
inserted with `INSERT OR IGNORE` on its name, then the flag is set, so a
reason already created by hand with that exact name is kept.
Managed at `/definitions/lost-reasons` (and `/admin/lost-reasons`) and via MCP.

`mark_lost` on a deal that is already lost, with a different reason, updates
`lost_reason_id` and logs an activity. `closed_at` stays. The same reason
returns `already_lost`. An empty note on that change leaves `lost_note` as it was.

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
| `field` | TEXT NOT NULL | One of a fixed allowlist (`icp.MATCHABLE_FIELDS`): industry, team size, is-a-company, preferred channel, presence of email / phone / website / social URL, source, value estimate, pain points, goals, plus `company_tag` and `deal_tag`. Not an arbitrary column, so a typo cannot create a rule that never matches. |
| `operator` | TEXT NOT NULL | `is_not_null`, `boolean`, `fuzzy_match` (case-insensitive substring, or a `difflib` ratio of at least 0.6), `eq` / `gt` / `gte` / `lt` / `lte` for numbers, or `has_tag`. `has_tag` is only valid with `company_tag` and `deal_tag`, and those two fields only accept `has_tag`. |
| `value` | TEXT | Ignored for `is_not_null`; `true` or `false` for `boolean`; a number for numeric operators; one tag slug for `has_tag` (stored lowercased, for example `google-ads` or `campaign:spring`). Validated against the operator at write time. |
| `weight` | INTEGER NOT NULL DEFAULT 1 | Points added on a match. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive rules are kept but not scored. |
| `created_at`, `updated_at` | TEXT | |

It is a deterministic weighted-rules formula, not a learned model, and it has no
pass/fail threshold. The call view shows it beside the call-priority score.
`get_lead` puts the same number on `fit` and on `conversion_readiness.fit_score`.
`list_leads` with `sort=fit` orders by it. `get_deal` returns it as `icp`.

A tag rule is one row, one slug, one weight. `company_tag` matches when the
linked company partner has that tag. If the linked partner is a person, the
parent company is the one that is checked, not the person. A lead with no
partner does not match a company-tag rule. `deal_tag` matches when the lead or
deal itself has that tag. Profile fields are unchanged: a lead still scores
those from its own columns, and an opportunity still scores them from the
partner. The tag fields are stored in the existing `field` / `operator` /
`value` columns, so adding them did not bump the schema (still version 14).

MCP: `list_icp_criteria`, `create_icp_criterion`, `update_icp_criterion`,
`delete_icp_criterion`. `list_catalog` includes `icp_fields` and `icp_operators`.

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

Operator configuration. A new database has no rows. A stage change runs
nothing extra until someone adds an automation in the admin UI or over MCP.

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
`(deal_id, tag)` covers lookups by deal), `partner_tags(tag)` (the primary key
on `(partner_id, tag)` covers lookups by partner; migration 10),
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

## Campaign metrics

`app/services/campaign_metrics.py` is a pure function,
`compute_campaign_metrics(campaign, deals, events, today, tz, period=None)`.
It does not read the database or the clock. The caller passes the campaign
window and targets, the deals (each already labelled `open`, `won`, `lost`,
or `disqualified`), and dated events. Every number comes back with the
formula and the inputs that produced it, so a screen can show them on hover.
`value` is `None` when the figure does not exist. It is never a stand-in zero.

Rates use a fixed length: a week is 7 days, a month is 30 days, a year is
365 days. That is not a calendar month. The period table is separate and
uses real calendar weeks (Monday start), months, and years in `tz`.

### Window

`days_total = (deadline - start_date).days + 1`. Both dates count. A start
of 1 September and a deadline of 30 September is 30 days. The day after the
deadline, the campaign has ended.

Days elapsed are the calendar days before today that sit inside the window,
so the start date itself has elapsed 0 and today still counts as a day
remaining. Elapsed + remaining = total. Time percent is
`days_elapsed / days_total * 100`.

| Situation | What is reported |
|---|---|
| No start date | Actuals only. No rates, no time percent, no projection. |
| No deadline | Actuals, and the actual rate once at least one day has elapsed. No required rate, no time percent, no projection. |
| Deadline before the start | Zero-length window. Actuals only. No divide-by-zero. |
| Start and deadline on the same day | A one-day campaign, not a zero-length window. |
| Today before the start | Elapsed 0, time percent 0. Required rate is the full target spread over the whole window. No actual rate yet. |
| Today inside the window, including the deadline | Required rate uses the days still left, including today. |
| Today after the deadline | Final percent against the target. Actual rate uses the whole window. No required rate and no projection. |

Worked example: start 1 September 2026, deadline 30 September 2026, today
16 September 2026. Total 30, elapsed 15, remaining 15, time percent 50.

### Actuals

| Target | Actual |
|---|---|
| Leads | Deals whose outcome is not disqualified. Open, won, and lost all count. |
| Contacts | Events typed `contact`, `contacts`, or `contact_made`. |
| Conversations | Events typed `conversation` or `conversations`. |
| Advanced | Events typed `advanced` or `advance`. |
| Won | Deals with outcome `won`. |
| Won value | Sum of `value` on won deals. A missing value counts as 0. |

Contact, conversation, and advanced targets count events, not unique deals.
A deal is contacted when any of those events names its id. Everyone else is
not yet touched.

Naive event timestamps are UTC, then converted to `tz`. A date-only value is
that calendar date and is not shifted. Campaign start, deadline, and `today`
are calendar dates: a naive datetime keeps its date part.

### Win/loss

Closed deals are won and lost only. Disqualified deals are excluded from the
ratio and the win rate, and so are open deals.

- Ratio is `won : lost`, unreduced (`3 : 9`).
- Win rate is `won / (won + lost) * 100`.
- When `won + lost` is 0, both are `None` with note `no closed deals`. A run
  of losses and no wins is a real 0%, not that note.
- Disqualification rate is `disqualified / deals * 100` over every deal
  passed in. No deals at all is `None` with note `no leads`.
- Lost deals are grouped by `lost_reason`. Disqualified deals are not in
  that group. Won value and lost value are separate sums.

### Pacing

For each target that is set (a missing target is skipped; `0` is a real
target and has no percent complete):

- Remaining is `target - actual` (negative once the target is passed).
- Percent complete is `actual / target * 100`.
- Required rate is `remaining / days_remaining * 7` (or 30, or 365).
- Actual rate is `actual / days_elapsed * 7` (or 30, or 365).
- Projection is `actual / days_elapsed * days_total`, and only while the
  window is still open and at least one day has elapsed.
- Status is ahead when percent complete is above the time percent, behind
  when it is below, on track when they match. `pacing_status` is behind if
  any target is behind, ahead if every comparable target is ahead, and on
  track in between.

Continuing the example, target 30 leads and actual 10: remaining 20, percent
complete `10 / 30 * 100`, required per week `20 / 15 * 7`, required per month 40,
required per year `20 / 15 * 365`, actual per week `10 / 15 * 7`, projection
20, status behind.

### Period breakdown

`period` is `week`, `month`, or `year`. Omit it and `periods` is `None`.

Each row is a whole calendar period. Its required figure is
`target * overlap_days / window_days`, so a week that only partly sits in
the window is prorated and the rows add back to the target. The cumulative
target is that same straight line at the end of the period. The cumulative
actual adds the period actuals. Undated rows are listed beside the table
and are not inside a period; overall actual = last cumulative actual +
undated.

Lead counts in a period use `created_at` and skip disqualified deals. Won,
lost, disqualified, and the period win rate use `closed_at`. A period with
no won or lost deals has win rate `None` and note `no closed deals`, even
if someone was disqualified that period.

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

An optional script `scripts/opportunities_to_leads.py` can move
early-stage opportunities back to leads (`--stage` repeatable, dry-run
by default, `--apply` to write).

**Why an API rather than direct database access:** the database is one sqlite
file with a single-writer assumption. A second process touching it would break
that, so integrations go through the API or MCP.

## Agent interface

`POST /mcp` is Streamable HTTP MCP over the same service functions as the admin
UI. Auth is `CRM_MCP_API_KEY` (fail-closed, header only, separate from the
ingest and stages keys) or an OAuth 2.1 access token. See [`MCP.md`](MCP.md).
