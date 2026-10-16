# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue:
https://github.com/sevasek/agent-crm/security/advisories/new

Include what you found, how to reproduce it, and which version or commit you
tested. You'll get a reply as soon as I can manage; this is a one-person project.

## Supported versions

The project is pre-1.0. Fixes go to `main`.

## Scope

The CRM is designed for a single operator behind a TLS-terminating reverse
proxy. Reports about the intended-absent features (roles and permissions, email)
are not vulnerabilities. Reports about authentication, session handling,
API-key checks, the MCP endpoint and its OAuth flow, rate limiting, and data
exposure are very welcome.

## Privacy

What the CRM collects, why it is collected, and how to request access or
deletion: [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Data breach response

If personal information held in this CRM is exposed (e.g. unauthorised access
to the database, a leaked API key, a compromised account):

1. **Contain it first.** Rotate the affected secret (`SECRET_KEY`,
   `CRM_API_KEY`, `CRM_STAGES_API_KEY`, or `CRM_MCP_API_KEY` — see
   [`docs/DEPLOY.md`](docs/DEPLOY.md) for how to rotate and redeploy) and
   change any affected account passwords.
2. **Work out what was exposed.** Which tables/records, how many people, what
   kind of data (names/emails/phone numbers are lower-severity than anything
   more sensitive).
3. **Decide if it's an "eligible data breach"** under the Privacy Act's
   Notifiable Data Breaches (NDB) scheme: unauthorised access or disclosure of
   personal information that a reasonable person would conclude is likely to
   result in serious harm to someone, and the access/disclosure could not be
   prevented by remedial action.
4. **If yes:** notify the OAIC (https://www.oaic.gov.au) and the affected
   individuals as soon as practicable, describing what happened, what
   information was involved, and what you're doing about it. There is no fixed
   statutory deadline, but the OAIC expects notification "as soon as
   practicable" after you become aware.
5. **If in doubt, treat it as notifiable** rather than guessing it isn't.

This is a process note, not legal advice — if a real breach happens, get
advice specific to the situation.
