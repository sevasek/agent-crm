from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
from collections import defaultdict, deque
import hashlib
import logging
import math
import os
import re
import secrets as secrets_module
import threading
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


def _read_bootstrap_admin_password() -> tuple[str, str]:
    """Return (password, source) for the first-admin bootstrap.

    Prefer BOOTSTRAP_ADMIN_PASSWORD_FILE (Docker/Podman secret) so the
    password is not copied into the process environment. Trailing newlines
    from the file are stripped (secret files usually end with one). If the
    file is set, readable and non-empty it wins over BOOTSTRAP_ADMIN_PASSWORD.
    An unreadable file is a misconfiguration: we do not fall back to the
    env password. An empty/unset file falls back to the env var.
    """
    path = (os.getenv("BOOTSTRAP_ADMIN_PASSWORD_FILE") or "").strip()
    env_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""
    if not path:
        return env_password, "env" if env_password else ""

    try:
        with open(path, encoding="utf-8") as fh:
            file_password = fh.read().rstrip("\r\n")
    except OSError as exc:
        logger.error(
            "bootstrap admin: could not read BOOTSTRAP_ADMIN_PASSWORD_FILE "
            "(%s): %s",
            path,
            exc,
        )
        return "", "file"

    if file_password:
        return file_password, "file"
    logger.error(
        "bootstrap admin: BOOTSTRAP_ADMIN_PASSWORD_FILE (%s) is empty",
        path,
    )
    return env_password, "env" if env_password else "file"


