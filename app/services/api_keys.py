import hashlib
import secrets

from app.database import get_db
from app.services.auth import sanitize_text

KEY_PREFIX = "crm_"


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_key(user_id: int, label: str):
    """Create a key, returning (row_dict, plaintext). Plaintext is shown once."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    label = sanitize_text(label, max_len=100) or "Unlabeled key"
    with get_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, label, key_hash, key_prefix) VALUES (?, ?, ?, ?)",
            (user_id, label, _hash_key(plaintext), plaintext[:12]),
        )
        db.commit()
        key_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row), plaintext


def list_api_keys(user_id: int):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_api_key(user_id: int, key_id: int) -> bool:
    with get_db() as db:
        cursor = db.execute(
            "DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)
        )
        db.commit()
        return cursor.rowcount > 0


def verify_api_key(plaintext: str):
    """Return the matching key row and mark it used, or None."""
    if not plaintext:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (_hash_key(plaintext),)
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM api_keys WHERE id = ?", (row["id"],)).fetchone())
