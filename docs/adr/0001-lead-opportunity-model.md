# ADR 0001: Leads and opportunities (Odoo model, leads on)

| Field | Value |
|---|---|
| Status | **Accepted**. This implementation request accepts the ADR using the recommended answers to Q1–Q13. |
| Date | 2026-10-05 |
| Schema impact | Schema version 7 to 8. Three tables are rebuilt. This breaks the "additive-only" rule. Section 6 gives the exception. |
| API impact | Breaking changes to `POST /api/v1/leads` and MCP `ingest_leads`. New MCP tools. Section 7 and section 8 give the list. |
| Review use case | An agent does independent lead-generation research. The agent decides when a lead is worth pursuit as an opportunity. Section 3 gives the walkthrough. |

## 0. How to read this document

This document uses a controlled-language style based on ASD-STE100
(Simplified Technical English). The rules are:

- One term has one meaning. Section 2 gives the terms. Do not use a
  synonym for a term in section 2.
- Sentences are short. A procedure step has one instruction.
- "Must" is a requirement. "Must not" is a prohibition. "Can" is an
  option. "Recommended" is a decision that the reviewer can change.
- Identifiers in `code` are exact names (tables, columns, tools, fields).

Each decision has an identifier (D1, D2, ...). Each open question has an
identifier (Q1, Q2, ...). Use these identifiers in review comments.

## 1. Context

### 1.1 Problem

The current data model has no definition of "lead" and no definition of
"opportunity". The code uses the word "lead" with three meanings:

1. An ingest payload (`app/services/leads.py`).
2. A partner and a deal together (`icp_criteria` fit score).
3. A deal that a call moved to a nurture stage (the `interested` call
   outcome, label "Interested (warm lead)").

Each ingested row immediately creates a partner and a deal. Thus:

- Unverified data goes into `partners`. An agent cannot keep a
  half-researched company away from confirmed contacts.
- Unqualified records are in the pipeline. Pipeline value, deal count and
  win rate include records that nobody has qualified.
- No record shows when or why a record became worth pursuit.
- An agent cannot log research notes for a company before a partner
  exists, because `activities.partner_id` is `NOT NULL`.

### 1.2 Target use case

An agent does lead-generation research without supervision. The agent
collects information in small parts, over more than one session. Examples:
a company name from a directory, a website, then a phone number, then a
contact person, then evidence of a need. At a point, the agent decides one
of these results:

- The lead is worth pursuit. The agent converts it to an opportunity.
- The lead is not a fit. The agent marks it lost, with a reason.
- The lead is not ready. The agent sets a next action and a date, or
  marks it lost with a "not now" reason that starts nurture.

The CRM must keep the research data, the evidence and the decision. The
CRM must not make the decision. `docs/SCOPE.md` says: "Judgement belongs in
the agent that talks to the MCP endpoint."

### 1.3 Reference model

The reference model is Odoo CRM with the "Leads" setting on. In Odoo:

- One model, `crm.lead`, keeps leads and opportunities. A `type` field
  (`lead` or `opportunity`) tells them apart.
- A lead keeps its own contact data. A lead does not need a partner.
- Conversion changes `type` on the same record. Conversion can create a
  partner, link an existing partner, and merge duplicates.
- "Lost" is not a stage. A lost record is archived (`active = False`) and
  has a lost reason.
- "Won" is a stage with the flag `is_won`.

agent-crm already matches parts of this model:

| Odoo | agent-crm now | Match |
|---|---|---|
| `res.partner` (`is_company`, `parent_id`) | `partners` (`is_company`, `parent_id`) | Yes |
| `crm.lead` with `type = 'opportunity'` | `deals` | Yes |
| `crm.lead` with `type = 'lead'` | None | No |
| `crm.stage.is_won` | `pipeline_stages.is_won` | Yes |
| Lost = `active = False` + `lost_reason_id` | `pipeline_stages.is_lost` stage role | No |
| `crm.lost.reason` | None | No |
| `crm.tag` | `deal_tags` | Yes |
| `mail.message` (chatter) | `activities` | Yes |
| `mail.activity` (scheduled action) | `deals.next_action`, `deals.next_action_date` | Partly (one action only) |
| Predictive lead scoring | `icp_criteria` fit score | Same role. Different method. |
| Lead to opportunity wizard | None | No |
| Merge wizard | None | No |

## 2. Terms (target glossary)

These definitions apply after implementation. They replace the "Entities"
section of `docs/DATA_MODEL.md` (section 9 lists the documentation work).

| Term | Definition |
|---|---|
| **Partner** | A person or a company that the operator or the agent has confirmed as a real party. One row in `partners`. Odoo equivalent: `res.partner`. |
| **Company partner** | A partner with `is_company = 1`. |
| **Person partner** | A partner with `is_company = 0`. A person partner can have a parent company partner (`parent_id`). |
| **Deal record** | One row in `deals`. A deal record is a lead or an opportunity. Odoo equivalent: one `crm.lead` record. |
| **Lead** | A deal record with `type = 'lead'`. A lead is an unqualified signal. A lead keeps its own contact data. A lead can exist without a partner and without a service. |
| **Opportunity** | A deal record with `type = 'opportunity'`. An opportunity is the qualified pursuit of one service for one partner. An opportunity must have a partner and a service. Only opportunities use pipeline stages. |
| **Conversion** | The operation that changes a lead into an opportunity. The record keeps its `id`. Conversion sets `type = 'opportunity'` and `date_conversion`. |
| **Open** | A deal record with `active = 1` that is not in an `is_won` stage. |
| **Won** | An opportunity in a stage with `is_won = 1`. |
| **Lost** | A deal record with `active = 0` and a `lost_reason_id`. A lead and an opportunity can both be lost. Lost is not a stage. |
| **Lost reason** | One row in `lost_reasons`. Operator configuration. Examples: "Not a fit", "No budget", "Not now". |
| **Merged** | A lost deal record with `merged_into_id` set. Its data moved into another deal record. The CRM does not delete merged records. |
| **Research note** | An `activities` row with `type = 'research'`. It records one finding and can record its source URL. |
| **Fit score** | The sum of weights of the matching `icp_criteria`. For a lead, the score reads the lead's own fields. For an opportunity, the score reads the partner fields. |
| **Conversion readiness** | A list of the data that conversion needs, with the items that are missing. The CRM calculates it. It is information, not a gate (D9). |
| **Customer** | A partner with one or more won opportunities. This is a calculated value. No column keeps it. |

