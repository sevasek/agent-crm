"""Daily follow-up gate on `deals.next_action_date`.

Same job as the engagement-gate used elsewhere in our stack: compute staleness
against an explicit clock and fail while anything is outstanding, so a cron MAILTO / monitoring
check keeps nagging until the date is moved or the deal is closed. Also logs a
one-shot system activity per due-date (idempotent across re-runs).

"Today" is `TZ` (UTC by default), not UTC.

Run inside the container:

    docker compose exec app python scripts/staleness_gate.py

Production does not use host crontab or a sudoers `compose exec` line. The
`staleness-cron` sidecar in docker-compose.prod.yml runs this same gate (via
`scripts/staleness_cron.py`) at 08:00 operator-local and once on container
start. Re-runs are idempotent.
"""
import sys

sys.path.insert(0, ".")

from app.database import init_db
from app.services.staleness import parse_action_date, run_staleness_gate


def run_and_report() -> int:
    """Print the gate result. 0 = all clear, 1 = due/overdue deals exist."""
    init_db()
    result = run_staleness_gate()
    due = result["due"]
    logged = result["logged_ids"]

    if not due:
        print("Follow-up gate: all clear (no open deals due or overdue).")
        return 0

    print(f"Follow-up gate: {len(due)} open deal(s) due or overdue")
    for deal in due:
        action_date = parse_action_date(deal.get("next_action_date"))
        days = deal.get("days_overdue")
        age = "due today" if days == 0 else f"overdue {days}d"
        action = deal.get("next_action") or ""
        print(
            f"  #{deal['id']} {deal.get('partner_name')} / {deal.get('service_name')}"
            f"  {action_date.isoformat() if action_date else '?'}  {age}"
            f"{'  ' + action if action else ''}"
        )
    if logged:
        print(f"Logged {len(logged)} new timeline activit{'y' if len(logged) == 1 else 'ies'}.")
    else:
        print("No new activities (already logged for the current due date).")
    return 1


def main() -> int:
    return run_and_report()


if __name__ == "__main__":
    sys.exit(main())
