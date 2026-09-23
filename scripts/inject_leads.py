"""Operator CLI to inject leads — same privilege class as scripts/create_admin.py.

Default (in-process) calls ingest_lead() after init_db(), bypassing the reverse proxy and
HTTP. Requires the app container / venv with DB access. --http POSTs the same
JSON to {URL}/api/v1/leads. --dry-run parses and prints, and writes nothing.

    python scripts/inject_leads.py tests/fixtures/leads/example.json
    python scripts/inject_leads.py --dry-run path/to/leads.json
    python scripts/inject_leads.py --http http://127.0.0.1:8000 path/to/leads.json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from app.database import init_db
from app.services.leads import ingest_lead

SUCCESS_STATUSES = frozenset({
    "created",
    "duplicate_open_deal",
    "existing_partner_new_deal",
})

USAGE = (
    "Usage: python scripts/inject_leads.py [--dry-run] [--http URL] FILE [FILE ...]\n"
    "Load a leads JSON file (see docs/DATA_MODEL.md, Lead ingest) and ingest it."
)


def load_leads(paths):
    """Load and concatenate `leads` arrays from JSON files.

    Returns (leads, error_message). error_message is set on failure.
    """
    if not paths:
        return None, "no files given"

    all_leads = []
    for path in paths:
        if not os.path.isfile(path):
            return None, f"file not found: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON in {path}: {exc}"
        except OSError as exc:
            return None, f"could not read {path}: {exc}"

        if not isinstance(payload, dict):
            return None, f"{path}: expected a JSON object with a 'leads' array"
        leads = payload.get("leads")
        if not isinstance(leads, list):
            return None, f"{path}: expected a 'leads' array (docs/DATA_MODEL.md, Lead ingest)"
        all_leads.extend(leads)

    if not all_leads:
        return None, "empty leads list"

    return all_leads, None


def payload_from_leads(leads):
    return {"leads": leads}


def print_dry_run(leads):
    print(f"dry-run: {len(leads)} lead(s), no writes")
    for lead in leads:
        if isinstance(lead, dict):
            name = lead.get("name") or ""
            email = lead.get("email") or ""
            slug = lead.get("service_slug") or ""
        else:
            name = email = slug = ""
        print(f"  name={name}  email={email}  service_slug={slug}")


def print_results(results):
    for item in results:
        email = item.get("email") or ""
        status = item.get("status") or ""
        print(f"{email}\t{status}")


def exit_code_for_results(results):
    """0 only if every lead succeeded; 1 if empty or any item failed."""
    if not results:
        return 1
    if any(item.get("status") not in SUCCESS_STATUSES for item in results):
        return 1
    return 0


def inject_in_process(leads):
    init_db()
    results = []
    for lead in leads:
        if not isinstance(lead, dict):
            results.append({"email": "", "status": "invalid"})
            continue
        try:
            results.append(ingest_lead(lead))
        except Exception:
            raw_email = lead.get("email")
            email = raw_email if isinstance(raw_email, str) else ""
            results.append({"email": email, "status": "invalid"})
    return results


def _leads_url(base_url):
    return base_url.rstrip("/") + "/api/v1/leads"


def inject_http(base_url, leads, api_key):
    """POST the payload. Returns (http_status, results_or_none, error_message)."""
    if not api_key:
        return 401, None, "CRM_API_KEY is unset; HTTP inject refused (fails closed, same as 401)"

    url = _leads_url(base_url)
    body = json.dumps(payload_from_leads(leads)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, None, raw or f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"HTTP request failed: {exc.reason}"

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, None, raw or f"HTTP {status}"

    if status != 200:
        return status, None, raw

    results = parsed.get("results")
    if not isinstance(results, list):
        return status, None, raw
    return status, results, None


def run(argv=None):
    parser = argparse.ArgumentParser(
        prog="inject_leads.py",
        description=(
            "Inject leads from JSON. Default is in-process (calls ingest_lead after "
            "init_db). --http POSTs to {URL}/api/v1/leads with X-API-Key from CRM_API_KEY."
        ),
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and print name/email/service_slug; no DB writes and no HTTP",
    )
    parser.add_argument(
        "--http",
        metavar="URL",
        help="POST to URL/api/v1/leads (e.g. http://127.0.0.1:8000) instead of in-process",
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="JSON file(s) with a top-level 'leads' array",
    )

    args = parser.parse_args(argv)
    leads, error = load_leads(args.files)
    if error:
        print(error, file=sys.stderr)
        if error == "no files given":
            print(USAGE, file=sys.stderr)
        return 1

    if args.dry_run:
        print_dry_run(leads)
        return 0

    if args.http:
        status, results, http_error = inject_http(
            args.http, leads, os.getenv("CRM_API_KEY", ""),
        )
        if results is None:
            print(http_error or f"HTTP {status}", file=sys.stderr)
            return 1
        print_results(results)
        return exit_code_for_results(results)

    results = inject_in_process(leads)
    print_results(results)
    return exit_code_for_results(results)


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