def maybe_bootstrap_admin() -> None:
    """Create the first admin from env when the users table is empty.

    Production sudoers cannot `compose exec`, and create_admin.py is
    interactive (getpass). Set BOOTSTRAP_ADMIN_EMAIL and a password via
    BOOTSTRAP_ADMIN_PASSWORD_FILE (preferred; Docker/Podman secret) or
    BOOTSTRAP_ADMIN_PASSWORD (optional BOOTSTRAP_ADMIN_NAME) once, start
    the app, and log in. The FILE form never lands in `docker inspect`.
    If the env password is used, delete it from .env and redeploy —
    Compose already interpolated it into the process environment.
    Empty/unset env is a no-op so local dev is unchanged. Misconfiguration
    logs and skips — never crashes startup.
    """
    email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip()
    password, password_source = _read_bootstrap_admin_password()
    name = (os.getenv("BOOTSTRAP_ADMIN_NAME") or "").strip()

    email_set = bool(email)
    password_set = bool(password)
    file_configured = bool((os.getenv("BOOTSTRAP_ADMIN_PASSWORD_FILE") or "").strip())
    if not email_set and not password_set and not file_configured:
        return

    try:
        existing = count_users()
    except Exception:
        logger.exception("bootstrap admin: could not read users table; skipping")
        return

    if existing:
        if os.getenv("BOOTSTRAP_ADMIN_PASSWORD"):
            logger.warning(
                "BOOTSTRAP_ADMIN_PASSWORD is set but the users table is not empty. "
                "Not creating another user and not overwriting. Delete "
                "BOOTSTRAP_ADMIN_PASSWORD from .env and redeploy so the secret "
                "leaves the running container (docker inspect still shows it until then)."
            )
        elif file_configured:
            logger.warning(
                "BOOTSTRAP_ADMIN_PASSWORD_FILE is set but the users table is not empty. "
                "Not creating another user and not overwriting. Unmount the secret "
                "when you no longer need it."
            )
        return

    if not email_set or not password_set:
        logger.error(
            "bootstrap admin: BOOTSTRAP_ADMIN_EMAIL and a password "
            "(BOOTSTRAP_ADMIN_PASSWORD_FILE or BOOTSTRAP_ADMIN_PASSWORD) "
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

    if password_source == "file":
        logger.info(
            "bootstrap admin: created user #%s (%s) from "
            "BOOTSTRAP_ADMIN_PASSWORD_FILE. After you can log in, unmount the "
            "secret so it is no longer readable by the process.",
            user_id,
            email.strip().lower(),
        )
    else:
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


# ==================== Rate limiting (in-memory; one uvicorn worker) ====================
RATE_LIMIT_WINDOW = 60
# Unauthenticated / failed-auth caps, keyed on client IP (plus email for login).
# Successful MCP/API requests use RATE_LIMIT_MAX_AUTH_BY_ACTION instead, keyed
# on the key/token identity, so a flood of bad keys cannot lock out the agent.
RATE_LIMIT_MAX_BY_ACTION = {
    "login": 5,
    "leads_api": 30,
    "stages_api": 30,
    # Bots are chatty (initialize + tools/list + several calls per turn).
    # 120/min is the unauthenticated key-guess cap per IP.
    "mcp_api": 120,
    "oauth_register": 10,
    "oauth_token": 20,
    "oauth_authorize": 10,
    "default": 5,
}
# Authenticated ceilings (per key/token id, not IP). Larger than the guess cap
# so a shared proxy IP does not throttle a busy agent.
RATE_LIMIT_MAX_AUTH_BY_ACTION = {
    "leads_api": 120,
    "stages_api": 120,
    "mcp_api": 600,
}

_rate_limit_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_lock = threading.Lock()


def _rate_limit_cap(action: str, authenticated: bool) -> int:
    if authenticated:
        if action in RATE_LIMIT_MAX_AUTH_BY_ACTION:
            return RATE_LIMIT_MAX_AUTH_BY_ACTION[action]
        unauth = RATE_LIMIT_MAX_BY_ACTION.get(action, RATE_LIMIT_MAX_BY_ACTION["default"])
        return max(unauth * 4, unauth)
    return RATE_LIMIT_MAX_BY_ACTION.get(action, RATE_LIMIT_MAX_BY_ACTION["default"])


def _rate_limit_bucket(key: str, action: str, authenticated: bool) -> str:
    if authenticated:
        return f"{action}:auth:{key}"
    return f"{action}:{key}"


def _prune_rate_limit_bucket(bucket: str, now: float) -> deque:
    cutoff = now - RATE_LIMIT_WINDOW
    hits = _rate_limit_hits[bucket]
    while hits and hits[0] < cutoff:
        hits.popleft()
    return hits


def check_rate_limit_retry(key: str, action: str = "default", *, authenticated: bool = False):
    """Return (allowed, retry_after_seconds). retry_after is 0 when allowed.

    Sliding-window remaining time is how long until the oldest hit ages out,
    at least 1s. Hits older than the window are dropped. In-memory: this app
    runs a single uvicorn worker, so a sqlite write per request is wasted
    (and a full/read-only disk would fail reads that only needed a limit check).

    Callers should count failed auth toward the unauthenticated IP bucket, and
    successful MCP/API requests toward the larger authenticated bucket
    (`authenticated=True`, key = key/token id).
    """
    bucket = _rate_limit_bucket(key, action, authenticated)
    now = time.time()
    limit = _rate_limit_cap(action, authenticated)
    with _rate_limit_lock:
        hits = _prune_rate_limit_bucket(bucket, now)
        if len(hits) >= limit:
            oldest = hits[0]
            remaining = max(1, math.ceil(RATE_LIMIT_WINDOW - (now - oldest)))
            return False, remaining
        hits.append(now)
    return True, 0


def check_rate_limit(key: str, action: str = "default", *, authenticated: bool = False) -> bool:
    allowed, _retry_after = check_rate_limit_retry(key, action, authenticated=authenticated)
    return allowed


def clear_rate_limits() -> None:
    """Drop every stored hit. Tests call this between cases."""
    with _rate_limit_lock:
        _rate_limit_hits.clear()


def get_rate_limit_key(ip: str, email: str = "") -> str:
    return f"{ip}:{email}" if email else ip


def env_key_rate_limit_identity(env_var: str) -> str:
    """Stable bucket id for a shared env API key (does not include the secret)."""
    return f"env:{env_var}"


def user_key_rate_limit_identity(key_id: int) -> str:
    return f"userkey:{key_id}"


def oauth_rate_limit_identity(client_id: str) -> str:
    return f"oauth:{client_id or 'token'}"


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
