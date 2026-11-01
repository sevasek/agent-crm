"""A deterministic, transparent ICP fit score — explicitly NOT a
learned/predictive model (same "formula, not AI" line the call-priority
score in app.services.call_queue draws, and the same decision recorded in
docs/SCOPE.md against predictive lead scoring).

Criteria are configured at /admin/icp, not hardcoded: each names a field
(from MATCHABLE_FIELDS), an operator (from OPERATORS), a comparison value
(ignored for `is_not_null`), and a weight. A lead's fit score is the sum of
weights for criteria that match — there's no pass/fail threshold enforced
here, just a number and the reasons behind it, same shape as the call
queue's score. Order doesn't affect the result, since it's a plain sum.

Tag rules are the same shape. `company_tag` / `deal_tag` with operator
`has_tag` and a tag slug add that rule's weight when the tag is present.
The slug is any valid tag (`google-ads`, `campaign:spring`), not a fixed list.

Company-metric rules are the same idea for timestamped intel. `company_metric`
names a free-form metric slug (`google-rating`) in `metric_key`. Numeric
operators compare the company's latest numeric snapshot. When `period_days`
is set, the same operators compare the change over that many days (latest
minus the reading from the start of the window). A person partner uses the
parent company's snapshots, same as `company_tag`.
"""

import difflib
import math
from datetime import datetime, timedelta, timezone

from app.database import get_db, row_to_dict
from app.services.auth import sanitize_text
from app.services.deal_tags import normalize_tag, tags_for_deal
from app.services.partner_metrics import company_id_for, parse_captured_at, series_for_partner
from app.services.partner_tags import tags_for_partner
from app.services.partners import get_partner, primary_social_url

MATCHABLE_FIELDS = {
    "industry": "Industry",
    "team_size": "Team size",
    "is_company": "Is a company (not an individual)",
    "preferred_channel": "Preferred contact channel",
    "email": "Has an email",
    "phone": "Has a phone",
    "website": "Website",
    "social_url": "Social URL",
    "source": "Lead source",
    "value_estimate": "Deal value estimate",
    "pain_points": "Pain points (free text)",
    "goals": "Goals (free text)",
    "company_tag": "Company has tag",
    "deal_tag": "Lead or deal has tag",
    "company_metric": "Company metric",
}

OPERATORS = {
    "is_not_null": "is set (not blank)",
    "boolean": "is true / false",
    "fuzzy_match": "roughly matches",
    "eq": "= (number)",
    "gt": "> (number)",
    "gte": ">= (number)",
    "lt": "< (number)",
    "lte": "<= (number)",
    "has_tag": "has this tag",
}

# Tag fields store the slug in `value` and only accept `has_tag`.
# `has_tag` is not a comparison for the profile fields above.
TAG_FIELDS = {"company_tag", "deal_tag"}

# Company-metric rules store the slug in `metric_key`, not in `value`.
# `value` is the number. `period_days` empty means "latest snapshot";
# a positive day count means the change over that window.
METRIC_FIELD = "company_metric"
MAX_PERIOD_DAYS = 3650

ERROR_MESSAGES = {
    "invalid_field": "Unknown field.",
    "invalid_operator": "Unknown operator.",
    "invalid_value": (
        "Value doesn't match what this operator needs "
        "(a number, true/false, or a tag slug such as google-ads)."
    ),
    "tag_operator": (
        "Company and lead/deal tag rules use the has_tag operator. "
        "has_tag does not apply to other fields."
    ),
    "metric_operator": (
        "Company metric rules use a numeric operator: eq, gt, gte, lt, or lte."
    ),
    "metric_fields": (
        "metric_key and period_days apply only to a company metric rule."
    ),
    "invalid_metric_key": (
        "Metric key must be a slug such as google-rating or google-review-count."
    ),
    "invalid_period": (
        "Period must be a whole number of days, from 1 to 3650, "
        "or blank to test the latest value."
    ),
    "invalid_weight": "Weight must be a whole number.",
    "not_found": "Criterion not found.",
}
NUMERIC_OPERATORS = {"eq", "gt", "gte", "lt", "lte"}
_OP_SYMBOL = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
FUZZY_MATCH_THRESHOLD = 0.6


