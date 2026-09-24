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
