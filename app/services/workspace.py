"""Instance-wide settings (`workspace_settings`), shared by every user.

Per-user preferences such as currency stay on `users`. The first setting
here is the organisation label: what a company-type contact is called on
this install (Company, Family, Household...). The data model keeps
`is_company`; only the words on screen and in agent output change.
"""
import sqlite3

from app.database import get_db
from app.services.auth import sanitize_text

DEFAULT_ORG_LABEL = {"singular": "Company", "plural": "Companies"}
ORG_LABEL_PRESETS = (
    ("Company", "Companies"),
    ("Organisation", "Organisations"),
    ("Family", "Families"),
    ("Household", "Households"),
)
LABEL_MAX_LEN = 40


def get_setting(key: str, default=None):
    with get_db() as db:
        try:
            row = db.execute(
                "SELECT value FROM workspace_settings WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Table missing (a process that hasn't migrated yet).
            return default
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO workspace_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        db.commit()


def default_plural(singular: str) -> str:
    lower = singular.lower()
    if lower.endswith("y") and len(lower) > 1 and lower[-2] not in "aeiou":
        return singular[:-1] + "ies"
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return singular + "es"
    return singular + "s"


def org_label() -> dict:
    """{"singular": "Family", "plural": "Families"} for templates and agents."""
    singular = get_setting("org_label_singular") or DEFAULT_ORG_LABEL["singular"]
    plural = get_setting("org_label_plural") or (
        DEFAULT_ORG_LABEL["plural"] if singular == DEFAULT_ORG_LABEL["singular"]
        else default_plural(singular)
    )
    return {"singular": singular, "plural": plural}


def set_org_label(singular: str, plural: str = ""):
    """Returns (label, error). An empty plural is derived from the singular."""
    singular = sanitize_text(singular or "", max_len=LABEL_MAX_LEN)
    if not singular:
        return None, "label_required"
    plural = sanitize_text(plural or "", max_len=LABEL_MAX_LEN) or default_plural(singular)
    set_setting("org_label_singular", singular)
    set_setting("org_label_plural", plural)
    return org_label(), None