Usage rules:

- Do not use "deal" alone in new documentation, UI text or tool
  descriptions. Use "lead", "opportunity" or "deal record".
- Existing tool names that contain `deal` stay (D11). Their descriptions
  must say which type they operate on.

## 3. Use case walkthrough (for the reviewer)

The reviewer must assess this ADR against this walkthrough. Each step
shows the tool call and the data that changes. Section 8 defines the tools.

### Step 1: The agent finds a candidate

The agent finds "Harbour Physio" in a directory. It has a name and a
website only.

1. The agent calls `find_duplicates` with `company_name` and `website`.
2. The CRM returns zero matches.
3. The agent calls `create_lead`:

```json
{
  "company_name": "Harbour Physio",
  "website": "https://harbourphysio.example",
  "source": "research:directory",
  "tags": ["campaign:physio-illawarra-2026-10"]
}
```

4. The CRM creates a deal record with `type = 'lead'`,
   `partner_id = NULL`, `service_id = NULL`. No partner is created.

Result: the pipeline and `partners` do not change.

### Step 2: The agent adds data in parts

In a later session, the agent reads the website.

1. The agent calls `update_lead` with `phone`, `industry` and `team_size`.
   The default is fill-empty. The CRM does not overwrite a value that is
   already there.
2. The agent calls `log_activity` with `deal_id`, `type = 'research'`,
   `source_url` and a body: "Website says they plan a second clinic in
   2027. Booking is by phone only."

In another session, the agent finds the practice manager on LinkedIn.

3. The agent calls `update_lead` with `contact_name`, `title`, `email` and
   `linkedin_url`.
4. The agent calls `log_activity` with the evidence and the source URL.

Result: the lead has its contact data and an evidence log. `partners`
still does not change.

### Step 3: The agent assesses the lead

1. The agent calls `get_lead`. The CRM returns:
   - the lead fields,
   - the research notes,
   - the fit score with the matched and unmatched criteria,
   - `possible_duplicates` (leads, opportunities and partners),
   - `conversion_readiness`, for example
     `{"ready": false, "missing": ["service"]}`.
2. The agent applies its own judgement. The CRM does not decide.

### Step 4a: The agent converts the lead

1. The agent calls `convert_lead`:

```json
{
  "lead_id": 412,
  "service_slug": "online-booking",
  "partner_action": "create",
  "value_estimate": 4800,
  "expected_close": "2026-12-15",
  "next_action": "Call practice manager",
  "next_action_date": "2026-10-08",
  "note": "Fit 7/9. Second clinic planned. Phone-only booking is the pain."
}
```

2. The CRM creates a company partner from `company_name` and a person
   partner from `contact_name`, with `parent_id` set to the company.
3. The CRM sets `partner_id` (to the person partner), `service_id`,
   `type = 'opportunity'`, `date_conversion`, and the stage to the default
   stage.
4. The CRM writes a `system` activity with the note and the fit score at
   conversion time.
5. The opportunity is now in the pipeline. It can go into the call queue.

### Step 4b: The agent rejects the lead

1. The agent calls `mark_lost` with `deal_id = 412`,
   `lost_reason = "Not a fit"` and a note.
2. The CRM sets `active = 0` and `lost_reason_id`. The lead leaves the
   default lists. The data and the research notes stay.

### Step 4c: The lead is not ready

Option 1: the agent calls `update_lead` with `next_action` and
`next_action_date`. The lead comes back in `list_due_followups`.

Option 2: the agent calls `mark_lost` with a lost reason that has
`triggers_nurture = 1` (for example "Not now"). The CRM sends the nurture
webhook if the lead has an email address.

### Step 5: A duplicate appears

A later ingest job sends the same company with a different source.

1. The CRM finds the open lead by a strong key: email, or phone and name,
   or website and name (section 5.6).
2. The CRM fills the empty fields on the existing lead. It merges tags.
3. The CRM returns `status = "duplicate_open_lead"` and the `lead_id`.

If the agent finds two leads for one company, it calls `merge_leads`. The
CRM moves the data, research notes, tags and tasks to the target lead. It
marks the other lead as merged (lost, with `merged_into_id`).

### Reviewer questions for this use case

- R1. Can the agent do steps 1 to 5 with the tools in section 8 only?
- R2. Does each step keep unverified data out of `partners`?
- R3. Does the agent have enough information at step 3 to decide? Is a
  field or a tool missing?
- R4. Is the decision recorded so that a person can audit it later (who,
  when, why, evidence)?
- R5. Can an ingest job and a research agent work on the same lead
  without data loss?

## 4. Decisions

| ID | Decision | Alternative that we did not select |
|---|---|---|
| D1 | Leads and opportunities are in one table, `deals`, with a `type` column. Conversion changes the type on the same row. | A separate `leads` table (Salesforce model). Rejected: conversion copies data and changes ids. Activities and tags would need two foreign keys. |
| D2 | The table name stays `deals`. The UI and the documentation say "lead" and "opportunity". | Rename to `leads` (Odoo name). Rejected: a table rename breaks every query and gives no function. |
| D3 | A lead keeps its own contact columns on `deals`. `deals.partner_id` becomes nullable. | Keep the partner requirement for leads. Rejected: unverified data would continue to go into `partners`. This is the main problem in 1.1. |
| D4 | `deals.service_id` becomes nullable. A lead can have no service. Conversion requires a service. | Keep the requirement. Rejected: a research agent often does not know the service when it creates the lead. |
| D5 | `activities.partner_id` and `delegated_tasks.partner_id` become nullable. A row must have a `partner_id` or a `deal_id`, or both. The service layer checks this rule. | Keep the requirement. Rejected: research notes and tasks on a lead without a partner would be impossible. |
| D6 | "Lost" is `active = 0` plus `lost_reason_id`, for leads and opportunities. The `is_lost` stage role is deprecated (section 6.6). | Keep a lost stage. Rejected: leads do not use stages, so a lead could not be lost. |
| D7 | Lost reasons are operator configuration in a new table `lost_reasons`. A lost reason can have `triggers_nurture = 1`. | Free-text reason only. Rejected: reasons could not be counted or configured. |
| D8 | Merge does not delete. The source record becomes lost with `merged_into_id`. | Delete the source (Odoo behaviour). Rejected: the MCP interface must not delete records (`docs/MCP.md`). |
| D9 | The CRM calculates conversion readiness and the fit score. The CRM does not enforce a fit threshold. Conversion has hard requirements only for identity and service (section 5.4). | A configurable minimum fit score for conversion. Rejected for now: `docs/SCOPE.md` puts judgement in the agent. See Q3. |
| D10 | For an opportunity, the partner is the source of truth for contact data. The contact columns on the deal record stay as a historical snapshot of the lead. | Synchronize both directions (Odoo behaviour). Rejected: two sources of truth for one value. |
| D11 | Existing MCP tools and REST endpoints keep their names. Tools that read deal records default to `type = 'opportunity'`. | Rename to `*_opportunity`. Rejected: this breaks every connected agent with no benefit. |
| D12 | All existing deals become opportunities in the migration. No existing deal becomes a lead automatically. An optional script can change early-stage opportunities to leads (section 6.5). | Automatic change of `new` and `contacted` deals to leads. Rejected: stage meaning is operator configuration. The migration must not guess it. |
| D13 | A deal record can be created as an opportunity directly (`create_deal`, or ingest with `type = "opportunity"`). This skips the lead step. | Require every record to start as a lead. Rejected: referrals and inbound requests are often qualified on arrival. Odoo also permits this. |
| D14 | Leads do not use pipeline stages. A lead keeps `stage` (the column is `NOT NULL`), but the value has no meaning while `type = 'lead'`. Stage tools reject leads. | A separate lead status column (New, Working, ...). Rejected: Odoo does not use one. `active`, `next_action_date` and research notes give the same information. See Q4. |

