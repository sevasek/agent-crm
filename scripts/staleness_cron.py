"""Production loop for the follow-up gate — no host crontab needed.

This process runs in the `staleness-cron` sidecar, shares ./data with `app`,
and uses the same uid (APP_UID) so sqlite stays writable.

Runs once at start (idempotent, so a midday deploy still covers today) then
sleeps until 08:00 in TZ (UTC by default) each day. A due/overdue
result is expected and does not kill the loop.

A crash (locked sqlite, unexpected error) retries a few times with a short
pause before giving up until the next 08:00 — the process stays Up either
way. Writes use a 30s busy timeout so they are not racing uvicorn's 5s
request-handler timeout.
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, ".")

from app.database import db_timeout
from scripts.staleness_gate import run_and_report

logger = logging.getLogger(__name__)

GATE_HOUR = 8
GATE_MINUTE = 0
GATE_BUSY_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30


def _tz():
    name = os.getenv("TZ", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Invalid TZ %r; using UTC for the staleness cron", name)
        return ZoneInfo("UTC")


def next_run_at(now: datetime, hour: int = GATE_HOUR, minute: int = GATE_MINUTE) -> datetime:
    """First `hour:minute` strictly after `now` in `now`'s timezone."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def sleep_until_next_run(hour: int = GATE_HOUR, minute: int = GATE_MINUTE) -> None:
    now = datetime.now(_tz())
    target = next_run_at(now, hour=hour, minute=minute)
    seconds = max(0.0, (target - now).total_seconds())
    logger.info("Next follow-up gate at %s (%ss)", target.isoformat(), int(seconds))
    time.sleep(seconds)


def retry_pause(seconds: float) -> None:
    time.sleep(seconds)


def run_once() -> int:
    with db_timeout(GATE_BUSY_TIMEOUT):
        return run_and_report()


def run_with_retries(
    attempts: int = RETRY_ATTEMPTS,
    delay: float = RETRY_DELAY_SECONDS,
):
    """Run the gate; retry transient failures, then give up until next 08:00."""
    for attempt in range(1, attempts + 1):
        try:
            return run_once()
        except Exception:
            logger.exception(
                "Follow-up gate crashed (attempt %s/%s)", attempt, attempts
            )
            if attempt < attempts:
                logger.info("Retrying follow-up gate in %ss", delay)
                retry_pause(delay)
    logger.error(
        "Follow-up gate failed after %s attempts; sleeping until next scheduled run",
        attempts,
    )
    return None


def loop() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    while True:
        run_with_retries()
        sleep_until_next_run()


if __name__ == "__main__":
    loop()
