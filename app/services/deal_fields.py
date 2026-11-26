"""Custom deal fields: fields an install defines on its deals.

Definitions live in `deal_fields`; values live on `deals.custom_fields`, a
JSON object keyed by field key. A field can apply to every service or only
to some. One text field can be the card title (for example a child's first
name), which the pipeline card shows instead of the contact name.

Fields are archived, never deleted, so stored values stay readable.
"""
import json
import re
from datetime import date, datetime, timezone

from app.database import get_db
from app.services.auth import sanitize_text

FIELD_TYPES = {
    "text": "Text",
    "long_text": "Long text",
    "number": "Number",
    "date": "Date",
    "yes_no": "Yes/no",
    "choice": "Choice",
    "multi_choice": "Multi-choice",
}
CHOICE_TYPES = ("choice", "multi_choice")
MAX_CHOICES = 100
CHOICE_MAX_LEN = 80
LABEL_MAX_LEN = 80
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TEXT_MAX = {"text": 500, "long_text": 5000}
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def key_from_label(label: str) -> str:
    """`Child's first name` -> `childs_first_name`. May be invalid (e.g. `123`)."""
    text = re.sub(r"[^a-z0-9]+", "_", (label or "").lower().replace("'", "")).strip("_")
    return text[:40].rstrip("_")


def is_valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def _row_to_field(row):
    field = dict(row)
    field["choices"] = json.loads(field.get("choices") or "[]")
    raw_services = field.get("service_ids")
    field["service_ids"] = json.loads(raw_services) if raw_services else None
    for flag in ("required", "show_on_card", "is_title", "archived"):
        field[flag] = bool(field.get(flag))
    field["type_label"] = FIELD_TYPES.get(field["type"], field["type"])
    return field


def list_fields(include_archived: bool = False):
    query = "SELECT * FROM deal_fields"
    if not include_archived:
        query += " WHERE archived = 0"
    query += " ORDER BY position ASC, label ASC"
    with get_db() as db:
        return [_row_to_field(r) for r in db.execute(query).fetchall()]


def get_field(key):
    if not is_valid_key(key):
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM deal_fields WHERE key = ?", (key,)).fetchone()
    return _row_to_field(row) if row else None


def applies_to(field, service_id) -> bool:
    scope = field.get("service_ids")
    if not scope:
        return True
    return service_id is not None and int(service_id) in scope


def fields_for_service(service_id):
    return [f for f in list_fields() if applies_to(f, service_id)]


def title_field():
    for field in list_fields():
        if field["is_title"]:
            return field
    return None


def _as_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def _clean_choices(raw):
    if raw is None:
        return [], None
    if isinstance(raw, str):
        raw = [part for line in raw.splitlines() for part in line.split(",")]
    if not isinstance(raw, list):
        return None, "invalid_choices"
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            return None, "invalid_choices"
        text = sanitize_text(item, max_len=CHOICE_MAX_LEN)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    if len(out) > MAX_CHOICES:
        return None, "too_many_choices"
    return out, None


def _clean_service_ids(raw):
    if raw in (None, "", []):
        return None, None
    if not isinstance(raw, list):
        raw = [raw]
    ids = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            return None, "invalid_service"
        if value < 1:
            return None, "invalid_service"
        if value not in ids:
            ids.append(value)
    with get_db() as db:
        placeholders = ",".join("?" * len(ids))
        found = {r[0] for r in db.execute(
            f"SELECT id FROM services WHERE id IN ({placeholders})", ids).fetchall()}
    if found != set(ids):
        return None, "invalid_service"
    return sorted(ids), None