def list_criteria(active_only: bool = False):
    query = "SELECT * FROM icp_criteria"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY weight DESC, id"
    with get_db() as db:
        return [dict(r) for r in db.execute(query).fetchall()]


def get_criterion(criterion_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM icp_criteria WHERE id = ?", (criterion_id,)).fetchone()
        return row_to_dict(row)


def criterion_display(criterion) -> str:
    """Label shown beside a fit score. A tag rule with no label names the slug."""
    if not criterion:
        return ""
    label = (criterion.get("label") or "").strip()
    if label:
        return label
    if criterion.get("operator") == "has_tag" and criterion.get("field") in TAG_FIELDS:
        kind = "Company has tag" if criterion.get("field") == "company_tag" else "Lead or deal has tag"
        return f"{kind} {criterion.get('value') or ''}".strip()
    if criterion.get("field") == METRIC_FIELD:
        return _metric_rule_display(criterion)
    return criterion.get("field") or ""


def _metric_rule_display(criterion) -> str:
    key = criterion.get("metric_key") or "metric"
    symbol = _OP_SYMBOL.get(criterion.get("operator") or "", criterion.get("operator") or "")
    value = criterion.get("value") if criterion.get("value") is not None else ""
    days = criterion.get("period_days")
    if days:
        return f"{key} change {symbol} {value} over {int(days)} days"
    return f"{key} {symbol} {value}".strip()


def _tag_list(value) -> list:
    """Normalize a tag collection. Drops anything that is not a slug."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = normalize_tag(item)
        if tag and tag not in out:
            out.append(tag)
    return out


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("nan", "inf", "infinity", "+inf", "-inf", "+infinity", "-infinity"):
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return None
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _stored_number(value):
    parsed = _finite_number(value)
    if parsed is None:
        return None
    if parsed.is_integer() and abs(parsed) < 1e15:
        return str(int(parsed))
    return str(parsed)


def _clean_period(period_days):
    """(days or None, error or None). Blank means no window."""
    if period_days in (None, ""):
        return None, None
    if isinstance(period_days, bool):
        return None, "invalid_period"
    if isinstance(period_days, float):
        if not period_days.is_integer():
            return None, "invalid_period"
    if isinstance(period_days, str):
        text = period_days.strip()
        if not text:
            return None, None
        if not text.isdigit():
            return None, "invalid_period"
        period_days = int(text)
    try:
        days = int(period_days)
    except (TypeError, ValueError):
        return None, "invalid_period"
    if days < 1 or days > MAX_PERIOD_DAYS:
        return None, "invalid_period"
    return days, None


def _blank_metric(value) -> bool:
    return value in (None, "")


def _stored_value(field, value):
    if field in TAG_FIELDS:
        return normalize_tag(str(value if value is not None else ""))
    if field == METRIC_FIELD:
        return _stored_number(value)
    if value is None:
        return None
    return sanitize_text(str(value), max_len=200) or None


def _stored_metric_key(field, metric_key):
    if field != METRIC_FIELD:
        return None
    if not isinstance(metric_key, str):
        metric_key = "" if metric_key is None else str(metric_key)
    return normalize_tag(metric_key)


def _validate(field, operator, value, *, metric_key=None, period_days=None):
    if field not in MATCHABLE_FIELDS:
        return "invalid_field"
    if operator not in OPERATORS:
        return "invalid_operator"
    if field in TAG_FIELDS or operator == "has_tag":
        if field not in TAG_FIELDS or operator != "has_tag":
            return "tag_operator"
        if not normalize_tag(str(value if value is not None else "")):
            return "invalid_value"
        return None
    if field != METRIC_FIELD and (not _blank_metric(metric_key) or not _blank_metric(period_days)):
        return "metric_fields"
    if field == METRIC_FIELD:
        if operator not in NUMERIC_OPERATORS:
            return "metric_operator"
        if not _stored_metric_key(field, metric_key):
            return "invalid_metric_key"
        _days, error = _clean_period(period_days)
        if error:
            return error
        if _finite_number(value) is None:
            return "invalid_value"
        return None
    if operator == "boolean" and str(value).strip().lower() not in ("true", "false"):
        return "invalid_value"
    if operator in NUMERIC_OPERATORS:
        if _finite_number(value) is None:
            return "invalid_value"
    if operator == "fuzzy_match" and not str(value or "").strip():
        return "invalid_value"
    return None


def create_criterion(field: str, operator: str, value: str = "", *, weight: int = 1,
                      label: str = "", active: bool = True, metric_key=None,
                      period_days=None):
    """Returns (criterion_dict, None) on success or (None, error_code) on failure."""
    error = _validate(field, operator, value, metric_key=metric_key, period_days=period_days)
    if error:
        return None, error
    days, _period_error = _clean_period(period_days if field == METRIC_FIELD else None)
    with get_db() as db:
        db.execute("""
            INSERT INTO icp_criteria
                (label, field, operator, value, weight, active, metric_key, period_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sanitize_text(label, max_len=120) or None,
            field, operator,
            _stored_value(field, value),
            int(weight) if weight else 0,
            int(bool(active)),
            _stored_metric_key(field, metric_key),
            days,
        ))
        db.commit()
        criterion_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_criterion(criterion_id), None