## 5. Target data model

### 5.1 `deals` (rebuilt)

Existing columns stay with the same names and the same meaning. The
changes are:

| Column | Type | Change | Notes |
|---|---|---|---|
| `partner_id` | INTEGER | **Now nullable** | FK to `partners(id)`. Required when `type = 'opportunity'`. |
| `service_id` | INTEGER | **Now nullable** | FK to `services(id)`. Required when `type = 'opportunity'`. |
| `type` | TEXT NOT NULL DEFAULT 'opportunity' | New | `lead` or `opportunity`. The service layer validates it. |
| `name` | TEXT | New | Short title, for example "Harbour Physio: online booking". Optional. The UI shows company name or contact name if it is empty. |
| `contact_name` | TEXT | New | Lead contact data. Odoo `contact_name`. |
| `company_name` | TEXT | New | Odoo `partner_name`. |
| `email` | TEXT | New | Odoo `email_from`. |
| `phone` | TEXT | New | |
| `website` | TEXT | New | |
| `title` | TEXT | New | Job title of the contact. Odoo `function`. |
| `address` | TEXT | New | |
| `linkedin_url`, `x_url`, `instagram_url`, `facebook_url`, `youtube_url` | TEXT | New | Same meaning as on `partners`. |
| `industry` | TEXT | New | |
| `team_size` | INTEGER | New | Non-negative. Bad input becomes null. |
| `preferred_channel` | TEXT | New | `email`, `phone` or `text`. |
| `probability` | INTEGER | New | 0 to 100. Set by the agent or the operator. Not calculated (D9, `docs/SCOPE.md`). |
| `priority` | INTEGER NOT NULL DEFAULT 0 | New | 0 to 3. Odoo stars. |
| `expected_close` | TEXT | New | ISO date. Odoo `date_deadline`. |
| `date_conversion` | TEXT | New | ISO timestamp. Set once by `convert_lead`. Null for records created as opportunities. |
| `active` | INTEGER NOT NULL DEFAULT 1 | New | 0 = lost (D6). |
| `lost_reason_id` | INTEGER | New | FK to `lost_reasons(id)`. Required when `active = 0`. |
| `lost_note` | TEXT | New | Free text for the loss. |
| `merged_into_id` | INTEGER | New | FK to `deals(id)`. Set by `merge_leads` on the source record. |
| `stage` | TEXT NOT NULL DEFAULT 'new' | Meaning changes | Has meaning only when `type = 'opportunity'` (D14). |
| `closed_at` | TEXT | Meaning extends | Set when an opportunity enters an `is_won` stage, or when any deal record becomes lost. Cleared on restore. |

Service-layer invariants (the database does not enforce these):

- I1. `type = 'opportunity'` requires `partner_id` and `service_id`.
- I2. `active = 0` requires `lost_reason_id`.
- I3. `merged_into_id` requires `active = 0`, and must not point to the
  same row or to a row that is itself merged.
- I4. A lead must have one or more of: `email`, `phone`, `website`,
  `linkedin_url`, `company_name`, `partner_id`.
- I5. `parent_deal_id` is permitted only when `type = 'opportunity'`.

### 5.2 `lost_reasons` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE NOT NULL | |
| `triggers_nurture` | INTEGER NOT NULL DEFAULT 0 | When a deal record becomes lost with this reason, the CRM sends the nurture webhook. |
| `active` | INTEGER NOT NULL DEFAULT 1 | Inactive reasons stay on old records but leave the pickers. |
| `created_at`, `updated_at` | TEXT | |

Seed data (one time, then operator data, like `pipeline_stages`):
"Not a fit", "No budget", "Not now" (`triggers_nurture = 1`),
"No response", "Lost to competitor", "Duplicate" (used by merge),
"Lost stage (migrated)" (used by section 6.3).

### 5.3 `activities` and `delegated_tasks` (rebuilt)

| Table | Change |
|---|---|
| `activities` | `partner_id` becomes nullable (D5). New column `source_url TEXT`. New allowed `type` value: `research`. |
| `delegated_tasks` | `partner_id` becomes nullable (D5). |

On conversion, the CRM sets `partner_id` on each `activities` row and each
`delegated_tasks` row of the lead where `partner_id` is null.

### 5.4 Conversion rules (`convert_lead`)

Hard requirements. If one fails, conversion stops and the CRM returns an
error code. No data changes.

| Rule | Error code |
|---|---|
| The record exists and `type = 'lead'`. | `not_a_lead` |
| The record is active. | `lead_lost` (restore it first) |
| A service is known (on the lead, or in the call). | `missing_service` |
| The partner is resolved (section 5.5). | `partner_ambiguous` (the response lists the candidates) |
| No other open opportunity has the same partner and service. | `duplicate_open_opportunity` (the response gives its id; use `merge_leads`) |

Conversion procedure (one transaction):

1. Resolve the partner (section 5.5).
2. Copy the lead contact fields to the partner. Use fill-empty. Do not
   overwrite partner values.
