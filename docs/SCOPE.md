# Scope

A lean CRM for one operator whose AI agent does most of the clicking. This
file records what is in, what is out, and the one design rule that keeps it
generic.

## Configuration, not code

Anything that describes how a particular business operates is operator data,
not application code: the service catalogue, offers and pitches, pipeline
stages and their roles, fit rules, task owners. It lives behind a schema and a
CRUD interface (admin UI, and MCP tools where an agent should manage it) and
ships empty. A fresh clone starts with nothing beyond the starter pipeline, and
each deployer configures their own.

`pipeline_stages` is the reference implementation: the starter pipeline is
seeded once as editable data, not a hardcoded workflow. `offers` and
`icp_criteria` follow the same shape.

`scripts/seed_services.py` is an optional example-data script. Example content
must not live inside `app/` or run automatically at startup.

Stage automations follow the same rule. A deal entering a stage runs whatever
rows an operator saved (a delegated task, child deals, or both), scoped
optionally to one service and one offer. A fresh database has none, so a
stage change does nothing extra. `scripts/seed_sevasek_automations.py` is the
sevasek import for that table. It does not run at startup and it does not
insert services, stages, or prices.

## In scope

- **Contacts**: one `partners` table for companies and people.
- **Interaction tracking**: an `activities` timeline per partner and deal record, including research notes on a lead that has no partner yet.
- **Leads and opportunities**: one `deals` table with `type`. A lead is an unqualified signal and keeps its own contact data. An opportunity is the qualified pursuit of one service for one partner and is the only type in the pipeline. Conversion, lost reasons, and merge are in scope. Judgement (fit threshold, when to convert) stays in the agent.
- **Lead and pipeline management**: the opportunity stage pipeline. Tags on a deal record (a campaign slug or any other label) are part of that.
- **Follow-ups**: next action and date on a deal record, due and overdue flags, a
  daily check that logs each due follow-up on open opportunities. Lost records are skipped.
- **Post-sale**: a follow-on is another deal (`parent_deal_id`) plus delegated
  tasks. Not a second CRM, a fulfilment pipeline, or invoice / delivery status.
- **Calling**: a ranked call queue (fixed formula) and a phone-first call view.
- **Dark mode**: follows the browser `prefers-color-scheme`. Pico's tokens
  flip automatically; custom colors (`--bg-alt`, `.pill-*`, nav, cards) are
  paired the same way. There is no in-app theme toggle.
- **Agent interface**: `POST /mcp`, with a separate key per capability.
- **Optional outbound webhooks**: nurture enrollment on a stage change,
  delegated-task notifications, and deal-won invoice hand-off. Unset means no-op.

## Out of scope

- **Email inside the CRM.** Log interactions manually or via your agent.
- **Native mobile app.** The web app is responsive and the call view is built
  for a phone browser.
- **Roles and permissions.** Single operator; session auth and CSRF are the
  right amount for one user. Revisit if a second person needs a login.
- **Reports and analytics.** The board and the filterable deals list are enough
  at this scale.
- **Built-in AI.** No learned scoring or forecasting. Judgement belongs in the
  agent that talks to the MCP endpoint. The call ranking and fit scores are
  fixed formulas with visible weights.
- **A general integration platform.** Three optional webhooks (nurture,
  delegated-task, deal-won), nothing more. Invoice status is not written
  back into the CRM.
