# Using the CRM

This is the day-to-day guide: the words on the screen, and what you do with them.
Installing the app, connecting an agent, and the database fields are separate pages.

## The words

A **partner** is a person or a company you have confirmed is real. A person can sit under their company.

A **lead** is someone you might contact who is not confirmed yet. A name and a phone number are enough. A lead is not on the pipeline board, and it does not have to be a partner yet.

A **deal** is a lead you have decided to pursue. It belongs to one partner and one service, and it moves through the pipeline. In the database this is called an opportunity. On screen it is a deal.

**Lost** means you stopped. You pick a reason from Lost reasons. The deal leaves the open board. You can bring it back if that was a mistake. A reason can be a disqualification (the wrong kind of business, too small, out of area). That deal shows as disqualified instead of lost. You can change the reason later; the date it closed stays.

## The screens

- **Partners** — companies and people.
- **Calls** — who to phone, with the offer to pitch and buttons for the outcome. It is laid out for a phone browser.
- **Pipeline** — the board. Drag a deal from one column to another.
- **Deals** — the same deals as a list. Due and overdue follow-ups show here.
- **Leads** — people you are still researching. Open a lead to add notes, or turn it into a deal when you want to pursue it.
- **Services** — what you sell.
- **Stages** — the columns on the board, and what each one does.
- **Offers** — the price and the short pitch you use on a call.
- **Automations** — optional. When a deal enters a stage, the CRM can create a task or a follow-on deal. A new install has none of these.
- **Lost reasons** — the list you pick from when a deal is lost. "Not now" can also notify your nurture tool, if you have set one up. Tick Disqualification when that reason should show as disqualified rather than lost. A new install includes a Disqualified reason with that tick on.
- **ICP** — fit rules. Each matching rule adds the points you gave it. There is no pass mark. You, or your agent, decide.
- **Settings** — your password and the keys an agent or an import uses.

A new install starts with seven stages: New, Contacted, Qualified, Nurture, Proposal, Won, and Lost. You can rename them. It does not start with your services, offers, or fit rules. You add those.

## Follow-ups

A deal can have a next step and a date. When that date is today or earlier, the deal shows as due. Once a day the CRM writes a note on the timeline for each one that is due. Lost deals are skipped. "Today" uses the timezone chosen when the CRM was installed.

## The call list

Calls are ordered by a score out of 100. The points come from the deal value, where the contact came from, how complete the contact details are, and how recently the stage changed. The formula does not change on its own, and it does not decide who you should pursue.

Each call has a one-tap outcome button:

- **No answer** — leaves the deal where it is, so it comes back up in today's call list for a same-day retry.
- **Warm** — the contact is interested but not ready yet. Moves the deal into nurture.
- **Meeting Scheduled** — you have a meeting booked. Moves the deal to the Proposal stage.
- **Won** — the contact became a customer. Moves the deal to the Won stage.
- **Lost** — the contact is not interested. Moves the deal to the Lost stage.

## What an agent can do

If you connect an AI agent, it can search contacts, work the call list, log calls, update leads, and move deals. It cannot delete records, send email, or decide who is allowed to log in.

Turning a lead into a deal is still a decision. The CRM can show the fit score and the call score. It does not make the call.

## What this CRM does not do

It does not send or receive email. Log the conversation yourself, or let the agent log it.

It does not have its own phone app. Open the site in the phone's browser.

It is built for one person logged in, not a team with different permissions.

It does not draw charts. The board and the deals list are the view.

It does not connect to the rest of your software in general. You can optionally notify another tool in three cases: a deal enters nurture, a task is handed to someone, or a deal is won. Leave the address blank and nothing is sent. Invoice status does not come back into the CRM.

## If someone asks about their information

There is no button for a contact to download their own record. Search Partners and Leads, edit the page if something is wrong, and use the privacy page as the text you send them. Put your own email address on that page first.

## The other pages

- [Privacy policy](PRIVACY.md) — what is stored, and how someone asks about their record.
- [Scope](SCOPE.md) — the same boundaries, in a shorter list.
- [Data model](DATA_MODEL.md) — every database field.
- [MCP: the agent interface](MCP.md) — connecting an agent, and the full tool list.
- [Deploy](DEPLOY.md) and [New client instance](CLIENT_DEPLOY.md) — putting it on a server.
- [Offboarding](OFFBOARDING.md) — shutting an instance down.