3. Set `partner_id`, `service_id`, `type = 'opportunity'`,
   `date_conversion`, and `stage` (the value in the call, else the stage
   with `is_default = 1`).
4. Apply the optional fields in the call (`value_estimate`,
   `expected_close`, `probability`, `offer_id`, `next_action`,
   `next_action_date`, `owner`). `owner` writes `owner_key`. The tool also
   accepts `owner_key`, as the existing tools do (`_owner_arg`).
5. Set `partner_id` on the activities and tasks of the lead (section 5.3).
6. Write one `system` activity. It records: the conversion, the fit score
   and the matched criteria at this time, the partner action, and the
   note from the call.

### 5.5 Partner resolution at conversion

`partner_action` has these values:

| Value | Behaviour |
|---|---|
| `link` | Use `partner_id` from the call. The partner must exist. |
| `create` | Create the partners. If `company_name` is set, find or create a company partner by exact name. If `contact_name` is set, create a person partner with `parent_id` set to the company. The deal record points to the person partner, else to the company partner. |
| `auto` (default) | Run the match order that ingest uses now: email, then phone + name, then website + name, then exact name in the same parent. One match: link it. Zero matches: create. More than one match: stop with `partner_ambiguous`. |

The match code moves from `app/services/leads.py` to a shared function in
`app/services/partners.py`. Ingest no longer calls it (section 7.1).

### 5.6 Duplicate detection

`find_duplicates` and ingest use the same function. It compares a set of
fields with open deal records and with partners.

Website comparison uses `_website_key()` in `app/services/partners.py`:
lowercase, strip, remove `http://` or `https://`, remove a leading
`www.`, remove a trailing `/`. It does not remove the path. So
`https://example.com/page` and `https://example.com` do not match.

**Strong keys** (ingest auto-merge, and the first results from
`find_duplicates`). These are the keys that ingest uses now for a
partner. A lead ingest must not be weaker:

1. Email (lowercase, trimmed).
2. Phone (digits only, `phone_digits()` in `app/services/phone.py`) **and**
   name (case-insensitive). Name is `company_name` or `contact_name` or
   `name`, the same as ingest uses now.
3. Website key **and** name (same name rule as key 2).

**Weak keys** (`find_duplicates` only; the agent decides; ingest does not
auto-merge):

4. Phone digits only (no name). A shared reception number is a weak match.
5. Website key only (no name). Skip a shared-host denylist:
   `facebook.com`, `instagram.com`, `linkedin.com`, `linktr.ee`,
   `wixsite.com`, `squarespace.com`, `square.site`, `wordpress.com`,
   `google.com`, `youtu.be`, `youtube.com`.
6. LinkedIn URL (lowercase, without the query string).
7. Exact company name.

Each result has `kind` (`lead`, `opportunity` or `partner`), `id`,
`matched_on` (the list of keys that matched) and `strength`
(`strong` or `weak`). Ingest uses only the strong keys for an automatic
match (section 7.1).

### 5.7 Fit score input

`icp.py` builds the scored values from these sources:

| Field group | Lead (`type = 'lead'`) | Opportunity |
|---|---|---|
| `industry`, `team_size`, `is_company`, `preferred_channel`, `email`, `phone`, `website`, social URL | The lead's own columns. `is_company` is true if `company_name` is set and `contact_name` is empty. | The partner (now). |
| `source`, `value_estimate`, `pain_points`, `goals` | The deal record (now). | The deal record (now). |

A lead always scores from its own columns. A linked `partner_id` does
not fill empty lead fields for the score (D10: one source of truth).
`get_lead` still returns the partner as a separate object when
`partner_id` is set.

### 5.8 Code that reads deal records

D3, D4 and D6 change two assumptions in the current read code. P1 must
change these places before a lead or a lost record can exist:

- `list_deals()` in `app/services/deals.py` and the queue query in
  `app/services/call_queue.py` use `JOIN partners` and `JOIN services`.
  An inner join drops each row with a null `partner_id` or `service_id`.
  Use `LEFT JOIN` where a lead can be in the result (`list_deals` with
  `type = 'lead'` or `'all'`, `list_due_followups`).
- `list_deals()` is also the admin board query (`app/routers/admin.py`).
  It must default to `type = 'opportunity'` and `include_lost = false`,
  the same as the MCP tool (section 8.2). Leads must not appear in a
  stage column (D14). The board inherits these defaults; P1 does not
  need a separate admin change.
- `get_deal_tool` and `create_delegated_task_tool` call
  `get_partner(deal["partner_id"])` and `get_service(deal["service_id"])`.
  These calls must accept a null id and return null.
- `pipeline_stages.closed_stage_keys()` (`is_won` or `is_lost` stages) is
  the "open" test in `get_open_deal_for_partner_service()` and in
  `app/services/staleness.py`. After D6, "open" is `active = 1` and the
  stage is not an `is_won` stage (section 2). Each caller must use the new
  test. A lead with `active = 1` is open.

### 5.9 Indexes

New: `deals(type, active)`, `deals(email)`, `deals(phone)`,
`deals(website)`, `deals(lost_reason_id)`, `deals(merged_into_id)`.
Recreated after the rebuild: all existing indexes on `deals`,
`activities` and `delegated_tasks` (section 6.2, step 7 lists them).

## 6. Database migration plan

### 6.1 Rule exception

`app/database.py` says: "Additive-only. Do not DROP or RENAME columns in
this series." SQLite cannot remove `NOT NULL` from a column with
`ALTER TABLE`. Thus D3, D4 and D5 need a table rebuild.

This ADR asks the reviewer to approve one exception:

- The exception applies to `migrate_008` only.
- The rebuild must not drop a column, rename a column or change the
  meaning of a column. It only removes `NOT NULL` and adds columns.
- `_SCHEMA_V1` must not change. A new database runs `migrate_001` to
  `migrate_008` in sequence, as now.
- The comment in `app/database.py` must be updated to say: "Additive-only,
  except a reviewed table rebuild that only relaxes constraints. See
  `docs/adr/0001-lead-opportunity-model.md`."

### 6.2 `migrate_008` procedure

`init_db()` already does these things. The migration must use them and
must not change them:

- It takes the file lock and `BEGIN IMMEDIATE`.
- It makes an online backup before the migration:
  `pre-migrate-v7-to-v8-<timestamp>.db`.
- It sets `PRAGMA user_version` after a successful migration.

`get_db()` does not set `PRAGMA foreign_keys = ON`. Thus foreign keys are
not enforced, and `DROP TABLE` does not cascade. Step 0 checks this.

