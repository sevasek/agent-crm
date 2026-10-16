# Privacy policy

This CRM stores personal information about leads, partners (contacts/companies), and the
people who operate the system.

## What we collect

- Contact details: name, email, phone number, company.
- Deal and lead information: service interest, pipeline stage, notes.
- Research notes: free-text notes an operator or AI agent attaches to a lead, which may
  include a source URL where the information was found.
- Call and activity history: a timestamped log of calls, notes, and status changes.
- Account information for people who log in to the CRM: email and a hashed password.

## Why we collect it

To run a sales/outreach pipeline: tracking who we've contacted, what they're interested in,
and what to do next.

## Who can see it

Only people with a login to this CRM instance, and any automation (bot/API key) explicitly
configured by the operator. See [SECURITY.md](../SECURITY.md) for how access is controlled.

## How long we keep it

Data is kept for as long as the CRM instance is in active use. See
[docs/OFFBOARDING.md](OFFBOARDING.md) for how an operator exports or deletes all data when
shutting an instance down.

## Access or correction

If you believe this CRM holds information about you and want to see it, correct it, or have
it deleted, contact the operator of this instance at `ops@example.com`. That is a
placeholder: each instance operator sets their own contact address here before publishing.

## Access and correction requests

Requests arrive at the operator contact address above. If someone asks what personal
information this CRM holds about them, or asks to correct or delete it: there is currently
no self-service UI for this. Handle it manually:

1. Search for the person in the admin UI (Partners, or Leads if lead-model search covers
   it).
2. For an export of their full record, see the data export process in
   [docs/OFFBOARDING.md](OFFBOARDING.md) (adapt the full-instance export to pull just the
   relevant rows if a single-person export isn't already supported).
3. For a correction, edit the record directly in the admin UI.
4. For deletion, see the deletion/retention guidance in
   [docs/OFFBOARDING.md](OFFBOARDING.md).

A self-service "download my data" / "request correction" flow is a future enhancement: not
yet decided or built, and deliberately deferred for now.

## Small business exemption status

The Australian Privacy Act 1988 exempts a "small business operator" (annual turnover under
$3,000,000 AUD) from most of the Act's obligations, unless a carve-out applies: trading in
personal information for a benefit, providing a health service, acting as a contracted
service provider under a Commonwealth contract, or being related to a business that together
crosses the turnover threshold.

**Current status:** `<TODO — confirm with the business owner: turnover vs $3M threshold,
and whether any carve-out above applies>`.

**Revisit this note when:** turnover approaches $3M AUD, the business starts selling or
trading personal information, takes on a Commonwealth contract, or starts handling health
information.

Even while exempt, the other items in this repo's compliance backlog (privacy policy, breach
response notes) are kept up to date as good practice, not because they're currently mandated.

## Questions

`ops@example.com` — the instance operator's contact address, configured per deployment.
