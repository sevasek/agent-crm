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
"""

import difflib
import math

from app.database import get_db, row_to_dict
from app.services.auth import sanitize_text
from app.services.partners import primary_social_url

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
}
NUMERIC_OPERATORS = {"eq", "gt", "gte", "lt", "lte"}
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


def _validate(field, operator, value):
    if field not in MATCHABLE_FIELDS:
        return "invalid_field"
    if operator not in OPERATORS:
        return "invalid_operator"
    if operator == "boolean" and str(value).strip().lower() not in ("true", "false"):
        return "invalid_value"
    if operator in NUMERIC_OPERATORS:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return "invalid_value"
        if not math.isfinite(parsed):
            return "invalid_value"
    if operator == "fuzzy_match" and not str(value or "").strip():
        return "invalid_value"
    return None


def create_criterion(field: str, operator: str, value: str = "", *, weight: int = 1,
                      label: str = "", active: bool = True):
    """Returns (criterion_dict, None) on success or (None, error_code) on failure."""
    error = _validate(field, operator, value)
    if error:
        return None, error
    with get_db() as db:
        db.execute("""
            INSERT INTO icp_criteria (label, field, operator, value, weight, active)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            sanitize_text(label, max_len=120) or None,
            field, operator,
            sanitize_text(str(value), max_len=200) or None,
            int(weight) if weight else 0,
            int(bool(active)),
        ))
        db.commit()
        criterion_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_criterion(criterion_id), None


def update_criterion(criterion_id, **fields):
    allowed = {"label", "field", "operator", "value", "weight", "active"}
    criterion = get_criterion(criterion_id)
    if not criterion:
        return None, "not_found"
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    merged_field = updates.get("field", criterion["field"])
    merged_operator = updates.get("operator", criterion["operator"])
    merged_value = updates.get("value", criterion["value"])
    error = _validate(merged_field, merged_operator, merged_value)
    if error:
        return None, error
    if "label" in updates:
        updates["label"] = sanitize_text(updates["label"], max_len=120) or None
    if "value" in updates:
        updates["value"] = sanitize_text(str(updates["value"]), max_len=200) or None
    if "weight" in updates:
        updates["weight"] = int(updates["weight"]) if updates["weight"] else 0
    if "active" in updates:
        updates["active"] = int(bool(updates["active"]))
    if not updates:
        return criterion, None
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


def build_matchable_row(partner: dict, deal: dict) -> dict:
    """Flatten the partner + deal fields ICP criteria can reference into one
    dict, keyed by MATCHABLE_FIELDS."""
    partner = partner or {}
    deal = deal or {}
    return {
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


def match_criterion(row: dict, field: str, operator: str, value) -> bool:
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
        try:
            actual_num = float(raw)
            target_num = float(value)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(actual_num) and math.isfinite(target_num)):
            return False
        if operator == "eq":
            return actual_num == target_num
        if operator == "gt":
            return actual_num > target_num
        if operator == "gte":
            return actual_num >= target_num
        if operator == "lt":
            return actual_num < target_num
        if operator == "lte":
            return actual_num <= target_num
    return False


def score_fit(row: dict, criteria=None):
    """criteria defaults to the active configured set. Returns score, max
    possible, and which criteria matched/didn't — same "formula, not a
    black box" shape as call_queue.score_deal."""
    if criteria is None:
        criteria = list_criteria(active_only=True)
    matched, unmatched = [], []
    total = 0
    max_total = 0
    for c in criteria:
        max_total += c["weight"]
        if match_criterion(row, c["field"], c["operator"], c["value"]):
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