Do these steps for each table: `deals`, then `activities`, then
`delegated_tasks`.

0. Read `PRAGMA foreign_keys`. If the value is 1, stop the migration with
   an error. (A future change that turns on foreign keys must update this
   procedure.) Run `PRAGMA foreign_key_check(<table>)` for `deals`,
   `activities`, `delegated_tasks` and `deal_tags`, and keep the result
   (see step 11). Do this step one time, before the first table.
1. Read the current `sqlite_sequence.seq` value for the table. Keep it.
2. Create `<table>_new` with the target DDL (section 5).
3. Copy the rows: `INSERT INTO <table>_new (<column list>) SELECT
   <column list> FROM <table>`. Use an explicit column list. Do not use
   `SELECT *`. Older databases have columns in a different order, because
   `_apply_additive_columns` added them with `ALTER TABLE`.
4. Compare the row counts. If they are different, raise an error. The
   transaction rolls back.
5. `DROP TABLE <table>`.
6. `ALTER TABLE <table>_new RENAME TO <table>`.
   - Do not rename the old table first. Since SQLite 3.26, a rename of
     `deals` to `deals_old` also changes the foreign keys in
     `activities`, `deal_tags` and `delegated_tasks` to point to
     `deals_old`.
7. Create all indexes for the table again (the existing names, plus
   section 5.9). The existing names are: `idx_deals_partner`,
   `idx_deals_stage`, `idx_deals_owner`, `idx_deals_parent`,
   `idx_activities_partner`, `idx_activities_deal`,
   `idx_delegated_tasks_deal`, `idx_delegated_tasks_owner_status`.
8. Set `sqlite_sequence.seq` for the table to the value from step 1.
   This prevents the reuse of an `id` from a deleted row. `DROP TABLE`
   removes the row for the old table. Step 3 creates a row only if it
   copied one or more rows. Thus use an upsert: update the row if it
   exists, else insert it. Skip this step if step 1 found no row.

After the three tables:

9. Create `lost_reasons` and insert the seed rows (section 5.2).
10. Backfill (section 6.3).
11. Run `PRAGMA foreign_key_check(<table>)` for `deals`, `activities`,
    `delegated_tasks` and `deal_tags`. If it returns a row that step 0 did
    not return, raise an error. Compare rows on `(table, rowid, parent)`
    only. Do not compare `fkid`: the rebuild adds foreign keys, so the
    `fkid` of an existing foreign key can change. The `rowid` does not
    change, because each rebuilt table keeps `id` as its `INTEGER PRIMARY
    KEY`. Foreign keys were never enforced, so a
    live database can already have broken references (for example a
    manual edit, or a v0 file). Those rows must not stop the start-up of
    the app. Log them as a warning.
12. Run `PRAGMA integrity_check`. If the result is not `ok`, raise an
    error.

### 6.3 Data backfill

| Data | Action |
|---|---|
| All rows in `deals` | `type = 'opportunity'`, `active = 1`, `priority = 0`. Contact columns stay null (D10). |
| Rows in `deals` whose `stage` is a stage with `is_lost = 1` | `active = 0`, `lost_reason_id` = "Lost stage (migrated)". `stage` does not change. If `closed_at` is null, set it from `updated_at`. |
| `activities`, `delegated_tasks` | No change to data. `source_url` is null. |

Result: every existing record keeps its id, partner, service, stage, tags,
activities and tasks.

### 6.4 Behaviour change after the migration

| Area | Before | After |
|---|---|---|
| Deals list, pipeline, `list_deals` | Show deals in the `lost` stage. | Hide lost records by default. `include_lost=true` shows them (section 8.2). The admin board uses `list_deals()`, so the Lost column is empty unless the operator uses the lost filter (P6). |
| Ingest (`POST /api/v1/leads`, `ingest_leads`, `inject_leads.py`) | Creates a partner and a deal. | Creates a lead only (section 7.1). |
| Client import (`import_clients.py`) | Creates a partner and a deal. | Unchanged default: still a partner and an opportunity. `--type lead` is opt-in. |
| Everything else | | No change for existing records, because they are all opportunities. |

### 6.5 Optional script: early opportunities to leads

`scripts/opportunities_to_leads.py` (new, optional, operator runs it).

- Arguments: `--stage <key>` (one or more), `--dry-run` (default on),
  `--apply`.
- It selects open opportunities in the given stages that have no won
  child and no `parent_deal_id`.
- For each record it: copies the partner contact fields into the lead
  columns, sets `type = 'lead'`, and keeps `partner_id` (a lead can have
  a partner).
- It writes one `system` activity on each changed record.
- It prints the ids and a count.
- The migration does not run this script (D12).

### 6.6 `is_lost` stage role deprecation

| Release | Behaviour |
|---|---|
| Schema 8 release | Existing `is_lost` stages still work. A move into an `is_lost` stage also applies `mark_lost` with reason "Lost stage (migrated)". `create_stage` refuses `is_lost = 1`. `update_stage` refuses a change of `is_lost` from 0 to 1. It accepts `is_lost = 1` on a stage that already has it, because the admin stage form (`edit_stage_submit`) sends every role flag on each save; else a label edit on the seeded `lost` stage would fail. REST and the admin form inherit the rule. `list_catalog` and `GET /api/v1/stages` mark `is_lost` as deprecated. |
| A later release (separate ADR or issue) | The column stays (additive rule). The seed `lost` stage can remain as a compatibility shim until an operator deletes it. |

### 6.7 Rollback

1. Stop the app and the `staleness-cron` service.
2. Restore the automatic backup:
   `python -m app.backup --restore <pre-migrate-v7-to-v8-*.db> --restore-dest <live db>`.
3. Deploy the previous image (schema 7).

The previous code refuses a schema 8 database (`SchemaVersionError`). Thus
a restore is the only rollback. Records created after the migration are
lost on rollback. The release notes must say this.

### 6.8 Migration tests