def update_criterion(criterion_id, **fields):
    allowed = {"label", "field", "operator", "value", "weight", "active", "metric_key", "period_days"}
    criterion = get_criterion(criterion_id)
    if not criterion:
        return None, "not_found"
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    merged_field = updates.get("field", criterion["field"])
    merged_operator = updates.get("operator", criterion["operator"])
    merged_value = updates.get("value", criterion["value"])
    merged_metric = updates["metric_key"] if "metric_key" in updates else criterion.get("metric_key")
    merged_period = updates["period_days"] if "period_days" in updates else criterion.get("period_days")
    if merged_metric == "":
        merged_metric = None
    if merged_period == "":
        merged_period = None
    # Switching off a company-metric rule drops the slug and the window.
    # Sending a new slug or period while the field is something else is an error.
    if merged_field != METRIC_FIELD:
        explicit_key = "metric_key" in updates and updates.get("metric_key") not in ("", None)
        explicit_period = "period_days" in updates and updates.get("period_days") not in ("", None)
        if explicit_key or explicit_period:
            return None, "metric_fields"
        merged_metric = None
        merged_period = None
    error = _validate(
        merged_field, merged_operator, merged_value,
        metric_key=merged_metric, period_days=merged_period,
    )
    if error:
        return None, error
    if not updates:
        return criterion, None
    if "label" in updates:
        updates["label"] = sanitize_text(updates["label"], max_len=120) or None
    if "value" in updates or "field" in updates:
        updates["value"] = _stored_value(merged_field, merged_value)
    if "weight" in updates:
        updates["weight"] = int(updates["weight"]) if updates["weight"] else 0
    if "active" in updates:
        updates["active"] = int(bool(updates["active"]))
    updates["metric_key"] = _stored_metric_key(merged_field, merged_metric)
    days, _period_error = _clean_period(merged_period if merged_field == METRIC_FIELD else None)
    updates["period_days"] = days
    with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [criterion_id]
        db.execute(
            f"UPDATE icp_criteria SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            values,
        )
        db.commit()
    return get_criterion(criterion_id), None


def delete_criterion(criterion_id):
    criterion = get_criterion(criterion_id)
    if not criterion:
        return False, "not_found"
    with get_db() as db:
        db.execute("DELETE FROM icp_criteria WHERE id = ?", (criterion_id,))
        db.commit()
    return True, None


