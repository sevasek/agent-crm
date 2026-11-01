"""Timestamped intel snapshots on a partner.

A snapshot is one observation of one metric: a numeric value and/or a short
text value, the source it came from (`google-maps`, `website-check`,
`ad-library`), and the time it was captured. Rows are inserted and never
updated, so a later reading does not erase an earlier one.

Metric keys are free-form slugs (`google-rating`, `google-review-count`),
the same shape as tags. Nothing in the product hard-codes a key or a source.
ICP company-metric rules read the company partner (a person's parent
company), not a person's own rows — see `company_id_for`.
"""
import math
from datetime import datetime, timezone

from app.database import get_db
from app.services.auth import sanitize_text
from app.services.deal_tags import normalize_tag
from app.services.partners import get_partner

TEXT_MAX_LEN = 500
MAX_BATCH = 50
LIST_DEFAULT = 50
LIST_MAX = 200

ERROR_MESSAGES = {
    "not_found": "Partner not found.",
    "invalid_metric_key": (
        "Metric key must be a slug such as google-rating or google-review-count."
    ),
    "invalid_source": "Source must be a slug such as google-maps.",
    "invalid_value": "A snapshot needs a numeric value, a text value, or both.",
    "invalid_captured_at": (
        "captured_at must be a date or timestamp "
        "(2026-08-01 or 2026-08-01T15:04:00Z)."
    ),
    "empty_snapshots": "snapshots must be a non-empty array.",
    "too_many_snapshots": f"At most {MAX_BATCH} snapshots per call.",
    "invalid_snapshot": "Each snapshot must be an object.",
}


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _as_utc_naive(value):
    if value is None:
        return _utc_now()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def format_captured_at(value: datetime) -> str:
    """Sortable UTC timestamp. Text order matches time order."""
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def parse_captured_at(value):
    """Parse an explicit timestamp.

    Returns `(datetime, None)`, `(None, None)` when the value is missing,
    or `(None, "invalid_captured_at")`. A date alone (`YYYY-MM-DD`) is
    midnight UTC. `Z` and numeric offsets are converted to UTC. This does
    not substitute "now" — callers do that when a blank means "at write time".
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        parsed = _as_utc_naive(value)
        if parsed is None:
            return None, "invalid_captured_at"
        return parsed, None
    if not isinstance(value, str):
        return None, "invalid_captured_at"
    text = value.strip()
    if not text:
        return None, None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.strptime(text, "%Y-%m-%d"), None
        except ValueError:
            return None, "invalid_captured_at"
    normalised = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None, "invalid_captured_at"
    parsed = _as_utc_naive(parsed)
    if parsed is None:
        return None, "invalid_captured_at"
    return parsed, None


def captured_bound(value, *, end: bool):
    """Inclusive range edge. `(text, None)` or `(None, error_code)`.

    A missing value is no bound. A date-only end is the last second of that
    day so `captured_before=2026-08-01` includes snapshots taken that day.
    """
    parsed, error = parse_captured_at(value)
    if error:
        return None, error
    if parsed is None:
        return None, None
    date_only = isinstance(value, str) and len(value.strip()) == 10
    if date_only and end:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return format_captured_at(parsed), None


def _number_out(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number.is_integer() and abs(number) < 1e15:
        return int(number)
    return number


def parse_numeric(value):
    """(number, None) or (None, error). Blank is absent: (None, None)."""
    if isinstance(value, bool):
        return None, "invalid_value"
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        if text.lower() in ("nan", "inf", "infinity", "+inf", "-inf", "+infinity", "-infinity"):
            return None, "invalid_value"
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None, "invalid_value"
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None, "invalid_value"
    if not math.isfinite(number):
        return None, "invalid_value"
    return _number_out(number), None


def parse_text_value(value):
    if value is None:
        return None
    text = sanitize_text(str(value), max_len=TEXT_MAX_LEN)
    return text or None


def _snapshot_dict(row) -> dict:
    return {
        "id": row["id"],
        "partner_id": row["partner_id"],
        "metric_key": row["metric_key"],
        "numeric_value": _number_out(row["numeric_value"]),
        "text_value": row["text_value"],
        "source": row["source"],
        "captured_at": row["captured_at"],
        "created_at": row["created_at"],
    }


def company_id_for(partner) -> int | None:
    """The company whose snapshots an ICP company-metric rule reads.

    A company partner is itself. A person partner is the parent company.
    No linked company (or a parent who is not a company) means no metrics.
    """
    partner = partner or {}
    if partner.get("is_company"):
        partner_id = partner.get("id")
        return int(partner_id) if partner_id else None
    parent_id = partner.get("parent_id")
    if not parent_id:
        return None
    parent = get_partner(int(parent_id))
    if not parent or not parent.get("is_company") or not parent.get("id"):
        return None
    return int(parent["id"])


def _touch_partner(db, partner_id: int, now: datetime) -> None:
    db.execute(
        "UPDATE partners SET updated_at = ? WHERE id = ?",
        (format_captured_at(now), partner_id),
    )


def _prepare_snapshot(spec, *, now: datetime):
    if not isinstance(spec, dict):
        return None, "invalid_snapshot"
    partner_id = spec.get("partner_id")
    try:
        partner_id = int(partner_id)
    except (TypeError, ValueError):
        return None, "not_found"
    if isinstance(spec.get("partner_id"), bool) or partner_id < 1:
        return None, "not_found"
    metric_key = normalize_tag(spec.get("metric_key") if isinstance(spec.get("metric_key"), str) else "")
    if not metric_key:
        return None, "invalid_metric_key"
    source = normalize_tag(spec.get("source") if isinstance(spec.get("source"), str) else "")
    if not source:
        return None, "invalid_source"
    numeric, error = parse_numeric(spec.get("numeric_value"))
    if error:
        return None, error
    text_value = parse_text_value(spec.get("text_value"))
    if numeric is None and text_value is None:
        return None, "invalid_value"
    captured, error = parse_captured_at(spec.get("captured_at"))
    if error:
        return None, error
    if captured is None:
        captured = now
    return {
        "partner_id": partner_id,
        "metric_key": metric_key,
        "numeric_value": numeric,
        "text_value": text_value,
        "source": source,
        "captured_at": format_captured_at(captured),
    }, None


def record_snapshots(specs, *, now=None):
    """Append snapshots in one transaction. Nothing is written on error.

    Returns (rows, None) or (None, error) where error is
    `{"error": code, "index": i}` for a bad item.
    """
    if not isinstance(specs, list) or not specs:
        return None, {"error": "empty_snapshots", "index": None}
    if len(specs) > MAX_BATCH:
        return None, {"error": "too_many_snapshots", "index": None}
    moment = _as_utc_naive(now) or _utc_now()
    prepared = []
    for index, spec in enumerate(specs):
        row, error = _prepare_snapshot(spec, now=moment)
        if error:
            return None, {"error": error, "index": index}
        prepared.append(row)
    created_at = format_captured_at(moment)
    with get_db() as db:
        partner_ids = sorted({row["partner_id"] for row in prepared})
        placeholders = ",".join("?" * len(partner_ids))
        found = {
            int(r["id"])
            for r in db.execute(
                f"SELECT id FROM partners WHERE id IN ({placeholders})",
                partner_ids,
            ).fetchall()
        }
        for index, row in enumerate(prepared):
            if row["partner_id"] not in found:
                return None, {
                    "error": "not_found",
                    "index": index,
                    "partner_id": row["partner_id"],
                }
        ids = []
        for row in prepared:
            cursor = db.execute(
                """
                INSERT INTO partner_metric_snapshots
                    (partner_id, metric_key, numeric_value, text_value, source,
                     captured_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["partner_id"],
                    row["metric_key"],
                    row["numeric_value"],
                    row["text_value"],
                    row["source"],
                    row["captured_at"],
                    created_at,
                ),
            )
            ids.append(int(cursor.lastrowid))
        for partner_id in partner_ids:
            _touch_partner(db, partner_id, moment)
        db.commit()
    return snapshots_by_ids(ids), None