| Test | Expected result |
|---|---|
| Migrate a schema 7 fixture with deals, activities, tasks, tags and a lost-stage deal. | Row counts and ids are the same. All FKs resolve. Tags still point to the correct deals. |
| Migrate a version-0 fixture whose `deals` columns are in the old order. | Column values go to the correct columns. |
| Delete the highest deal id before the migration, then insert a new deal after it. | The new id is higher than the deleted id. |
| A lost-stage deal. | `active = 0`, reason "Lost stage (migrated)". |
| A lost-stage deal whose `closed_at` is null. | `closed_at` equals that row's `updated_at`. |
| A schema 7 fixture with a deal whose `offer_id` points to a missing offer. | The migration completes. The log has a warning. |
| A schema 7 fixture where all deals were deleted (`sqlite_sequence` has a value, the table is empty). | A new deal after the migration gets an id higher than the old `seq`. |
| Restore a migrated lost-stage deal. | `active = 1`, `stage` is the default stage. |
| Edit the label of the seeded `lost` stage in the admin form (the form sends `is_lost = 1`). | The save succeeds. Setting `is_lost = 1` on a stage that does not have it is refused. |
| Force an error after step 5. | The transaction rolls back. The schema stays at 7. The tables are as before. |
| Run `init_db()` two times. | The second run does nothing. |
| A new empty database. | It reaches schema 8. The schema is equal to a migrated database (compare `sqlite_master`). |

## 7. REST API changes

### 7.1 `POST /api/v1/leads` (breaking)

Auth, rate limit, body cap and the 100-lead limit do not change.

Request, per lead. New and changed keys:

| Key | Change |
|---|---|
| `type` | New. `lead` (default) or `opportunity`. `opportunity` uses the current behaviour: it creates or matches a partner and creates an opportunity (D13). |
| `service_slug` | Optional for `type = lead`. Required for `type = opportunity`. If it is given, it must exist (`invalid_service`). |
| `name` | For `type = lead`, it fills `contact_name`. It is not required if `company_name` is set. |
| `company_name` | For `type = lead`, it fills the `company_name` column. It does not create a company partner. |
| `is_company` | For `type = lead`, a true value puts `name` into `company_name` and leaves `contact_name` empty. |
| `probability`, `priority`, `expected_close` | New. Optional. |
| All other current keys | Same names. For `type = lead`, contact keys go to the lead columns. |

Validation for `type = lead`: one of `name` or `company_name` is required,
and invariant I4 applies. Else the status is `invalid`.

Behaviour for `type = lead`:

1. Run duplicate detection (section 5.6) with the **strong** keys
   against open deal records (leads and opportunities). Do not match
   partners automatically. Do not auto-merge on a weak key.
2. One open match: fill the empty fields on that record and merge the
   tags. Status `duplicate_open_lead` (if the match is a lead) or
   `duplicate_open_opportunity` (if the match is an opportunity). For an
   opportunity, the fill-empty goes to the deal fields only, not to the
   contact columns (D10).
3. More than one open match: use the oldest record. Add the other ids to
   `also_matched`.
4. No match: create a lead. If the email matches exactly one partner, set
   `partner_id` on the lead (Odoo behaviour: a lead from a known
   contact). Do not change the partner. Status `created`.

Response, per lead:

```json
{
  "email": "pm@harbourphysio.example",
  "status": "created",
  "type": "lead",
  "deal_id": 412,
  "lead_id": 412,
  "partner_id": null,
  "also_matched": []
}
```

| Status | Meaning | Change |
|---|---|---|
| `created` | A new lead (or opportunity, for `type = opportunity`). | Meaning extended |
| `duplicate_open_lead` | Matched an open lead. Fill-empty applied. | New |
| `duplicate_open_opportunity` | Matched an open opportunity. Fill-empty applied. | New |
| `existing_partner_new_deal` | `type = opportunity` only. Unchanged. | Unchanged |
| `duplicate_open_deal` | `type = opportunity` only. Unchanged. | Unchanged |
| `invalid`, `invalid_service` | Unchanged. | Unchanged |

`lead_id` is equal to `deal_id` when `type = lead`. `lead_id` is null when
`type = opportunity`.

When a request has no `type` key, the default is `lead`. For one
release, REST also sets the header
`Deprecation: type omitted; default is now lead` and each item in the
response includes `"type_defaulted": true`. Do not write a `system`
activity per ingested row (that would flood the timeline).

CLI `scripts/inject_leads.py`: same changes. A new flag
`--type opportunity` keeps the old behaviour for existing jobs. The exit
code is 0 when every lead has a status other than `invalid` and
`invalid_service`. The current allow-list `SUCCESS_STATUSES` must gain
`duplicate_open_lead` and `duplicate_open_opportunity`. Else a successful
match would exit 1.

`scripts/import_clients.py` (`app/services/client_import.py`) creates
confirmed clients. It keeps the current behaviour: it creates or matches
a partner and creates an opportunity. A `--type lead` flag is opt-in for
research-style markdown. It does not inherit the ingest default.

Migration note for operators: an ETL job that needs the old behaviour
must send `"type": "opportunity"`. The CHANGELOG must say this under
"Changed" with the word **Breaking**.

### 7.2 Stages API

`GET /api/v1/stages` and `GET /api/v1/stages/{key}`: each stage gets
`"deprecated_fields": ["is_lost"]` when `is_lost` is true.

`POST /api/v1/stages` refuses `is_lost = 1`, and `PATCH
/api/v1/stages/{key}` refuses a change of `is_lost` from 0 to 1, with
`invalid_stage_role` (section 6.6). Existing `is_lost` stages stay
as they are until an operator clears the flag or deletes an empty stage.
A move of a deal into an existing `is_lost` stage still applies
`mark_lost` (section 6.6).

### 7.3 Lost reasons API

Not added in REST. Lost reasons are managed in `/admin/lost-reasons` and
with MCP (section 8.1). Q6 asks if REST needs them.

## 8. MCP tool changes

### 8.1 New tools

