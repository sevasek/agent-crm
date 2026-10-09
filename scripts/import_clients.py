"""One-off import of a directory of markdown client files into partners.

The markdown files are not in this repo. Point the script at the directory
(or at individual files). Re-runs are safe: companies match by name, people
by email or name+company, and an already-open deal for the same service is
skipped.

Compose only bind-mounts `./app` and `./data`, so a host path is invisible
inside `docker compose exec`. Use a one-off bind mount, or run on the host
with the same venv the tests use:

    python scripts/import_clients.py --dry-run tests/fixtures/clients
    python scripts/import_clients.py --dry-run /path/to/clients
    python scripts/import_clients.py /path/to/clients

    docker compose run --rm -v /path/to/clients:/clients:ro \\
        app python scripts/import_clients.py /clients
"""
import sys

sys.path.insert(0, ".")

from app.database import init_db
from app.services.client_import import import_paths


def main() -> int:
    raw = sys.argv[1:]
    dry_run = "--dry-run" in raw
    record_type = "opportunity"
    args = []
    index = 0
    while index < len(raw):
        item = raw[index]
        if item == "--dry-run":
            index += 1
            continue
        if item == "--type" and index + 1 < len(raw):
            record_type = raw[index + 1]
            index += 2
            continue
        args.append(item)
        index += 1
    if record_type not in ("lead", "opportunity"):
        print("type must be lead or opportunity", file=sys.stderr)
        return 1
    if not args:
        print("Usage: python scripts/import_clients.py [--dry-run] [--type lead|opportunity] <dir-or-file.md> [...]")
        print("Docker: bind-mount the clients dir; compose exec cannot see a host path.")
        return 1

    init_db()
    results = import_paths(args, dry_run=dry_run, record_type=record_type)
    if not results:
        print("No markdown files found.")
        return 1

    for item in results:
        name = (item.get("record") or {}).get("name") or (item.get("record") or {}).get("company") or "?"
        bits = [item.get("status") or item.get("person") or "ok"]
        if item.get("error"):
            bits.append(item["error"])
        if item.get("company"):
            bits.append(f"company={item['company']}")
        if item.get("person"):
            bits.append(f"person={item['person']}")
        if item.get("deal"):
            bits.append(f"deal={item['deal']}")
        print(f"{item['file']}: {name}  ({', '.join(bits)})")

    invalid = sum(1 for r in results if r.get("status") == "invalid")
    errors = sum(1 for r in results if r.get("status") == "error")
    if errors:
        return 1
    return 1 if invalid == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
