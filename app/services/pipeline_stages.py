"""The configurable deal pipeline: an ordered list of stages, each optionally
carrying one or more roles that used to be hardcoded stage names elsewhere in
the app (call-queue eligibility, nurture enrollment, won/lost).

Stage `key` is the immutable identifier stored on `deals.stage` and used by
the API; `label` is the display name and is the only thing "renaming" a
stage changes. Deleting a stage in use, or the last remaining stage, is
refused rather than orphaning deals.
"""

from app.database import get_db, row_to_dict
from app.services.auth import sanitize_text
import re

ROLE_FLAGS = ("is_default", "is_qualified_pool", "triggers_nurture", "is_won", "is_lost")


def is_valid_stage_key(key: str) -> bool:
    """Stage keys are not URL slugs — underscores are allowed (in_review)."""
    if not key or len(key) < 2 or len(key) > 50:
        return False
    return bool(re.match(r"^[a-z0-9_-]+$", key))


def list_stages():
    with get_db() as db:
        rows = db.execute("SELECT * FROM pipeline_stages ORDER BY position, id").fetchall()
        return [dict(r) for r in rows]


def get_stage(key: str):
    if not key:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM pipeline_stages WHERE key = ?", (key,)).fetchone()
        return row_to_dict(row)


def stage_keys() -> set:
    return {s["key"] for s in list_stages()}


def default_stage_key():
    stages = list_stages()
    for s in stages:
        if s["is_default"]:
            return s["key"]
    return stages[0]["key"] if stages else None


def _keys_with_role(role: str) -> set:
    return {s["key"] for s in list_stages() if s[role]}


def qualified_pool_keys() -> set:
    """All stages flagged `is_qualified_pool` — deliberately a union, not a
    single stage: the call queue is meant to work from every stage the
    business considers callable-qualified, however many there are."""
    return _keys_with_role("is_qualified_pool")


def closed_stage_keys() -> set:
    return _keys_with_role("is_won") | _keys_with_role("is_lost")


def create_stage(key: str, label: str, *, position=None, is_default=False,
                  is_qualified_pool=False, triggers_nurture=False, is_won=False, is_lost=False):
    """Returns (stage_dict, None) on success or (None, error_code) on failure."""
    key = (key or "").strip().lower()
    label = sanitize_text(label, max_len=60)
    if not is_valid_stage_key(key):
        return None, "invalid_key"
    if not label:
        return None, "label_required"
    if is_won and is_lost:
        return None, "won_lost_conflict"
    with get_db() as db:
        if db.execute("SELECT id FROM pipeline_stages WHERE key = ?", (key,)).fetchone():
            return None, "duplicate_key"
        if position is None:
            row = db.execute("SELECT COALESCE(MAX(position), -1) + 1 AS next FROM pipeline_stages").fetchone()
            position = row["next"]
        if is_default:
            db.execute("UPDATE pipeline_stages SET is_default = 0")
        db.execute("""
            INSERT INTO pipeline_stages
                (key, label, position, is_default, is_qualified_pool, triggers_nurture, is_won, is_lost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            key, label, position,
            int(bool(is_default)), int(bool(is_qualified_pool)),
            int(bool(triggers_nurture)), int(bool(is_won)), int(bool(is_lost)),
        ))
        db.commit()
    return get_stage(key), None


def update_stage(key: str, **fields):
    """Update label and/or role flags. The key itself is immutable — it's
    what `deals.stage` and callers reference, so "renaming" a stage only
    ever changes its label."""
    allowed = {"label", "position", *ROLE_FLAGS}
    stage = get_stage(key)
    if not stage:
        return None, "not_found"
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "label" in updates:
        label = sanitize_text(updates["label"], max_len=60)
        if not label:
            return None, "label_required"
        updates["label"] = label
    for flag in ROLE_FLAGS:
        if flag in updates:
            updates[flag] = int(bool(updates[flag]))
    resulting_won = updates.get("is_won", stage["is_won"])
    resulting_lost = updates.get("is_lost", stage["is_lost"])
    if resulting_won and resulting_lost:
        return None, "won_lost_conflict"
    if not updates:
        return stage, None
    with get_db() as db:
        if updates.get("is_default"):
            db.execute("UPDATE pipeline_stages SET is_default = 0 WHERE key != ?", (key,))
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [key]
        db.execute(
            f"UPDATE pipeline_stages SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            values,
        )
        db.commit()
    return get_stage(key), None


def delete_stage(key: str):
    """Returns (True, None) or (False, error_code). Refuses to delete a stage
    that any deal currently sits in, or the last remaining stage."""
    stage = get_stage(key)
    if not stage:
        return False, "not_found"
    with get_db() as db:
        in_use = db.execute("SELECT COUNT(*) AS n FROM deals WHERE stage = ?", (key,)).fetchone()["n"]
        if in_use:
            return False, "stage_in_use"
        total = db.execute("SELECT COUNT(*) AS n FROM pipeline_stages").fetchone()["n"]
        if total <= 1:
            return False, "last_stage"
        db.execute("DELETE FROM pipeline_stages WHERE key = ?", (key,))
        db.commit()
    return True, None


def move_stage(key: str, direction: str):
    """Swap position with the previous ('up') or next ('down') stage."""
    stages = list_stages()
    index = next((i for i, s in enumerate(stages) if s["key"] == key), None)
    if index is None:
        return False, "not_found"
    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(stages):
        return False, "cannot_move"
    a, b = stages[index], stages[target]
    with get_db() as db:
        db.execute("UPDATE pipeline_stages SET position = ? WHERE key = ?", (b["position"], a["key"]))
        db.execute("UPDATE pipeline_stages SET position = ? WHERE key = ?", (a["position"], b["key"]))
        db.commit()
    return True, None


def reorder_stages(ordered_keys):
    """Set the full stage order in one call — the shape an API/agent wants,
    vs. the admin UI's one-step-at-a-time move_stage()."""
    existing = stage_keys()
    if set(ordered_keys) != existing or len(ordered_keys) != len(existing):
        return False, "key_set_mismatch"
    with get_db() as db:
        for i, key in enumerate(ordered_keys):
            db.execute("UPDATE pipeline_stages SET position = ? WHERE key = ?", (i, key))
        db.commit()
    return True, None