def define_field(key=None, *, label=None, type=None, choices=None, service_ids=None,
                 required=None, show_on_card=None, is_title=None, position=None,
                 archived=None, set_service_ids=False):
    """Create a field, or update the attributes passed (None = leave as is).

    The type is fixed once a field exists, so stored values never change
    meaning. Returns (field, error).
    """
    existing = get_field(key) if key else None
    if existing is None:
        label_text = sanitize_text(label or "", max_len=LABEL_MAX_LEN)
        if not label_text:
            return None, "label_required"
        key = key or key_from_label(label_text)
        if not is_valid_key(key):
            return None, "invalid_key"
        if type not in FIELD_TYPES:
            return None, "invalid_type"
    elif type is not None and type != existing["type"]:
        return None, "type_locked"
    field_type = existing["type"] if existing else type

    updates = {}
    if label is not None:
        label_text = sanitize_text(label, max_len=LABEL_MAX_LEN)
        if not label_text:
            return None, "label_required"
        updates["label"] = label_text
    if choices is not None or (existing is None and field_type in CHOICE_TYPES):
        cleaned, error = _clean_choices(choices)
        if error:
            return None, error
        if field_type in CHOICE_TYPES and not cleaned:
            return None, "choices_required"
        updates["choices"] = json.dumps(cleaned if field_type in CHOICE_TYPES else [])
    if set_service_ids or service_ids is not None:
        cleaned, error = _clean_service_ids(service_ids)
        if error:
            return None, error
        updates["service_ids"] = json.dumps(cleaned) if cleaned else None
    if required is not None:
        updates["required"] = int(_as_flag(required))
    if show_on_card is not None:
        updates["show_on_card"] = int(_as_flag(show_on_card))
    if is_title is not None:
        title = _as_flag(is_title)
        if title and field_type != "text":
            return None, "title_must_be_text"
        updates["is_title"] = int(title)
    if position is not None:
        try:
            updates["position"] = int(position)
        except (TypeError, ValueError):
            return None, "invalid_position"
    if archived is not None:
        updates["archived"] = int(_as_flag(archived))
        if updates["archived"]:
            updates["is_title"] = 0

    now = _now()
    with get_db() as db:
        if updates.get("is_title"):
            db.execute("UPDATE deal_fields SET is_title = 0 WHERE key != ?", (key,))
        if existing is None:
            if "position" not in updates:
                row = db.execute("SELECT MAX(position) FROM deal_fields").fetchone()
                updates["position"] = (row[0] if row[0] is not None else -1) + 1
            row = {"key": key, "type": field_type, "created_at": now, "updated_at": now, **updates}
            cols = ", ".join(row)
            db.execute(
                f"INSERT INTO deal_fields ({cols}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
        elif updates:
            updates["updated_at"] = now
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            db.execute(f"UPDATE deal_fields SET {set_clause} WHERE key = ?",
                       [*updates.values(), key])
        db.commit()
    return get_field(key), None


def _coerce(field, raw):
    """One value -> (stored value, error). None means 'clear this field'."""
    kind = field["type"]
    if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
        return None, None
    if kind in _TEXT_MAX:
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            return None, "invalid_value"
        text = sanitize_text(str(raw), max_len=_TEXT_MAX[kind], allow_newlines=kind == "long_text")
        return text or None, None
    if kind == "number":
        if isinstance(raw, bool):
            return None, "invalid_value"
        try:
            number = float(str(raw).strip())
        except ValueError:
            return None, "invalid_value"
        if number != number or number in (float("inf"), float("-inf")):
            return None, "invalid_value"
        return int(number) if number.is_integer() else number, None
    if kind == "date":
        try:
            return date.fromisoformat(str(raw).strip()[:10]).isoformat(), None
        except ValueError:
            return None, "invalid_value"
    if kind == "yes_no":
        if isinstance(raw, bool):
            return raw, None
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True, None
        if text in _FALSE:
            return False, None
        return None, "invalid_value"
    by_lower = {c.lower(): c for c in field["choices"]}
    if kind == "choice":
        if not isinstance(raw, str):
            return None, "invalid_value"
        match = by_lower.get(raw.strip().lower())
        return (match, None) if match else (None, "invalid_choice")
    # multi_choice
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list):
        return None, "invalid_value"
    out = []
    for item in items:
        if not isinstance(item, str):
            return None, "invalid_value"
        if not item.strip():
            continue
        match = by_lower.get(item.strip().lower())
        if not match:
            return None, "invalid_choice"
        if match not in out:
            out.append(match)
    return out or None, None


