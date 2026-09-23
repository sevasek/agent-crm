"""Shared JSON-RPC / tool argument helpers. Never raise on bad bot input."""

import json
import math

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_ACTIVITIES = 20
MAX_INGEST = 50
MAX_BULK = 50
MAX_BODY_BYTES = 256 * 1024

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def json_safe(value):
    """sqlite Row / datetimes / bools → JSON-serializable. Never raises."""
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    try:
        return str(value)
    except Exception:
        return None


def dumps(value) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, separators=(",", ":"))


def as_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n", ""):
            return False
    return default


def as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    if isinstance(value, float) and math.isfinite(value):
        converted = int(value)
        return converted if _SQLITE_INT_MIN <= converted <= _SQLITE_INT_MAX else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            converted = int(text, 10)
        except (TypeError, ValueError):
            return None
        return converted if _SQLITE_INT_MIN <= converted <= _SQLITE_INT_MAX else None
    return None


def as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        if number.is_integer():
            converted = int(number)
            return converted if _SQLITE_INT_MIN <= converted <= _SQLITE_INT_MAX else None
        return number
    return None


def as_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        return str(value)
    return ""


def clamp_limit(value, default=DEFAULT_LIMIT, maximum=MAX_LIMIT):
    parsed = as_int(value)
    if parsed is None or parsed < 1:
        return default
    return min(parsed, maximum)


def paginate(items, limit):
    items = list(items)
    sliced = items[:limit]
    return sliced, len(items) > limit
