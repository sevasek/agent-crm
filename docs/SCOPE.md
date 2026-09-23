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

## In scope

- **Contacts**: one `partners` table for companies and people.
- **Interaction tracking**: an `activities` timeline per contact and deal.
- **Lead and pipeline management**: `deals` and the stage pipeline. This is the core.
- **Follow-ups**: next action and date on every deal, due and overdue flags, a
  daily check that logs each due follow-up.
- **Calling**: a ranked call queue (fixed formula) and a phone-first call view.
- **Agent interface**: `POST /mcp`, with a separate key per capability.
- **Optional outbound webhooks**: nurture enrollment on a stage change, and
  delegated-task notifications. Unset means no-op.

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
- **A general integration platform.** Two optional webhooks, nothing more.