def company_tags_for(partner, company_tags=None) -> list:
    """Tags on the company partner.

    A company partner contributes its own tags. A person partner contributes
    the parent company's tags, not the person's own tags. No linked company
    means no tags. `company_tags` skips the lookup when the caller already
    has the list (including an empty list).
    """
    if company_tags is not None:
        return _tag_list(company_tags)
    partner = partner or {}
    if partner.get("is_company"):
        if isinstance(partner.get("tags"), list):
            return _tag_list(partner["tags"])
        if partner.get("id"):
            return tags_for_partner(int(partner["id"]))
        return []
    parent_id = partner.get("parent_id")
    if not parent_id:
        return []
    parent = get_partner(int(parent_id))
    if not parent or not parent.get("is_company") or not parent.get("id"):
        return []
    return tags_for_partner(int(parent["id"]))


def deal_tags_for(deal, deal_tags=None) -> list:
    """Tags on the lead or deal itself. A passed list, including [], is used as-is."""
    if deal_tags is not None:
        return _tag_list(deal_tags)
    deal = deal or {}
    if isinstance(deal.get("tags"), list):
        return _tag_list(deal["tags"])
    if deal.get("id"):
        return tags_for_deal(int(deal["id"]))
    return []


def _company_metric_series(partner, company_metric_series):
    """History the company-metric rules read. A passed dict, including {}, is used as-is."""
    if company_metric_series is not None:
        return company_metric_series if isinstance(company_metric_series, dict) else {}
    company_id = company_id_for(partner)
    if not company_id:
        return {}
    return series_for_partner(company_id)


def build_matchable_row(partner: dict, deal: dict, *, company_tags=None, deal_tags=None,
                        company_metric_series=None) -> dict:
    """Flatten the partner + deal fields ICP criteria can reference into one
    dict, keyed by MATCHABLE_FIELDS.

    A lead scores profile fields from its own columns only (ADR §5.7 / Q12).
    A linked partner never fills empty lead fields (D10). Tag rules are the
    exception: `deal_tag` is this record's tags, and `company_tag` is the
    linked company (or the parent company of a linked person). Both are lists
    of slugs on the row; `has_tag` checks membership.

    `company_metric` is the same exception. `company_metric_series` is the
    company's snapshots grouped by metric key (oldest first). A person uses
    the parent company. Passing `company_metric_series` (including {}) skips
    the lookup.
    """
    partner = partner or {}
    deal = deal or {}
    tag_values = {
        "company_tag": company_tags_for(partner, company_tags),
        "deal_tag": deal_tags_for(deal, deal_tags),
        "company_metric_series": _company_metric_series(partner, company_metric_series),
    }
    if deal.get("type") == "lead":
        is_company = bool(
            (deal.get("company_name") or "").strip()
            and not (deal.get("contact_name") or "").strip()
        )
        social = (
            deal.get("linkedin_url") or deal.get("x_url") or deal.get("instagram_url")
            or deal.get("facebook_url") or deal.get("youtube_url")
        )
        row = {
            "industry": deal.get("industry"),
            "team_size": deal.get("team_size"),
            "is_company": is_company,
            "preferred_channel": deal.get("preferred_channel"),
            "email": deal.get("email"),
            "phone": deal.get("phone"),
            "website": deal.get("website"),
            "social_url": social,
            "source": deal.get("source"),
            "value_estimate": deal.get("value_estimate"),
            "pain_points": deal.get("pain_points"),
            "goals": deal.get("goals"),
        }
        row.update(tag_values)
        return row
    row = {
        "industry": partner.get("industry"),
        "team_size": partner.get("team_size"),
        "is_company": partner.get("is_company"),
        "preferred_channel": partner.get("preferred_channel"),
        "email": partner.get("email"),
        "phone": partner.get("phone"),
        "website": partner.get("website"),
        "social_url": primary_social_url(partner),
        "source": deal.get("source"),
        "value_estimate": deal.get("value_estimate"),
        "pain_points": deal.get("pain_points"),
        "goals": deal.get("goals"),
    }
    row.update(tag_values)
    return row


def _scoring_now(now):
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        return now.replace(microsecond=0)
    parsed, error = parse_captured_at(now)
    if error or parsed is None:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    return parsed


