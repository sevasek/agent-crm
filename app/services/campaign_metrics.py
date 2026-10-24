"""Straight-line campaign results, win/loss, pacing, and period breakdown.

Pure function: no database, no clock, and no web framework. Pass plain
dicts in and read plain dicts out. Campaign pages and MCP tools call this.
Deal outcome (``open`` / ``won`` / ``lost`` / ``disqualified``) is decided
by the caller; this module does not read lost-reason flags.

Formulas: docs/DATA_MODEL.md → Campaign metrics.

Signature
---------
``compute_campaign_metrics(campaign, deals, events, today, tz, period=None) -> dict``

``campaign``
    ``start_date`` and ``deadline`` are calendar dates (``datetime.date``,
    ``YYYY-MM-DD``, or a datetime — see Dates). Both optional.
    ``targets`` is a dict. Each key is optional: ``leads``, ``contacts``,
    ``conversations``, ``advanced``, ``won``, ``won_value``. A missing key,
    ``None``, or ``""`` means "not set". ``0`` is a real target. Numbers
    may be numeric strings. Negative or non-numeric targets raise
    ``ValueError``.

``deals``
    Each record: ``id``, ``outcome`` (``open`` | ``won`` | ``lost`` |
    ``disqualified``; anything else counts as open), ``stage``, ``value``,
    ``created_at``, ``closed_at``, ``lost_reason``. Each record is counted
    once; the caller dedupes.

``events``
    Dated countable events. ``type`` maps as follows (case-insensitive;
    spaces and hyphens become underscores):

    - ``contact``, ``contacts``, ``contact_made`` → contacts
    - ``conversation``, ``conversations`` → conversations
    - ``advanced``, ``advance`` → advanced

    Any other type is ignored. ``timestamp`` is the instant; ``occurred_at``
    is used when ``timestamp`` is missing. ``deal_id`` links a deal to
    "contacted" (string and int ids match).

``today``
    The operator's calendar date. Required. This function does not read
    the clock. Records dated after ``today`` still count: ``today`` is the
    pacing clock, not a filter. The caller chooses which rows to pass.

``tz``
    ``zoneinfo.ZoneInfo``, another ``datetime.tzinfo``, or an IANA name
    such as ``"Australia/Sydney"``. Used to bucket events and deal
    timestamps. Unknown names raise ``ValueError``.

``period``
    ``None`` or ``""`` omits the breakdown (``periods`` is ``None``).
    ``"week"``, ``"month"``, or ``"year"`` (any case) fills it. Anything
    else raises ``ValueError``. Weeks start Monday.

Dates
-----
Campaign ``start_date``, ``deadline``, and ``today`` are calendar dates in
the operator timezone. A date-only value uses that date. A naive datetime
uses its date part and is not shifted. An aware datetime is converted to
``tz`` first.

Deal and event timestamps are instants. A naive value is UTC (the way this
CRM stores timestamps), then converted to ``tz``. A date-only value is that
calendar date and is not shifted. An unparseable timestamp is undated: it
still counts in the overall actuals and is listed under ``periods.undated``.

Window
------
The window is inclusive of both dates.
``days_total = (deadline - start_date).days + 1``.
A start of 1 September and a deadline of 30 September is 30 days. The
deadline date is the last day inside the window. The day after the
deadline, the campaign has ended: final percents, no required rate, no
projection.

On a day inside the window, days elapsed are the calendar days before
today (the start date itself has elapsed 0) and days remaining include
today. Elapsed + remaining = total.

A deadline before the start is a zero-length window: actuals are reported,
and every rate, time percent, and projection is ``None`` (never a
divide-by-zero). The same calendar day for start and deadline is a one-day
campaign, not a zero-length window.

Rates
-----
A "week" in a rate is 7 days, a "month" is 30 days, and a "year" is 365
days. Those are not calendar months. The period breakdown uses real
calendar weeks, months, and years, with the target spread by day
(straight line, no weighting).

Counts
------
- Lead actuals are deals whose outcome is not ``disqualified`` (open, won,
  and lost all count).
- Contact, conversation, and advanced actuals are event counts, not unique
  deals.
- A deal is contacted when it has a contact, conversation, or advanced
  event. ``not_touched`` is the rest. Together they equal the deal count.
- Win rate is ``won / (won + lost)``. Disqualified and open deals are
  excluded. When ``won + lost`` is 0 the value is ``None`` and the note is
  ``"no closed deals"`` — never 0%. A genuine losing streak (lost deals,
  no wins) is 0%.
- Disqualification rate is ``disqualified / deals`` over every deal passed
  in. When there are no deals the value is ``None`` and the note is
  ``"no leads"``.
- Won and lost value sum ``value`` for that outcome. Missing or
  non-numeric values count as 0. Disqualified value is separate and is
  not part of lost value.

Every calculated number is a metric dict:

``{"value": <json-friendly or None>, "formula": "<text>", "inputs": {...}, "note": <str or None>}``

``value`` is ``None`` when the figure does not exist. ``note`` says why.
``inputs`` are the numbers and dates that went into the formula, for a
hover explanation. Counts are ints. Rates and percents are floats on a
0–100 scale for percents (``25.0`` means 25%). The ratio's value is a
string ``"3 : 9"`` (not reduced) or ``None``.

Return shape
------------
``results``
    ``deals``, ``leads``, ``by_stage`` (value is ``{stage: count}``),
    ``contacted``, ``not_touched``, ``open``, ``won``, ``lost``,
    ``disqualified``, ``contacts``, ``conversations``, ``advanced``,
    ``open_value``, ``won_value``, ``lost_value``, ``disqualified_value``.

``win_loss``
    ``won``, ``lost``, ``open``, ``disqualified``, ``ratio``, ``win_rate``,
    ``disqualification_rate``, ``lost_by_reason`` (value is a list of
    ``{"reason", "count"}``, lost deals only, blank reason ``""``, most
    common first), ``won_value``, ``lost_value``.

``window``
    ``start_date``, ``deadline``, ``today``, ``timezone``, ``state``,
    ``deadline_passed``, ``not_started``, ``days_total``, ``days_elapsed``,
    ``days_remaining``, ``percent_elapsed``.

    ``state`` is one of ``in_progress``, ``not_started``, ``ended``,
    ``no_deadline``, ``no_start``, ``zero_length``. ``not_started`` is also
    a boolean, so a missing deadline and a future start can both be seen.
    ``deadline_passed`` is true only for a positive window on a day after
    the deadline.

    ``targets``
    One entry per target that is set, in the order leads, contacts,
    conversations, advanced, won, won_value. Each has ``name``, ``target``,
    ``actual``, ``remaining`` (target minus actual, negative when over),
    ``percent_complete`` (``None`` when the target is 0), ``days_total``,
    ``days_elapsed``, ``days_remaining``, ``percent_elapsed`` (the same
    metric objects as ``window``), ``required_rate`` and ``actual_rate``
    (each ``week`` / ``month`` / ``year``), ``status`` (``ahead`` /
    ``behind`` / ``on_track`` / ``None``), ``status_inputs``
    (``percent_complete`` and ``percent_elapsed``), and ``projection``.

    Required rate is remaining work over days still left, including today.
    Actual rate is actual over days elapsed. Projection is
    ``actual / days_elapsed * days_total``, only while the window is in
    progress and at least one day has elapsed. No deadline, no start, a
    zero-length window, or a passed deadline leaves the required rate and
    the projection ``None``.

``pacing_status``
    ``behind`` if any target is behind, ``ahead`` if every comparable
    target is ahead, ``on_track`` otherwise when at least one target has a
    status, ``None`` when none do. Per-target ``status`` is the detail.

``periods``
    ``None`` when ``period`` is omitted. Otherwise ``grouping``,
    ``window_days``, ``undated`` (plain counts, not metrics), and ``rows``.
    Each row: ``key``, ``start``, ``end`` (inclusive), ``overlap_days``,
    ``actuals``, ``required``, ``cumulative_actual``, ``cumulative_target``
    for each set target, plus ``won``, ``lost``, ``disqualified``, and
    ``win_rate`` for deals closed in that period (``closed_at``).

    Week keys are ISO ``YYYY-Www`` of the Monday. Month keys are
    ``YYYY-MM``. Year keys are ``YYYY``. Rows run from the earliest of the
    start date and any activity through the deadline (and later, if
    something is dated after it). With no deadline, the span runs through
    today once the start date has arrived.

    ``required`` for a period is ``target * overlap_days / window_days``.
    ``cumulative_target`` is that straight line at the period's end.
    Periods outside the window have required 0. ``cumulative_actual`` sums
    period actuals only; add ``undated`` to reconcile with the overall
    actual. Lead period actuals use ``created_at`` and skip disqualified
    deals. Won value uses ``closed_at``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from math import isclose, isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "AHEAD",
    "BEHIND",
    "DAYS_PER_MONTH",
    "DAYS_PER_WEEK",
    "DAYS_PER_YEAR",
    "DEADLINE_PASSED",
    "NO_CLOSED_DEALS",
    "NO_DEADLINE",
    "NO_ELAPSED_DAYS",
    "NO_LEADS",
    "NO_START_DATE",
    "ON_TRACK",
    "OUTCOMES",
    "START_IN_FUTURE",
    "TARGET_IS_ZERO",
    "TARGET_KEYS",
    "ZERO_LENGTH_WINDOW",
    "compute_campaign_metrics",
]

TARGET_KEYS = ("leads", "contacts", "conversations", "advanced", "won", "won_value")
OUTCOMES = ("open", "won", "lost", "disqualified")
PERIODS = ("week", "month", "year")

DAYS_PER_WEEK = 7
DAYS_PER_MONTH = 30
DAYS_PER_YEAR = 365
_PERIOD_LENGTH = {
    "week": DAYS_PER_WEEK,
    "month": DAYS_PER_MONTH,
    "year": DAYS_PER_YEAR,
}

# Event type (normalised) → target key.
_EVENT_TARGETS = {
    "contact": "contacts",
    "contacts": "contacts",
    "contact_made": "contacts",
    "conversation": "conversations",
    "conversations": "conversations",
    "advanced": "advanced",
    "advance": "advanced",
}
_TOUCH_TARGETS = frozenset({"contacts", "conversations", "advanced"})

NO_CLOSED_DEALS = "no closed deals"
NO_LEADS = "no leads"
NO_DEADLINE = "no deadline"
DEADLINE_PASSED = "deadline passed"
ZERO_LENGTH_WINDOW = "zero-length window"
NO_START_DATE = "no start date"
NO_ELAPSED_DAYS = "no elapsed days"
START_IN_FUTURE = "start date in the future"
TARGET_IS_ZERO = "target is zero"

ON_TRACK = "on_track"
BEHIND = "behind"
AHEAD = "ahead"

STATE_IN_PROGRESS = "in_progress"
STATE_NOT_STARTED = "not_started"
STATE_ENDED = "ended"
STATE_NO_DEADLINE = "no_deadline"
STATE_NO_START = "no_start"
STATE_ZERO_LENGTH = "zero_length"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_PERIODS = 6000


def compute_campaign_metrics(campaign, deals, events, today, tz, period=None) -> dict:
    """Results, win/loss, pacing, and an optional period breakdown.

    See the module docstring for the contract and the return shape.
    """
    zone = _resolve_tz(tz)
    today_date = _require_calendar_date(today, zone, "today")
    campaign = _as_campaign(campaign)
    deals = _as_records(deals, "deals")
    events = _as_records(events, "events")
    grouping = _normalize_period(period)

    start = _require_calendar_date(campaign.get("start_date"), zone, "start_date")
    deadline = _require_calendar_date(campaign.get("deadline"), zone, "deadline")
    targets = _parse_targets(campaign.get("targets"))
    window = _build_window(start, deadline, today_date, _tz_label(zone))

    counted = _count(deals, events, zone)
    results = _results(counted)
    win_loss = _win_loss(counted, results)
    window_payload = _window_payload(window)
    target_rows, pacing_status = _pacing(targets, counted, window, window_payload)
    periods = None if grouping is None else _periods(grouping, window, targets, counted)

    return {
        "results": results,
        "win_loss": win_loss,
        "window": window_payload,
        "targets": target_rows,
        "pacing_status": pacing_status,
        "periods": periods,
    }


# --- metric wrapper -------------------------------------------------------

def _metric(value, formula, inputs, note=None):
    return {
        "value": value,
        "formula": formula,
        "inputs": dict(inputs),
        "note": note,
    }


# --- parsing --------------------------------------------------------------

def _resolve_tz(tz):
    if isinstance(tz, ZoneInfo):
        return tz
    if isinstance(tz, tzinfo):
        return tz
    if isinstance(tz, str):
        name = tz.strip()
        if not name:
            raise ValueError("tz is required")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {tz!r}") from exc
    raise TypeError("tz must be a timezone or an IANA name")


def _tz_label(tz) -> str:
    key = getattr(tz, "key", None)
    if key:
        return key
    return str(tz)


def _as_campaign(campaign):
    if campaign is None:
        return {}
    if not isinstance(campaign, Mapping):
        raise TypeError("campaign must be a dict")
    return campaign


def _as_records(records, label):
    if records is None:
        return []
    if isinstance(records, Mapping):
        raise TypeError(f"{label} must be a list of records")
    try:
        rows = list(records)
    except TypeError as exc:
        raise TypeError(f"{label} must be a list of records") from exc
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"each {label[:-1]} must be a dict")
    return rows


def _normalize_period(period):
    if period is None:
        return None
    if isinstance(period, str):
        text = period.strip().lower()
        if not text:
            return None
        if text in PERIODS:
            return text
    raise ValueError("period must be week, month, year, or omitted")


def _parse_targets(raw):
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError("campaign targets must be a dict")
    parsed = {}
    for key in TARGET_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        number = _num(value)
        if number is None or number < 0:
            raise ValueError(f"invalid target {key}: {value!r}")
        parsed[key] = number
    return parsed


def _num(value):
    """Finite number, or None. Booleans are rejected (they are ints)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not isfinite(value):
            return None
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not isfinite(number):
            return None
        if number.is_integer():
            return int(number)
        return number
    return None


