"""Seed an example starting service catalog.

ingest_lead requires a resolving service_slug; a fresh production DB has none.
Admin UI can create them once a login exists (BOOTSTRAP_ADMIN_*), or MCP
`create_service` (idempotent on slug) if a bot is driving. This CLI unblocks
API inject without requiring an API key.

The three rows below are placeholder example data, not a canonical catalog —
edit this file (or just use Admin → Services / MCP `create_service`) to match
whatever this deployment actually sells. See "Configuration vs. code" in
docs/SCOPE.md.

Idempotent — re-running does not duplicate slugs (UNIQUE).

Local (compose exec is fine in dev):

    docker compose exec app python scripts/seed_services.py

In production, run a one-off container:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
        run --rm app python scripts/seed_services.py

Or create the row in Admin → Services / MCP create_service after bootstrap login.
"""
import sys

sys.path.insert(0, ".")

from app.database import db_timeout, init_db
from app.services.catalog import get_or_create_service
from app.services.offers import seed_starter_offers

# Example rows only — swap these for this deployment's actual catalog, or
# skip this script entirely and use Admin → Services / MCP create_service.
SEED_SERVICES = (
    {"name": "Consulting", "slug": "consulting"},
    {"name": "Website Rebuild", "slug": "website-rebuild"},
    {"name": "Monthly Retainer", "slug": "monthly-retainer"},
)


def seed_services():
    """Create the example starting catalog. Returns list of (service, created) tuples."""
    results = []
    for spec in SEED_SERVICES:
        service, created = get_or_create_service(spec["name"], spec["slug"])
        results.append((service, created))
    seed_starter_offers()
    return results


def main():
    try:
        with db_timeout(30):
            init_db()
            results = seed_services()
        created = [s for s, was_created in results if was_created]
        existing = [s for s, was_created in results if not was_created]
        for service in created:
            print(f"created: {service['slug']} ({service['name']})")
        for service in existing:
            print(f"existing: {service['slug']} ({service['name']})")
        print(f"{len(created)} created, {len(existing)} already existed.")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
