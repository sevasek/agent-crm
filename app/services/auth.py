from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
import hashlib
import logging
import math
import os
import re
import secrets as secrets_module
import time
from datetime import datetime

from app.database import get_db

logger = logging.getLogger(__name__)

# PBKDF2-SHA256: a well-tested default.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
csrf_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="csrf")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_user_by_email(email: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(email: str, name: str, password: str) -> int:
    password_hash = hash_password(password)
    clean_name = sanitize_text(name, max_len=120) if name else None
    with get_db() as db:
        db.execute(
            "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
            (email.strip().lower(), clean_name, password_hash),
        )
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def count_users() -> int:
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def maybe_bootstrap_admin() -> None:
    """Create the first admin from env when the users table is empty.

    Production sudoers cannot `compose exec`, and create_admin.py is
    interactive (getpass). Set BOOTSTRAP_ADMIN_EMAIL + BOOTSTRAP_ADMIN_PASSWORD
    (optional BOOTSTRAP_ADMIN_NAME) once, start the app, log in, then delete
    the password from .env and redeploy. Deleting the line is not enough while
    the current container is still running — Compose already interpolated it
    into the process environment (`docker inspect` still shows it). Empty/unset
    env is a no-op so local dev is unchanged. Misconfiguration logs and skips
    — never crashes startup.
    """
    email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""
    name = (os.getenv("BOOTSTRAP_ADMIN_NAME") or "").strip()

    email_set = bool(email)
    password_set = bool(password)
    if not email_set and not password_set:
        return

    try:
        existing = count_users()
    except Exception:
        logger.exception("bootstrap admin: could not read users table; skipping")
        return

    if existing:
        if password_set:
            logger.warning(
                "BOOTSTRAP_ADMIN_PASSWORD is set but the users table is not empty. "
                "Not creating another user and not overwriting. Delete "
                "BOOTSTRAP_ADMIN_PASSWORD from .env and redeploy so the secret "
                "leaves the running container (docker inspect still shows it until then)."
            )
        return

    if not email_set or not password_set:
        logger.error(
            "bootstrap admin: BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD "
            "must both be set to create the first admin; skipping"
        )
        return

    if not is_valid_email(email):
        logger.error(
            "bootstrap admin: BOOTSTRAP_ADMIN_EMAIL is not a valid email; skipping"
        )
        return

    if len(password) < 8:
        logger.error(
            "bootstrap admin: BOOTSTRAP_ADMIN_PASSWORD must be at least 8 "
            "characters; skipping"
        )
        return

    try:
        if get_user_by_email(email.strip().lower()):
            logger.warning(
                "bootstrap admin: a user with that email already exists; skipping"
            )
            return
        user_id = create_user(email, name, password)
    except Exception:
        logger.exception("bootstrap admin: failed to create the first admin; skipping")
        return

    logger.info(
        "bootstrap admin: created user #%s (%s). After you can log in, delete "
        "BOOTSTRAP_ADMIN_PASSWORD from .env and redeploy so the secret leaves "
        "the running container.",
        user_id,
        email.strip().lower(),
    )


def authenticate_user(email: str, password: str):
    user = get_user_by_email(email.strip().lower())
    if not user:
        return None
    if verify_password(password, user["password_hash"]):
        return user
    return None


# ==================== CSRF ====================
def generate_csrf_token() -> str:
    return csrf_serializer.dumps({"t": datetime.utcnow().isoformat()})


def validate_csrf_token(token: str) -> bool:
    try:
        csrf_serializer.loads(token, max_age=3600 * 8)
        return True
    except Exception:
        return False


# ==================== Rate limiting (sqlite, shared across workers) ====================
RATE_LIMIT_WINDOW = 60
# Per-action caps. "leads_api" 30/min is the lead-ingest ceiling. The check
# runs before _check_api_key, so that number is also the unauthenticated
# key-guess cap per IP. The reverse proxy's allowlist is the real front door; do not
# treat a later bump as free for abuse.
RATE_LIMIT_MAX_BY_ACTION = {
    "login": 5,
    "leads_api": 30,
    "stages_api": 30,
    # Bots are chatty (initialize + tools/list + several calls per turn).
    # 120/min is the unauthenticated key-guess cap per IP as well.
    "mcp_api": 120,
    "oauth_register": 10,
    "oauth_token": 20,
    "oauth_authorize": 10,
    "default": 5,
}


def check_rate_limit_retry(key: str, action: str = "default"):
    """Return (allowed, retry_after_seconds). retry_after is 0 when allowed.

    Sliding-window remaining time is how long until the oldest hit ages out,
    at least 1s. Hits older than the window are deleted so rotating IPs cannot
    grow the table forever. Stored in sqlite so two workers share one bucket.
    """
    full_key = f"{action}:{key}"
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    limit = RATE_LIMIT_MAX_BY_ACTION.get(action, RATE_LIMIT_MAX_BY_ACTION["default"])
    with get_db() as conn:
        conn.execute(
            "DELETE FROM rate_limit_hits WHERE bucket = ? AND hit_at < ?",
            (full_key, cutoff),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS c, MIN(hit_at) AS oldest "
            "FROM rate_limit_hits WHERE bucket = ?",
            (full_key,),
        ).fetchone()
        count = row["c"]
        oldest = row["oldest"]
        if count >= limit:
            remaining = RATE_LIMIT_WINDOW
            if oldest is not None:
                remaining = max(1, math.ceil(RATE_LIMIT_WINDOW - (now - oldest)))
            conn.commit()
            return False, remaining
        conn.execute(
            "INSERT INTO rate_limit_hits (bucket, hit_at) VALUES (?, ?)",
            (full_key, now),
        )
        conn.commit()
    return True, 0


def check_rate_limit(key: str, action: str = "default") -> bool:
    allowed, _retry_after = check_rate_limit_retry(key, action)
    return allowed


def clear_rate_limits() -> None:
    """Drop every stored hit. Tests call this between cases."""
    with get_db() as conn:
        conn.execute("DELETE FROM rate_limit_hits")
        conn.commit()


def get_rate_limit_key(ip: str, email: str = "") -> str:
    return f"{ip}:{email}" if email else ip


def _digest_api_key(value: str) -> bytes:
    return hashlib.sha256((value or "").encode("utf-8")).digest()


def check_env_api_key(provided: str, env_var: str) -> bool:
    """Constant-time compare of a presented secret against an env var.

    Fails closed if the env var is unset/empty. Hash both sides so
    compare_digest always sees equal-length 32-byte digests (wrong-length
    or non-ASCII headers must be 401, not 500).
    """
    expected = os.getenv(env_var, "")
    if not expected:
        return False
    return secrets_module.compare_digest(
        _digest_api_key(provided),
        _digest_api_key(expected),
    )


# ==================== Validation / sanitization ====================
def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False
    if any(c.isspace() or ord(c) < 32 for c in email):
        return False
    return True


def is_valid_slug(slug: str) -> bool:
    if not slug or len(slug) < 2 or len(slug) > 50:
        return False
    return bool(re.match(r'^[a-z0-9-]+$', slug))


def clean_owner_key(value):
    """alice / bob / sales-agent. Empty or garbage → None, never raises."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")[:40]
    return text or None


def sanitize_text(value: str, max_len: int = 2000, allow_newlines: bool = False) -> str:
    if not value:
        return ""
    cleaned = "".join(ch for ch in value if ch >= " " or ch in "\n\t")
    if not allow_newlines:
        cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    return cleaned.strip()[:max_len]


def should_use_secure_cookies() -> bool:
    env_flag = os.getenv("SECURE_COOKIES")
    if env_flag is not None:
        return env_flag.lower() in ("1", "true", "yes")
    base = os.getenv("BASE_URL", "http://localhost:8000")
    return base.startswith("https://")