def record_snapshot(partner_id, metric_key, source, *, numeric_value=None,
                    text_value=None, captured_at=None, now=None):
    """Append one snapshot. Returns (row, None) or (None, error_code)."""
    rows, error = record_snapshots([{
        "partner_id": partner_id,
        "metric_key": metric_key,
        "source": source,
        "numeric_value": numeric_value,
        "text_value": text_value,
        "captured_at": captured_at,
    }], now=now)
    if error:
        return None, error["error"]
    return rows[0], None


def snapshots_by_ids(ids) -> list:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM partner_metric_snapshots WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
    by_id = {row["id"]: _snapshot_dict(row) for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def list_snapshots(partner_id, *, metric_key=None, captured_after=None,
                   captured_before=None, limit=None):
    """Newest first. `limit` None returns every matching row."""
    key = None
    if metric_key not in (None, ""):
        key = normalize_tag(metric_key if isinstance(metric_key, str) else "")
        if not key:
            return None, "invalid_metric_key"
    after, error = captured_bound(captured_after, end=False)
    if error:
        return None, error
    before, error = captured_bound(captured_before, end=True)
    if error:
        return None, error
    query = "SELECT * FROM partner_metric_snapshots WHERE partner_id = ?"
    params = [int(partner_id)]
    if key:
        query += " AND metric_key = ?"
        params.append(key)
    if after:
        query += " AND captured_at >= ?"
        params.append(after)
    if before:
        query += " AND captured_at <= ?"
        params.append(before)
    query += " ORDER BY captured_at DESC, id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return [_snapshot_dict(row) for row in rows], None


def latest_snapshots(partner_id, *, metric_key=None, captured_after=None,
                     captured_before=None):
    """Newest row per metric key inside the same filters as list_snapshots."""
    rows, error = list_snapshots(
        partner_id,
        metric_key=metric_key,
        captured_after=captured_after,
        captured_before=captured_before,
        limit=None,
    )
    if error:
        return None, error
    latest = {}
    for row in rows:
        latest.setdefault(row["metric_key"], row)
    return [latest[key] for key in sorted(latest)], None


def series_for_partner(partner_id: int) -> dict:
    """Every snapshot for a partner, oldest first, grouped by metric key.

    Scoring needs the whole history so a change-over-period rule can find
    the baseline. Listing for an agent uses `list_snapshots` instead.
    """
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM partner_metric_snapshots
            WHERE partner_id = ?
            ORDER BY captured_at ASC, id ASC
            """,
            (int(partner_id),),
        ).fetchall()
    grouped = {}
    for row in rows:
        item = _snapshot_dict(row)
        grouped.setdefault(item["metric_key"], []).append(item)
    return grouped


def recent_snapshots(partner_id: int, limit: int = 10) -> list:
    rows, _error = list_snapshots(partner_id, limit=limit)
    return rows or []