def _numeric_points(series, now):
    """(captured_at, id, number) for snapshots at or before `now`, oldest first."""
    points = []
    for item in series or []:
        if not isinstance(item, dict):
            continue
        captured, error = parse_captured_at(item.get("captured_at"))
        if error or captured is None or captured > now:
            continue
        number = _finite_number(item.get("numeric_value"))
        if number is None:
            continue
        try:
            row_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            row_id = 0
        points.append((captured, row_id, number))
    points.sort()
    return points


def _metric_actual(row, metric_key, period_days, now):
    """The number a company-metric rule compares, or None when it cannot.

    No period: the newest numeric snapshot at or before `now`.
    With a period: latest minus the newest numeric snapshot at or before
    `now - period`. The latest reading must fall strictly inside the window.
    A missing endpoint means the rule does not match.
    """
    key = normalize_tag(metric_key if isinstance(metric_key, str) else "")
    if not key:
        return None
    series = (row.get("company_metric_series") or {}).get(key) or []
    points = _numeric_points(series, now)
    if not points:
        return None
    days, error = _clean_period(period_days)
    if error:
        return None
    latest = points[-1][2]
    if days is None:
        return latest
    cutoff = now - timedelta(days=days)
    if points[-1][0] <= cutoff:
        return None
    baseline = None
    for captured, _row_id, number in points:
        if captured <= cutoff:
            baseline = number
        else:
            break
    if baseline is None:
        return None
    return round(latest - baseline, 6)


def _compare_number(actual, operator, value) -> bool:
    target = _finite_number(value)
    actual_num = _finite_number(actual)
    if actual_num is None or target is None:
        return False
    if operator == "eq":
        return actual_num == target
    if operator == "gt":
        return actual_num > target
    if operator == "gte":
        return actual_num >= target
    if operator == "lt":
        return actual_num < target
    if operator == "lte":
        return actual_num <= target
    return False


def match_criterion(row: dict, field: str, operator: str, value, *,
                    metric_key=None, period_days=None, now=None) -> bool:
    if field == METRIC_FIELD:
        moment = _scoring_now(now)
        actual = _metric_actual(row, metric_key, period_days, moment)
        return _compare_number(actual, operator, value)
    if operator == "has_tag":
        wanted = normalize_tag(str(value if value is not None else ""))
        if not wanted:
            return False
        return wanted in _tag_list(row.get(field))
    raw = row.get(field)
    if operator == "is_not_null":
        return raw is not None and str(raw).strip() != ""
    if operator == "boolean":
        expected = str(value).strip().lower() == "true"
        if isinstance(raw, str):
            actual = raw.strip().lower() not in ("", "0", "false", "none", "no")
        else:
            actual = bool(raw)
        return actual == expected
    if operator == "fuzzy_match":
        if raw is None:
            return False
        a = str(raw).strip().lower()
        b = str(value or "").strip().lower()
        if not a or not b:
            return False
        if b in a or a in b:
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_MATCH_THRESHOLD
    if operator in NUMERIC_OPERATORS:
        return _compare_number(raw, operator, value)
    return False


def score_fit(row: dict, criteria=None, *, now=None):
    """criteria defaults to the active configured set. Returns score, max
    possible, and which criteria matched/didn't — same "formula, not a
    black box" shape as call_queue.score_deal.

    `now` is the clock company-metric windows are measured from. The default
    is the current UTC time, so a rule for the last 90 days means the last
    90 days as of the moment the score is calculated.
    """
    if criteria is None:
        criteria = list_criteria(active_only=True)
    matched, unmatched = [], []
    total = 0
    max_total = 0
    for c in criteria:
        max_total += c["weight"]
        if match_criterion(
            row, c["field"], c["operator"], c["value"],
            metric_key=c.get("metric_key"),
            period_days=c.get("period_days"),
            now=now,
        ):
            total += c["weight"]
            matched.append(c)
        else:
            unmatched.append(c)
    return {
        "score": total,
        "max": max_total,
        "matched": matched,
        "unmatched": unmatched,
        "criteria": criteria,
    }