| Tool | Arguments | Returns | Errors |
|---|---|---|---|
| `create_lead` | Contact fields (5.1), `name`, `service_slug`, `source`, `tags`, `pain_points`, `goals`, `value_estimate`, `probability`, `priority`, `expected_close`, `next_action`, `next_action_date`, `owner`, `external_ref`, `partner_id` (optional). | The lead, and `possible_duplicates` (5.6). It creates the lead also when duplicates exist. | `invalid` (I4), `invalid_service`, `partner_not_found` |
| `update_lead` | `lead_id`, any field from `create_lead`, `fill_empty_only` (default `true`), `tags` / `add_tags` / `remove_tags`. | The updated lead. | `not_found`, `not_a_lead`, `lead_lost` |
| `get_lead` | `lead_id` | The lead, `tags`, recent activities (research notes first), `last_researched_at` (max `occurred_at` of `type = 'research'`), `research_note_count`, `fit` (score, matched, unmatched), `possible_duplicates`, `conversion_readiness`. | `not_found`, `not_a_lead` |
| `list_leads` | `query` (name, company, email, phone, website), `owner`, `service_slug`, `tags`, `source_prefix`, `min_fit`, `due_only`, `include_lost` (default `false`), `sort` (`fit`, `created`, `updated`, `next_action_date`, `last_research`), `limit` (max 50). | Leads with `fit_score`, `last_researched_at`, `research_note_count` and a short `conversion_readiness`. | |
| `find_duplicates` | `lead_id`, or a set of contact fields. | Matches with `kind`, `id`, `matched_on`. | `not_found` |
| `convert_lead` | `lead_id`, `partner_action` (`auto`, `link`, `create`), `partner_id`, `service_slug`, `stage`, `value_estimate`, `expected_close`, `probability`, `offer_id`, `next_action`, `next_action_date`, `owner`, `note`. | The opportunity, the partner, `partner_created` (bool), the `system` activity id. | See 5.4 |
| `mark_lost` | `deal_id` (lead or opportunity), `lost_reason_id` or `lost_reason` (name), `note`. | The record. `nurture` result when the reason has `triggers_nurture`. | `not_found`, `already_lost`, `invalid_lost_reason` |
| `restore_deal` | `deal_id` | The record with `active = 1`. Clears `lost_reason_id`, `lost_note`, `closed_at`. If the record is an opportunity in an `is_lost` stage (section 6.3), it also moves the stage to the stage with `is_default = 1`. Else the record would be open and in a lost stage at the same time. Writes a `system` activity. | `not_found`, `not_lost`, `merged` (a merged record cannot be restored) |
| `merge_leads` | `target_id`, `source_ids` (max 10). Target and sources are leads, or the target is an opportunity and the sources are leads. | The target, and the ids that were merged. | `not_found`, `invalid_merge` (an opportunity as a source, a lost record, the same id) |
| `list_lost_reasons` / `create_lost_reason` / `update_lost_reason` | `name`, `triggers_nurture`, `active` | The reason(s). | `duplicate_name` |

`conversion_readiness` shape:

```json
{
  "ready": false,
  "missing": ["service"],
  "warnings": ["possible_duplicate_partner", "no_research_notes"],
  "fit_score": 7,
  "fit_max": 9
}
```

`missing` lists only hard requirements from 5.4 that the CRM can check
before the call: `service`, `identity` (I4 and partner resolution). The
`warnings` list is information for the agent. Values:
`possible_duplicate_partner`, `possible_duplicate_lead`,
`no_research_notes`, `no_contact_person`, `no_email_or_phone`.

`merge_leads` procedure (one transaction): fill-empty from each source to
the target, in order of `created_at`. Move `activities`, `deal_tags` and
`delegated_tasks` to the target. Set each source to lost with reason
"Duplicate" and `merged_into_id = target_id`. Write one `system` activity
on the target with the source ids.

- `deal_tags` has the primary key `(deal_id, tag)`. A tag that the target
  already has must not cause an error. Use `INSERT OR IGNORE` for the
  target, then delete the rows of the source.
- If the target has a `partner_id`, set it on each moved `activities` and
  `delegated_tasks` row where `partner_id` is null (same rule as 5.3).

### 8.2 Changed tools

| Tool | Change |
|---|---|
| `ingest_leads` | Same changes as REST (7.1). |
| `list_deals` | New arguments: `type` (default `opportunity`; `lead` and `all` are permitted) and `include_lost` (default `false`). Each row gets `type`, `active`, `lost_reason`, `probability`, `priority`, `expected_close`, `date_conversion`. **Breaking:** lost records are hidden by default. |
| `get_deal` | Works for leads and opportunities. Returns the new fields. For a lead it also returns the lead contact columns. |
| `create_deal` | Creates an opportunity (D13). Unchanged requirements. Accepts `probability`, `priority`, `expected_close`. |
| `update_deal` | Accepts the new fields. Rejects `type`. (Use `convert_lead`.) |
| `set_deal_stage`, `record_call_outcome` | Reject a lead with `not_an_opportunity`. Reject a lost record with `deal_lost`. A move into an `is_lost` stage also applies `mark_lost` (6.6). The `not_interested` outcome uses `mark_lost` with reason "Not a fit" instead of the `is_lost` stage. |
| `bulk_update_deals` | Selects opportunities only. Gets `mark_lost` (with `lost_reason`) as a bulk action. |
| `get_call_queue` | Opportunities only, `active = 1`. No other change. See Q2 for leads. |
| `list_due_followups` | Includes leads and opportunities. Each row has `type`. New argument `type`. |
| `log_activity` | `partner_id` is optional when `deal_id` is given. Then the CRM uses the `partner_id` of the deal record (it can be null for a lead). The current `deal_mismatch` check applies only when the call gives a `partner_id` and the deal record has one. New type `research`. New argument `source_url` (http or https only, max 2,000 characters). |
| `list_activities` | Accepts `deal_id` for a lead without a partner. |
| `create_delegated_task` | Accepts a lead. `partner_id` is null if the lead has no partner. The `partner` in the response is null. |
| `list_delegated_tasks`, `get_delegated_task`, `update_delegated_task`, `complete_delegated_task` | `partner_id` can be null. No other change. |
| `set_deal_owner` | Works for leads and opportunities. Rejects a lost record with `deal_lost`. |
| `list_tags` | Counts tags on leads and opportunities. Lost records are not counted. |
| `get_partner` | Adds `leads` (open leads with this `partner_id`) and `is_customer`. |
| `list_catalog` | Adds `lost_reasons`. Adds `research` to the activity types. Marks `is_lost` as deprecated. |
| `search_partners` | No change. Leads are not partners. Use `list_leads` with `query`. |

### 8.3 Webhook payload changes

| Webhook | Change |
|---|---|
| Nurture | It can now fire from `mark_lost` with a `triggers_nurture` reason. New fields: `type`, `lost_reason`. `partner_id` can be null. `email` and `name` come from the lead when there is no partner. The list slug comes from the service; if the lead has no service, the hand-off fails and the CRM logs a `system` activity (the current rule for a missing slug). |
| Deal-won | No change. Only opportunities can be won. |
| Delegated task | `partner_id` can be null. |

### 8.4 Tool descriptions

Tool descriptions are part of the agent interface. These changes are
required:

- `ingest_leads`: say that it creates leads, not partners, and that
  `type = "opportunity"` skips qualification.