def validate_values(values, service_id, *, existing=None, check_required=True):
    """Check values against the definitions for this service.

    `values` maps field key -> raw value; a None or empty value clears the
    field. Returns (merged values, error). The error is `code:field_key`,
    for example `invalid_choice:room` or `required:date_of_birth`.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return None, "invalid_custom_fields:"
    merged = dict(existing or {})
    defined = {f["key"]: f for f in list_fields()}
    for key, raw in values.items():
        field = defined.get(key)
        if not field:
            return None, f"unknown_field:{key}"
        cleaned, error = _coerce(field, raw)
        if error:
            return None, f"{error}:{key}"
        if cleaned is not None and not applies_to(field, service_id):
            return None, f"field_not_for_service:{key}"
        if cleaned is None:
            if field["required"] and applies_to(field, service_id):
                return None, f"required:{key}"
            merged.pop(key, None)
        else:
            merged[key] = cleaned
    if check_required:
        for field in defined.values():
            if field["required"] and applies_to(field, service_id) and field["key"] not in merged:
                return None, f"required:{field['key']}"
    return merged, None


def error_message(error) -> str:
    code, _, key = (error or "").partition(":")
    field = get_field(key) if key else None
    name = field["label"] if field else key
    return {
        "unknown_field": f"There is no deal field {key!r}. Call list_catalog for the fields.",
        "invalid_value": f"{name}: that value is not valid for a {field['type_label'].lower() if field else 'this'} field.",
        "invalid_choice": f"{name}: pick one of {', '.join(field['choices']) if field else 'the defined choices'}.",
        "field_not_for_service": f"{name} does not apply to this deal's service.",
        "required": f"{name} is required.",
        "invalid_custom_fields": "custom_fields must be an object of field key to value.",
    }.get(code, error)


def load_values(deal) -> dict:
    raw = (deal or {}).get("custom_fields")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_values(values) -> str | None:
    return json.dumps(values, sort_keys=True) if values else None


def save_values(deal_id: int, values) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE deals SET custom_fields = ?, updated_at = ? WHERE id = ?",
            (dump_values(values), _now(), deal_id),
        )
        db.commit()


def display_value(field, value) -> str:
    if value is None:
        return ""
    if field["type"] == "yes_no":
        return "Yes" if value else "No"
    if field["type"] == "multi_choice" and isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def attach_custom_fields(deals):
    """Add custom_fields (dict), card_title and card_fields to each deal dict.

    card_title, not title: deals already have a title column (a lead's
    contact job title).
    """
    fields = list_fields()
    title = next((f for f in fields if f["is_title"]), None)
    on_card = [f for f in fields if f["show_on_card"] and not f["is_title"]]
    for deal in deals:
        values = load_values(deal)
        deal["custom_fields"] = values
        deal["card_title"] = values.get(title["key"]) if title else None
        deal["card_fields"] = [
            {"label": f["label"], "value": display_value(f, values[f["key"]])}
            for f in on_card if f["key"] in values
        ]
    return deals


def json_path(key: str) -> str:
    if not is_valid_key(key):
        raise ValueError(key)
    return f'$."{key}"'


def filter_sql(filters):
    """`{key: value}` -> (SQL fragment, params, error) for list_deals.

    Equality on the stored value; a multi-choice field matches when the
    value is one of its selections. An unknown key or value is an error.
    """
    if not filters:
        return "", [], None
    if not isinstance(filters, dict):
        return None, None, "invalid_custom_fields:"
    defined = {f["key"]: f for f in list_fields(include_archived=True)}
    sql, params = "", []
    for key, raw in filters.items():
        field = defined.get(key)
        if not field:
            return None, None, f"unknown_field:{key}"
        probe = dict(field, type="choice") if field["type"] == "multi_choice" else field
        value, error = _coerce(probe, raw)
        if error or value is None:
            return None, None, f"{error or 'invalid_value'}:{key}"
        if field["type"] == "multi_choice":
            sql += (" AND EXISTS (SELECT 1 FROM json_each(deals.custom_fields, ?)"
                    " WHERE json_each.value = ?)")
            params += [json_path(key), value]
        elif field["type"] == "yes_no":
            sql += " AND json_extract(deals.custom_fields, ?) = ?"
            params += [json_path(key), 1 if value else 0]
        else:
            sql += " AND json_extract(deals.custom_fields, ?) = ?"
            params += [json_path(key), value]
    return sql, params, None