def _require_calendar_date(value, tz, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        if field == "today":
            raise ValueError("today is required")
        return None
    parsed = _calendar_date(value, tz)
    if parsed is None:
        raise ValueError(f"invalid {field}: {value!r}")
    return parsed


def _calendar_date(value, tz):
    """Operator calendar date. Naive datetimes keep their date part."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(tz).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _DATE_ONLY.match(text):
            return _parse_ymd(text)
        normalised = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.date()
        return parsed.astimezone(tz).date()
    return None


def _occurred_on(value, tz):
    """Local calendar date of an instant. Naive datetimes are UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        return _parse_ymd(text)
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date()


def _parse_ymd(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _iso(value):
    if value is None:
        return None
    return value.isoformat()


def _id_key(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _outcome(deal):
    raw = str(deal.get("outcome") or "").strip().lower()
    if raw in OUTCOMES:
        return raw
    return "open"


def _stage(deal):
    raw = deal.get("stage")
    if raw is None:
        return ""
    return str(raw)


def _reason(deal):
    raw = deal.get("lost_reason")
    if raw is None:
        return ""
    return str(raw).strip()


def _event_target(event):
    raw = event.get("type")
    if raw is None:
        return None
    key = str(raw).strip().lower().replace("-", " ").replace("_", " ")
    key = "_".join(key.split())
    return _EVENT_TARGETS.get(key)


def _event_instant(event):
    if "timestamp" in event and event.get("timestamp") not in (None, ""):
        return event.get("timestamp")
    return event.get("occurred_at")


# --- counting -------------------------------------------------------------

@dataclass
class _Counts:
    deals: int = 0
    leads: int = 0
    open: int = 0
    won: int = 0
    lost: int = 0
    disqualified: int = 0
    open_value: float | int = 0
    won_value: float | int = 0
    lost_value: float | int = 0
    disqualified_value: float | int = 0
    won_missing_value: int = 0
    lost_missing_value: int = 0
    open_missing_value: int = 0
    disqualified_missing_value: int = 0
    by_stage: dict | None = None
    lost_by_reason: dict | None = None
    contacted: int = 0
    contacts: int = 0
    conversations: int = 0
    advanced: int = 0
    contact_events: int = 0
    # Period placement. Lists of local dates, plus undated tallies.
    lead_dates: list | None = None
    contact_dates: list | None = None
    conversation_dates: list | None = None
    advanced_dates: list | None = None
    won_dates: list | None = None
    won_value_points: list | None = None
    lost_dates: list | None = None
    disqualified_dates: list | None = None
    undated_leads: int = 0
    undated_contacts: int = 0
    undated_conversations: int = 0
    undated_advanced: int = 0
    undated_won: int = 0
    undated_won_value: float | int = 0
    undated_lost: int = 0
    undated_disqualified: int = 0
    activity_dates: list | None = None


def _count(deals, events, tz) -> _Counts:
    counts = _Counts(
        by_stage=defaultdict(int),
        lost_by_reason=defaultdict(int),
        lead_dates=[],
        contact_dates=[],
        conversation_dates=[],
        advanced_dates=[],
        won_dates=[],
        won_value_points=[],
        lost_dates=[],
        disqualified_dates=[],
        activity_dates=[],
    )
    touched = set()
    for event in events:
        target = _event_target(event)
        if target is None:
            continue
        when = _occurred_on(_event_instant(event), tz)
        if target == "contacts":
            counts.contacts += 1
            counts.contact_events += 1
            _place(counts.contact_dates, counts, "undated_contacts", when)
        elif target == "conversations":
            counts.conversations += 1
            _place(counts.conversation_dates, counts, "undated_conversations", when)
        else:
            counts.advanced += 1
            _place(counts.advanced_dates, counts, "undated_advanced", when)
        if when is not None:
            counts.activity_dates.append(when)
        if target in _TOUCH_TARGETS:
            deal_id = _id_key(event.get("deal_id"))
            if deal_id is not None:
                touched.add(deal_id)

    for deal in deals:
        outcome = _outcome(deal)
        value, missing = _deal_value(deal)
        counts.deals += 1
        counts.by_stage[_stage(deal)] += 1
        if outcome != "disqualified":
            counts.leads += 1
            created = _occurred_on(deal.get("created_at"), tz)
            _place(counts.lead_dates, counts, "undated_leads", created)
            if created is not None:
                counts.activity_dates.append(created)
        if outcome == "open":
            counts.open += 1
            counts.open_value += value
            counts.open_missing_value += missing
        elif outcome == "won":
            counts.won += 1
            counts.won_value += value
            counts.won_missing_value += missing
            closed = _occurred_on(deal.get("closed_at"), tz)
            _place(counts.won_dates, counts, "undated_won", closed)
            if closed is None:
                counts.undated_won_value += value
            else:
                counts.won_value_points.append((closed, value))
                counts.activity_dates.append(closed)
        elif outcome == "lost":
            counts.lost += 1
            counts.lost_value += value
            counts.lost_missing_value += missing
            counts.lost_by_reason[_reason(deal)] += 1
            closed = _occurred_on(deal.get("closed_at"), tz)
            _place(counts.lost_dates, counts, "undated_lost", closed)
            if closed is not None:
                counts.activity_dates.append(closed)
        else:
            counts.disqualified += 1
            counts.disqualified_value += value
            counts.disqualified_missing_value += missing
            closed = _occurred_on(deal.get("closed_at"), tz)
            _place(counts.disqualified_dates, counts, "undated_disqualified", closed)
            if closed is not None:
                counts.activity_dates.append(closed)
        if _id_key(deal.get("id")) in touched:
            counts.contacted += 1
    return counts


def _place(bucket, counts, undated_attr, when):
    if when is None:
        setattr(counts, undated_attr, getattr(counts, undated_attr) + 1)
    else:
        bucket.append(when)


def _deal_value(deal):
    number = _num(deal.get("value"))
    if number is None:
        return 0, 1
    return number, 0


def _value_metric(total, missing, summed, formula):
    return _metric(
        total,
        formula,
        {"summed": summed, "missing_value": missing},
    )


# --- results and win/loss -------------------------------------------------

def _results(counts: _Counts) -> dict:
    stages = {key: counts.by_stage[key] for key in sorted(counts.by_stage)}
    not_touched = counts.deals - counts.contacted
    return {
        "deals": _metric(counts.deals, "count(deals)", {"deals": counts.deals}),
        "leads": _metric(
            counts.leads,
            "count(deals where outcome != disqualified)",
            {
                "deals": counts.deals,
                "disqualified": counts.disqualified,
                "counted": counts.leads,
            },
        ),
        "by_stage": _metric(
            stages,
            "count(deals) grouped by stage",
            {"deals": counts.deals},
        ),
        "contacted": _metric(
            counts.contacted,
            "count(deals with a contact, conversation, or advanced event)",
            {"deals": counts.deals, "touched": counts.contacted},
        ),
        "not_touched": _metric(
            not_touched,
            "count(deals with no contact, conversation, or advanced event)",
            {"deals": counts.deals, "touched": counts.contacted},
        ),
        "open": _outcome_metric(counts.open, "open"),
        "won": _outcome_metric(counts.won, "won"),
        "lost": _outcome_metric(counts.lost, "lost"),
        "disqualified": _outcome_metric(counts.disqualified, "disqualified"),
        "contacts": _metric(
            counts.contacts,
            "count(events where type is contact)",
            {"contact_events": counts.contact_events},
        ),
        "conversations": _metric(
            counts.conversations,
            "count(events where type is conversation)",
            {"conversation_events": counts.conversations},
        ),
        "advanced": _metric(
            counts.advanced,
            "count(events where type is advanced)",
            {"advanced_events": counts.advanced},
        ),
        "open_value": _value_metric(
            counts.open_value,
            counts.open_missing_value,
            counts.open - counts.open_missing_value,
            "sum(value of deals where outcome = open); missing values count as 0",
        ),
        "won_value": _value_metric(
            counts.won_value,
            counts.won_missing_value,
            counts.won - counts.won_missing_value,
            "sum(value of deals where outcome = won); missing values count as 0",
        ),
        "lost_value": _value_metric(
            counts.lost_value,
            counts.lost_missing_value,
            counts.lost - counts.lost_missing_value,
            "sum(value of deals where outcome = lost); missing values count as 0",
        ),
        "disqualified_value": _value_metric(
            counts.disqualified_value,
            counts.disqualified_missing_value,
            counts.disqualified - counts.disqualified_missing_value,
            "sum(value of deals where outcome = disqualified); missing values count as 0",
        ),
    }


def _outcome_metric(count, outcome):
    return _metric(
        count,
        f"count(deals where outcome = {outcome})",
        {"counted": count},
    )


def _win_loss(counts: _Counts, results: dict) -> dict:
    closed = counts.won + counts.lost
    if closed == 0:
        ratio = _metric(
            None,
            "won : lost",
            {"won": counts.won, "lost": counts.lost},
            note=NO_CLOSED_DEALS,
        )
        win_rate = _metric(
            None,
            "won / (won + lost) * 100",
            {"won": counts.won, "lost": counts.lost},
            note=NO_CLOSED_DEALS,
        )
    else:
        ratio = _metric(
            f"{counts.won} : {counts.lost}",
            "won : lost",
            {"won": counts.won, "lost": counts.lost},
        )
        win_rate = _metric(
            counts.won / closed * 100,
            "won / (won + lost) * 100",
            {"won": counts.won, "lost": counts.lost},
        )
    if counts.deals == 0:
        dq_rate = _metric(
            None,
            "disqualified / deals * 100",
            {"disqualified": counts.disqualified, "deals": counts.deals},
            note=NO_LEADS,
        )
    else:
        dq_rate = _metric(
            counts.disqualified / counts.deals * 100,
            "disqualified / deals * 100",
            {"disqualified": counts.disqualified, "deals": counts.deals},
        )
    reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            counts.lost_by_reason.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    return {
        "won": results["won"],
        "lost": results["lost"],
        "open": results["open"],
        "disqualified": results["disqualified"],
        "ratio": ratio,
        "win_rate": win_rate,
        "disqualification_rate": dq_rate,
        "lost_by_reason": _metric(
            reasons,
            "count(deals where outcome = lost) grouped by lost_reason",
            {"lost": counts.lost},
        ),
        "won_value": results["won_value"],
        "lost_value": results["lost_value"],
    }


# --- window ---------------------------------------------------------------

@dataclass
class _Window:
    start: date | None
    deadline: date | None
    today: date
    tz_name: str
    state: str
    days_total: int | None
    days_elapsed: int | None
    days_remaining: int | None
    percent_elapsed: float | None
    deadline_passed: bool
    not_started: bool
    total_note: str | None
    elapsed_note: str | None
    remaining_note: str | None
    percent_note: str | None
    unclamped_elapsed: int | None


def _build_window(start, deadline, today, tz_name) -> _Window:
    unclamped = None if start is None else (today - start).days
    base = dict(
        start=start,
        deadline=deadline,
        today=today,
        tz_name=tz_name,
        unclamped_elapsed=unclamped,
    )
    if start is None:
        return _Window(
            **base,
            state=STATE_NO_START,
            days_total=None,
            days_elapsed=None,
            days_remaining=None,
            percent_elapsed=None,
            deadline_passed=False,
            not_started=False,
            total_note=NO_START_DATE,
            elapsed_note=NO_START_DATE,
            remaining_note=NO_START_DATE,
            percent_note=NO_START_DATE,
        )
    if deadline is None:
        future = today < start
        elapsed = 0 if future else unclamped
        return _Window(
            **base,
            state=STATE_NO_DEADLINE,
            days_total=None,
            days_elapsed=elapsed,
            days_remaining=None,
            percent_elapsed=None,
            deadline_passed=False,
            not_started=future,
            total_note=NO_DEADLINE,
            elapsed_note=START_IN_FUTURE if future else (NO_ELAPSED_DAYS if elapsed == 0 else None),
            remaining_note=NO_DEADLINE,
            percent_note=NO_DEADLINE,
        )
    if deadline < start:
        return _Window(
            **base,
            state=STATE_ZERO_LENGTH,
            days_total=0,
            days_elapsed=0,
            days_remaining=0,
            percent_elapsed=None,
            deadline_passed=False,
            not_started=today < start,
            total_note=ZERO_LENGTH_WINDOW,
            elapsed_note=ZERO_LENGTH_WINDOW,
            remaining_note=ZERO_LENGTH_WINDOW,
            percent_note=ZERO_LENGTH_WINDOW,
        )

    total = (deadline - start).days + 1
    if today < start:
        return _Window(
            **base,
            state=STATE_NOT_STARTED,
            days_total=total,
            days_elapsed=0,
            days_remaining=total,
            percent_elapsed=0.0,
            deadline_passed=False,
            not_started=True,
            total_note=None,
            elapsed_note=START_IN_FUTURE,
            remaining_note=None,
            percent_note=None,
        )
    if today > deadline:
        return _Window(
            **base,
            state=STATE_ENDED,
            days_total=total,
            days_elapsed=total,
            days_remaining=0,
            percent_elapsed=100.0,
            deadline_passed=True,
            not_started=False,
            total_note=None,
            elapsed_note=DEADLINE_PASSED,
            remaining_note=DEADLINE_PASSED,
            percent_note=None,
        )
    elapsed = unclamped
    remaining = total - elapsed
    return _Window(
        **base,
        state=STATE_IN_PROGRESS,
        days_total=total,
        days_elapsed=elapsed,
        days_remaining=remaining,
        percent_elapsed=(elapsed / total * 100) if total else None,
        deadline_passed=False,
        not_started=False,
        total_note=None,
        elapsed_note=NO_ELAPSED_DAYS if elapsed == 0 else None,
        remaining_note=None,
        percent_note=None,
    )


def _window_payload(window: _Window) -> dict:
    date_inputs = {
        "start_date": _iso(window.start),
        "deadline": _iso(window.deadline),
        "today": _iso(window.today),
    }
    return {
        "start_date": _iso(window.start),
        "deadline": _iso(window.deadline),
        "today": _iso(window.today),
        "timezone": window.tz_name,
        "state": window.state,
        "deadline_passed": window.deadline_passed,
        "not_started": window.not_started,
        "days_total": _metric(
            window.days_total,
            "(deadline - start_date).days + 1",
            date_inputs,
            note=window.total_note,
        ),
        "days_elapsed": _metric(
            window.days_elapsed,
            "calendar days before today inside the window; 0 on the start date",
            {**date_inputs, "unclamped_days": window.unclamped_elapsed},
            note=window.elapsed_note,
        ),
        "days_remaining": _metric(
            window.days_remaining,
            "days_total - days_elapsed",
            {
                "days_total": window.days_total,
                "days_elapsed": window.days_elapsed,
            },
            note=window.remaining_note,
        ),
        "percent_elapsed": _metric(
            window.percent_elapsed,
            "days_elapsed / days_total * 100",
            {
                "days_elapsed": window.days_elapsed,
                "days_total": window.days_total,
            },
            note=window.percent_note,
        ),
    }


# --- pacing ---------------------------------------------------------------

def _pacing(targets, counts: _Counts, window: _Window, window_payload):
    rows = {}
    statuses = []
    for key in TARGET_KEYS:
        if key not in targets:
            continue
        target = targets[key]
        actual = _actual_for(key, counts)
        remaining = target - actual
        if target == 0:
            percent = _metric(
                None,
                "actual / target * 100",
                {"actual": actual, "target": target},
                note=TARGET_IS_ZERO,
            )
            percent_value = None
        else:
            percent_value = actual / target * 100
            percent = _metric(
                percent_value,
                "actual / target * 100",
                {"actual": actual, "target": target},
            )
        status = _status(percent_value, window.percent_elapsed)
        statuses.append(status)
        rows[key] = {
            "name": key,
            "target": _metric(target, "campaign target", {"target": target}),
            "actual": _actual_metric(key, counts),
            "remaining": _metric(
                remaining,
                "target - actual",
                {"target": target, "actual": actual},
            ),
            "percent_complete": percent,
            "days_total": window_payload["days_total"],
            "days_elapsed": window_payload["days_elapsed"],
            "days_remaining": window_payload["days_remaining"],
            "percent_elapsed": window_payload["percent_elapsed"],
            "required_rate": _rates(
                remaining,
                window.days_remaining,
                _required_note(window),
                "remaining",
                "days_remaining",
            ),
            "actual_rate": _rates(
                actual,
                window.days_elapsed,
                _actual_rate_note(window),
                "actual",
                "days_elapsed",
            ),
            "status": status,
            "status_inputs": {
                "percent_complete": percent_value,
                "percent_elapsed": window.percent_elapsed,
            },
            "projection": _projection(actual, window),
        }
    return rows, _rollup(statuses)


def _actual_for(key, counts: _Counts):
    return {
        "leads": counts.leads,
        "contacts": counts.contacts,
        "conversations": counts.conversations,
        "advanced": counts.advanced,
        "won": counts.won,
        "won_value": counts.won_value,
    }[key]


def _actual_metric(key, counts: _Counts):
    if key == "leads":
        return _metric(
            counts.leads,
            "count(deals where outcome != disqualified)",
            {
                "deals": counts.deals,
                "disqualified": counts.disqualified,
                "counted": counts.leads,
            },
        )
    if key == "contacts":
        return _metric(
            counts.contacts,
            "count(events where type is contact)",
            {"contact_events": counts.contacts},
        )
    if key == "conversations":
        return _metric(
            counts.conversations,
            "count(events where type is conversation)",
            {"conversation_events": counts.conversations},
        )
    if key == "advanced":
        return _metric(
            counts.advanced,
            "count(events where type is advanced)",
            {"advanced_events": counts.advanced},
        )
    if key == "won":
        return _metric(
            counts.won,
            "count(deals where outcome = won)",
            {"counted": counts.won},
        )
    return _value_metric(
        counts.won_value,
        counts.won_missing_value,
        counts.won - counts.won_missing_value,
        "sum(value of deals where outcome = won); missing values count as 0",
    )


def _required_note(window: _Window):
    if window.state == STATE_NO_START:
        return NO_START_DATE
    if window.state == STATE_NO_DEADLINE:
        return NO_DEADLINE
    if window.state == STATE_ZERO_LENGTH:
        return ZERO_LENGTH_WINDOW
    if window.deadline_passed or not window.days_remaining:
        return DEADLINE_PASSED
    return None


def _actual_rate_note(window: _Window):
    if window.state == STATE_NO_START:
        return NO_START_DATE
    if window.state == STATE_ZERO_LENGTH:
        return ZERO_LENGTH_WINDOW
    if window.not_started:
        return START_IN_FUTURE
    if not window.days_elapsed:
        return NO_ELAPSED_DAYS
    return None


def _projection_note(window: _Window):
    if window.state == STATE_NO_START:
        return NO_START_DATE
    if window.state == STATE_NO_DEADLINE:
        return NO_DEADLINE
    if window.state == STATE_ZERO_LENGTH:
        return ZERO_LENGTH_WINDOW
    if window.deadline_passed:
        return DEADLINE_PASSED
    if window.not_started:
        return START_IN_FUTURE
    if not window.days_elapsed or not window.days_total:
        return NO_ELAPSED_DAYS
    return None


def _rates(amount, days, note, amount_key, days_key):
    rates = {}
    for name, length in _PERIOD_LENGTH.items():
        inputs = {
            amount_key: amount,
            days_key: days,
            f"days_per_{name}": length,
        }
        formula = f"{amount_key} / {days_key} * {length}"
        if note or not days:
            rates[name] = _metric(None, formula, inputs, note=note or NO_ELAPSED_DAYS)
        else:
            rates[name] = _metric(amount / days * length, formula, inputs)
    return rates


def _projection(actual, window: _Window):
    note = _projection_note(window)
    inputs = {
        "actual": actual,
        "days_elapsed": window.days_elapsed,
        "days_total": window.days_total,
    }
    formula = "actual / days_elapsed * days_total"
    if note:
        return _metric(None, formula, inputs, note=note)
    return _metric(actual / window.days_elapsed * window.days_total, formula, inputs)


def _status(percent_complete, percent_elapsed):
    if percent_complete is None or percent_elapsed is None:
        return None
    if isclose(percent_complete, percent_elapsed, rel_tol=1e-9, abs_tol=1e-9):
        return ON_TRACK
    if percent_complete > percent_elapsed:
        return AHEAD
    return BEHIND


def _rollup(statuses):
    known = [status for status in statuses if status is not None]
    if not known:
        return None
    if BEHIND in known:
        return BEHIND
    if all(status == AHEAD for status in known):
        return AHEAD
    return ON_TRACK


# --- periods --------------------------------------------------------------

def _periods(grouping, window: _Window, targets, counts: _Counts):
    span_start, span_end = _span(window, counts.activity_dates)
    rows = []
    running = {key: 0 for key in targets}
    cursor = _period_start(span_start, grouping)
    last = _period_start(span_end, grouping)
    positive = (
        window.start is not None
        and window.deadline is not None
        and window.days_total
    )
    guard = 0
    while cursor <= last:
        guard += 1
        if guard > _MAX_PERIODS:
            raise ValueError("period span is too long")
        end = _period_end(cursor, grouping)
        overlap = _overlap_days(cursor, end, window) if positive else None
        covered = _covered_days(end, window) if positive else None
        actuals = {}
        required = {}
        cumulative_actual = {}
        cumulative_target = {}
        for key in targets:
            period_actual = _period_actual(key, counts, cursor, end)
            running[key] += period_actual
            actuals[key] = _metric(
                period_actual,
                _period_actual_formula(key),
                {"counted": period_actual},
            )
            required[key] = _required_period(targets[key], overlap, window)
            cumulative_actual[key] = _metric(
                running[key],
                "sum of this target's period actuals through this period",
                {"through_previous": running[key] - period_actual, "this_period": period_actual},
            )
            cumulative_target[key] = _cumulative_target(targets[key], covered, window)
        won = _dates_in(counts.won_dates, cursor, end)
        lost = _dates_in(counts.lost_dates, cursor, end)
        disqualified = _dates_in(counts.disqualified_dates, cursor, end)
        rows.append({
            "key": _period_key(cursor, grouping),
            "start": cursor.isoformat(),
            "end": end.isoformat(),
            "overlap_days": overlap,
            "actuals": actuals,
            "required": required,
            "cumulative_actual": cumulative_actual,
            "cumulative_target": cumulative_target,
            "won": _metric(
                won,
                "count(deals where outcome = won and closed_at is in this period)",
                {"counted": won},
            ),
            "lost": _metric(
                lost,
                "count(deals where outcome = lost and closed_at is in this period)",
                {"counted": lost},
            ),
            "disqualified": _metric(
                disqualified,
                "count(deals where outcome = disqualified and closed_at is in this period)",
                {"counted": disqualified},
            ),
            "win_rate": _period_win_rate(won, lost),
        })
        cursor = _next_period(cursor, grouping)
    return {
        "grouping": grouping,
        "window_days": window.days_total if positive else None,
        "undated": {
            "leads": counts.undated_leads,
            "contacts": counts.undated_contacts,
            "conversations": counts.undated_conversations,
            "advanced": counts.undated_advanced,
            "won": counts.undated_won,
            "won_value": counts.undated_won_value,
            "lost": counts.undated_lost,
            "disqualified": counts.undated_disqualified,
        },
        "rows": rows,
    }


def _period_actual_formula(key):
    if key == "leads":
        return "count(deals created in this period where outcome != disqualified)"
    if key == "contacts":
        return "count(contact events in this period)"
    if key == "conversations":
        return "count(conversation events in this period)"
    if key == "advanced":
        return "count(advanced events in this period)"
    if key == "won":
        return "count(deals where outcome = won and closed_at is in this period)"
    return "sum(value of won deals closed in this period); missing values count as 0"


def _period_actual(key, counts: _Counts, start, end):
    if key == "leads":
        return _dates_in(counts.lead_dates, start, end)
    if key == "contacts":
        return _dates_in(counts.contact_dates, start, end)
    if key == "conversations":
        return _dates_in(counts.conversation_dates, start, end)
    if key == "advanced":
        return _dates_in(counts.advanced_dates, start, end)
    if key == "won":
        return _dates_in(counts.won_dates, start, end)
    total = 0
    for when, value in counts.won_value_points:
        if start <= when <= end:
            total += value
    return total


def _dates_in(dates, start, end):
    return sum(1 for when in dates if start <= when <= end)


def _required_period(target, overlap, window: _Window):
    inputs = {
        "target": target,
        "overlap_days": overlap,
        "window_days": window.days_total if window.days_total else None,
    }
    formula = "target * overlap_days / window_days"
    if not window.days_total or overlap is None:
        return _metric(None, formula, inputs, note=_required_note(window) or ZERO_LENGTH_WINDOW)
    return _metric(target * overlap / window.days_total, formula, inputs)


def _cumulative_target(target, covered, window: _Window):
    inputs = {
        "target": target,
        "window_days_through_period_end": covered,
        "window_days": window.days_total if window.days_total else None,
    }
    formula = "target * window_days_through_period_end / window_days"
    if not window.days_total or covered is None:
        return _metric(None, formula, inputs, note=_required_note(window) or ZERO_LENGTH_WINDOW)
    return _metric(target * covered / window.days_total, formula, inputs)


def _period_win_rate(won, lost):
    closed = won + lost
    inputs = {"won": won, "lost": lost}
    if closed == 0:
        return _metric(None, "won / (won + lost) * 100", inputs, note=NO_CLOSED_DEALS)
    return _metric(won / closed * 100, "won / (won + lost) * 100", inputs)


def _span(window: _Window, activity_dates):
    points = list(activity_dates)
    if window.start is not None:
        points.append(window.start)
    if (
        window.deadline is not None
        and window.start is not None
        and window.deadline >= window.start
    ):
        points.append(window.deadline)
    elif window.deadline is not None and window.start is None:
        points.append(window.deadline)
    if not points:
        points.append(window.today)
    start, end = min(points), max(points)
    # No deadline: keep the table current once the campaign has started.
    if window.deadline is None and window.start is not None and window.today >= window.start:
        if window.today > end:
            end = window.today
    if window.deadline is None and window.start is None and window.today > end:
        end = window.today
    return start, end


def _overlap_days(period_start, period_end, window: _Window):
    first = max(period_start, window.start)
    last = min(period_end, window.deadline)
    if last < first:
        return 0
    return (last - first).days + 1


def _covered_days(period_end, window: _Window):
    last = min(period_end, window.deadline)
    if last < window.start:
        return 0
    return (last - window.start).days + 1


def _period_start(day, grouping):
    if grouping == "week":
        return day - timedelta(days=day.weekday())
    if grouping == "month":
        return date(day.year, day.month, 1)
    return date(day.year, 1, 1)


def _period_end(start, grouping):
    return _next_period(start, grouping) - timedelta(days=1)


def _next_period(start, grouping):
    if grouping == "week":
        return start + timedelta(days=7)
    if grouping == "month":
        if start.month == 12:
            return date(start.year + 1, 1, 1)
        return date(start.year, start.month + 1, 1)
    return date(start.year + 1, 1, 1)


def _period_key(start, grouping):
    if grouping == "week":
        iso = start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if grouping == "month":
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.year:04d}"