- `create_partner`: say "Do not use this for a research candidate. Use
  `create_lead`."
- `create_deal`: say "This creates an opportunity. For an unqualified
  candidate, use `create_lead`."
- `convert_lead`: say "Call `get_lead` first. Check
  `conversion_readiness` and `possible_duplicates`."

## 9. Implementation plan

Each phase is one pull request. Each phase must pass the full test suite.
A phase must not start before the previous phase is merged.

| Phase | Scope | Main files | Tests |
|---|---|---|---|
| P0 | This ADR. | `docs/adr/0001-lead-opportunity-model.md` | None |
| P1 | `migrate_008`, schema 8, `lost_reasons` seed, read paths return the new fields. Read paths accept a null partner and service, and use the new "open" test (5.8). `list_deals()` defaults to `type = 'opportunity'` and `include_lost = false` (admin board inherits). Stages write path refuses a new `is_lost = 1` (6.6). No other behaviour change. | `app/database.py`, `app/services/deals.py`, `app/services/call_queue.py`, `app/services/staleness.py`, `app/services/pipeline_stages.py`, `app/routers/api.py` (stages write) | 6.8, plus the existing suite |
| P2 | Lead service layer: create, update (fill-empty), get, list, duplicate detection (strong vs weak keys, 5.6), mark lost, restore, merge, convert. Partner match code moves to `partners.py`. Activities and tasks accept a null partner. | `app/services/leads.py` (split ingest into `lead_ingest.py` if it gets large), `partners.py`, `activities.py`, `delegated_tasks.py` | Unit tests for 5.4, 5.5, 5.6 and the invariants I1 to I5 |
| P3 | Ingest change (7.1), CLI flags. Client import stays opportunity by default; `--type lead` is opt-in. | `leads.py`, `routers/api.py`, `scripts/inject_leads.py`, `client_import.py` | Update `test_leads_*`, `test_inject_leads.py`, `test_client_import.py` |
| P4 | MCP tools (8.1, 8.2, 8.4). | `app/mcp/tools.py` | `test_mcp_tools.py`, a new end-to-end test of section 3 |
| P5 | Fit score input (5.7), call queue filter, follow-ups, nurture from lost reasons (8.3). | `icp.py`, `call_queue.py`, `scripts/staleness_gate.py`, `nurture.py` | `test_icp.py`, `test_call_queue.py`, `test_nurture_integration.py`, `test_staleness.py` |
| P6 | Admin UI: Leads list, lead form, convert dialog (with partner candidates), lost dialog, lost reasons page, type badge, lost filter. | `app/routers/admin.py`, `app/templates/admin/*` | `test_a11y.py`, `test_htmx.py`, new view tests |
| P7 | Documentation: `DATA_MODEL.md` (glossary, schema, ingest), `MCP.md`, `SCOPE.md`, `README.md` "Ingest leads", CHANGELOG. Optional script 6.5. | `docs/*`, `README.md`, `CHANGELOG.md`, `scripts/` | `npm run docs:test` |

Release: P1 to P7 ship in one minor release. The release notes must list
the breaking changes (6.4, 7.1, 8.2) and the rollback limit (6.7).

The end-to-end test in P4 must run the walkthrough in section 3 through
MCP `call_tool`. It must check, after each step, that `partners` has the
expected row count.

## 10. Open questions

| ID | Question | Recommended answer |
|---|---|---|
| Q1 | Must `POST /api/v1/leads` default to `type = lead` (breaking) or to `type = opportunity` (compatible)? | `lead`. The project is pre-1.0. The purpose of this change is to stop unverified data in `partners`. When `type` is omitted, set the deprecation header and `type_defaulted` (7.1). Client import does not inherit this default. |
| Q2 | Can the call queue include leads with a phone number (qualification calls)? | Not in this change. Add a later `include_leads` argument if an operator needs it. |
| Q3 | Must the operator be able to set a minimum fit score for conversion? | No. Keep it as agent judgement (D9). Reconsider after real use. |
| Q4 | Do leads need a status (New, Working) apart from `active` and `next_action_date`? | No. Use tags if an operator needs more states. |
| Q5 | When an opportunity is lost and later the same partner shows interest again, is that a new lead or a restore? | A new lead (or a new opportunity) linked to the partner. Restore is for mistakes. |
| Q6 | Do lost reasons need a REST API? | No. Admin UI and MCP are sufficient. |
| Q7 | Must the CRM keep a structured "qualification evidence" record (criteria and the activities that support each one)? | Not now. The `system` activity at conversion records the fit score and the matched criteria. Research notes keep the evidence. |
| Q8 | Must `create_lead` refuse to create when a strong duplicate (email or website) exists? | No. It returns `possible_duplicates`. The agent decides. Ingest auto-merges on strong keys only: email, or phone + name, or website + name (5.6, 7.1). |
| Q9 | Must ingest auto-merge on website domain or phone digits alone? | No. Those are weak keys for `find_duplicates` only. Shared hosts and shared reception numbers would merge unrelated leads. |
| Q10 | When an operator sets `is_lost = 1` on a stage that holds active deals, mark those deals lost, or refuse the write? | Refuse a change from 0 to 1 from schema 8 (6.6). The move shim still marks a deal lost when it enters an existing `is_lost` stage. |
| Q11 | Must the backfill set `closed_at` on migrated lost deals that have it null? | Yes. Use `updated_at` (6.3). |
| Q12 | For a lead that already has a `partner_id`, does the fit score mix lead and partner fields? | No. A lead scores from its own columns only (5.7). |
| Q13 | Does `get_lead` need a last-researched timestamp? | Yes. Return `last_researched_at` and `research_note_count`. `list_leads` accepts `sort = last_research` (8.1). |

## 11. Consequences

Good:

- `partners` contains confirmed parties only.
- Pipeline numbers count qualified opportunities only.
- The research history and the conversion decision are on one record.
- An agent can research over many sessions without a partner.
- The vocabulary matches a known CRM (Odoo). Operators and agents who
  know Odoo can use the model without new training.

Bad:

- One table rebuild migration. It is the first rebuild of a table that
  keeps live data. (`migrate_003` already dropped `rate_limit_hits`, but
  that table had no data that the app used.)
- Contact data is in two places (lead columns and partners). D10 limits
  the risk: after conversion, the partner is the source of truth.
- Breaking changes for ingest jobs and for `list_deals` callers that
  expect lost records.
- About seven pull requests of work, with changes to most service modules
  and to about one third of the test files.
