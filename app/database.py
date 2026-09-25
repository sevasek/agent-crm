import sqlite3
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
import logging

IntegrityConflict = sqlite3.IntegrityError

# Bump SCHEMA_VERSION only when adding a numbered function to MIGRATIONS.
# Existing DBs with no user_version (PRAGMA returns 0) are treated as
# version 0 and migrated, not rejected.
#
# After a live database is at version N, editing _SCHEMA_V1 or
# _apply_additive_columns will NOT update it. For deployed DBs add
# migrate_00N and bump this constant. Never add columns to an already
# shipped version in place.
SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    """The on-disk schema is newer than this code; refuse to start."""

# sqlite3.connect default. Request handlers stay here so a locked write fails
# in ~5s instead of holding a worker thread. Seed CLI uses db_timeout(30).
_DEFAULT_BUSY_TIMEOUT = 5.0
_db_timeout = ContextVar("db_timeout", default=_DEFAULT_BUSY_TIMEOUT)


def is_lock_error(exc: BaseException) -> bool:
    """True for SQLite busy/locked; other DB-API drivers can extend this."""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    return False


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/crm.db")
DB_PATH = DATABASE_URL.replace("sqlite:///", "")

logger = logging.getLogger(__name__)


def get_db_path():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    return DB_PATH


@contextmanager
def db_timeout(seconds):
    """Use a longer busy timeout for nested get_db() calls (seed CLI)."""
    token = _db_timeout.set(seconds)
    try:
        yield
    finally:
        _db_timeout.reset(token)


@contextmanager
def get_db(timeout=None):
    seconds = timeout if timeout is not None else _db_timeout.get()
    conn = sqlite3.connect(get_db_path(), timeout=seconds)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass  # non-fatal; continue with default journaling
    try:
        yield conn
    finally:
        conn.close()


def get_user_version(conn) -> int:
    """PRAGMA user_version; 0 means a legacy DB that has never been versioned."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _schema_too_new_message(current: int) -> str:
    return (
        f"Database schema version {current} is newer than this code "
        f"(supports up to {SCHEMA_VERSION}). Refusing to start so an older "
        f"container cannot mutate a newer schema. Deploy a matching image, "
        f"or restore the backup taken automatically before the upgrade."
    )


@contextmanager
def _migrate_lock(db_path: str):
    """Exclusive file lock so app and staleness-cron do not race on migrate.

    Compose is not used for start-order (a sibling change owns those files).
    BEGIN IMMEDIATE on the sqlite file is a second layer inside init_db().
    """
    import fcntl

    lock_path = f"{os.path.abspath(db_path)}.migrate.lock"
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _has_user_tables(conn) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row[0] > 0


def _backup_before_migrate(from_ver: int, to_ver: int) -> str:
    from app.backup import default_backup_dir, online_backup

    src = get_db_path()
    dest_dir = default_backup_dir(src)
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    dest = os.path.join(dest_dir, f"pre-migrate-v{from_ver}-to-v{to_ver}-{stamp}.db")
    logger.info(
        "Backing up %s to %s before migrating v%s -> v%s",
        src,
        dest,
        from_ver,
        to_ver,
    )
    return online_backup(src, dest)


def _table_columns(db, table: str) -> set:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(db, table: str, name: str, ddl: str) -> bool:
    if name in _table_columns(db, table):
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def _split_sql(script: str):
    """Split DDL on ';' without the implicit COMMIT executescript() issues."""
    buf = []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0]
        buf.append(stripped)
    text = "\n".join(buf)
    for part in text.split(";"):
        stmt = part.strip()
        if stmt:
            yield stmt


def _run_sql(db, script: str) -> None:
    for stmt in _split_sql(script):
        db.execute(stmt)


# Additive-only. Do not DROP or RENAME columns in this series.
_SCHEMA_V1 = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            is_company INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER REFERENCES partners(id),
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            website TEXT,
            title TEXT,
            address TEXT,
            social_url TEXT,
            preferred_channel TEXT,
            industry TEXT,
            team_size INTEGER,
            linkedin_url TEXT,
            x_url TEXT,
            instagram_url TEXT,
            facebook_url TEXT,
            youtube_url TEXT,
            owner_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            nurture_list_slug TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            service_id INTEGER NOT NULL REFERENCES services(id),
            stage TEXT NOT NULL DEFAULT 'new',
            source TEXT,
            value_estimate REAL,
            pain_points TEXT,
            goals TEXT,
            next_action TEXT,
            next_action_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            offer_id INTEGER REFERENCES offers(id),
            owner_key TEXT,
            external_ref TEXT,
            parent_deal_id INTEGER REFERENCES deals(id)
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            label TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            deal_id INTEGER REFERENCES deals(id),
            type TEXT NOT NULL,
            body TEXT,
            occurred_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- The pipeline's stages, in Kanban column order. `deals.stage` stores
        -- the `key` as free text (no FK — deleting a stage in use is refused
        -- at the service layer instead, so existing deals never dangle).
        -- Behavior that used to switch on a hardcoded stage name (call-queue
        -- eligibility, nurture enrollment, won/lost/closed_at) now switches on
        -- these role flags, so the workflow itself is configurable — including
        -- via the /api/v1/stages API for scripts/agents.
        CREATE TABLE IF NOT EXISTS pipeline_stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0,
            is_qualified_pool INTEGER NOT NULL DEFAULT 0,
            triggers_nurture INTEGER NOT NULL DEFAULT 0,
            is_won INTEGER NOT NULL DEFAULT 0,
            is_lost INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- What you're pitching on a call: a one-line value prop, a proof
        -- point, and a price anchor. A deal references one via
        -- `deals.offer_id` (added below) so the call view can show the
        -- pitch to deliver, not just the lead's own pain points/goals.
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            pitch TEXT,
            proof_point TEXT,
            price_anchor TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            service_id INTEGER REFERENCES services(id),
            price REAL,
            currency TEXT,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Configurable ICP fit rules (see app.services.icp) — a deterministic,
        -- transparent weighted-rules score over partner/deal fields, same
        -- "formula, not a model" spirit as the call-priority score in
        -- app.services.call_queue. Order doesn't affect the score (it's a
        -- sum over every matching criterion), so there's no position column.
        CREATE TABLE IF NOT EXISTS icp_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            field TEXT NOT NULL,
            operator TEXT NOT NULL,
            value TEXT,
            weight INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_partners_parent ON partners(parent_id);
        CREATE INDEX IF NOT EXISTS idx_deals_partner ON deals(partner_id);
        CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
        CREATE INDEX IF NOT EXISTS idx_activities_partner ON activities(partner_id);
        CREATE INDEX IF NOT EXISTS idx_activities_deal ON activities(deal_id);
        CREATE INDEX IF NOT EXISTS idx_pipeline_stages_position ON pipeline_stages(position);
        CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

        -- Sliding-window rate-limit hits, shared across workers via the same
        -- sqlite file. In-process dicts would reset per uvicorn worker.
        CREATE TABLE IF NOT EXISTS rate_limit_hits (
            bucket TEXT NOT NULL,
            hit_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_bucket_hit
            ON rate_limit_hits (bucket, hit_at);

        -- MCP connector OAuth clients (RFC 7591). Codes and tokens
        -- are signed, not stored; client_id + redirect_uris must survive a
        -- restart or a registered client would 401 on refresh.
        CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_name TEXT,
            redirect_uris TEXT NOT NULL,
            token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Deal-scoped work handed from a sales agent to another agent or a human.
        -- Not a project manager: title + brief + owner + status, tied to a deal.
        CREATE TABLE IF NOT EXISTS delegated_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL REFERENCES deals(id),
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            title TEXT NOT NULL,
            brief TEXT,
            owner TEXT NOT NULL DEFAULT 'agent',
            status TEXT NOT NULL DEFAULT 'delegated',
            due_date TEXT,
            created_by TEXT,
            result_notes TEXT,
            webhook_notified_at TEXT,
            webhook_last_attempt_at TEXT,
            webhook_last_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_delegated_tasks_deal ON delegated_tasks(deal_id);
        CREATE INDEX IF NOT EXISTS idx_delegated_tasks_owner_status ON delegated_tasks(owner, status);
"""


def _apply_additive_columns(db) -> None:
    """ADD COLUMN for anything an older table might be missing.

    CREATE TABLE IF NOT EXISTS does not add columns to an existing table, so
    indexes that follow (parent_id, owner_key, …) would fail on a v0 file
    that predates those columns. Additive only — no DROP / RENAME.
    """
    for col, ddl in (
        ("is_company", "is_company INTEGER NOT NULL DEFAULT 0"),
        ("parent_id", "parent_id INTEGER REFERENCES partners(id)"),
        ("email", "email TEXT"),
        ("phone", "phone TEXT"),
        ("website", "website TEXT"),
        ("title", "title TEXT"),
        ("address", "address TEXT"),
        ("social_url", "social_url TEXT"),
        ("preferred_channel", "preferred_channel TEXT"),
        ("industry", "industry TEXT"),
        ("team_size", "team_size INTEGER"),
        ("linkedin_url", "linkedin_url TEXT"),
        ("x_url", "x_url TEXT"),
        ("instagram_url", "instagram_url TEXT"),
        ("facebook_url", "facebook_url TEXT"),
        ("youtube_url", "youtube_url TEXT"),
        ("owner_key", "owner_key TEXT"),
        # CURRENT_TIMESTAMP is not a constant default; SQLite rejects it on ALTER.
        ("created_at", "created_at TEXT"),
        ("updated_at", "updated_at TEXT"),
    ):
        _add_column_if_missing(db, "partners", col, ddl)

    for col, ddl in (
        ("offer_id", "offer_id INTEGER REFERENCES offers(id)"),
        ("owner_key", "owner_key TEXT"),
        ("external_ref", "external_ref TEXT"),
        ("parent_deal_id", "parent_deal_id INTEGER REFERENCES deals(id)"),
    ):
        _add_column_if_missing(db, "deals", col, ddl)

    for col, ddl in (
        ("service_id", "service_id INTEGER REFERENCES services(id)"),
        ("price", "price REAL"),
        ("currency", "currency TEXT"),
        ("description", "description TEXT"),
    ):
        _add_column_if_missing(db, "offers", col, ddl)

    if _table_columns(db, "delegated_tasks"):
        _add_column_if_missing(
            db, "delegated_tasks", "webhook_last_attempt_at", "webhook_last_attempt_at TEXT"
        )
        _add_column_if_missing(
            db, "delegated_tasks", "webhook_last_error", "webhook_last_error TEXT"
        )


def migrate_001(db) -> None:
    """Bring a version-0 (unversioned) database up to the current schema.

    CREATE TABLE IF NOT EXISTS is a no-op on tables that already exist;
    guarded ALTER TABLE ADD COLUMN covers DBs that shipped before a column.
    Indexes run after ALTERs so a stripped legacy table cannot break startup.
    Additive only — no DROP / RENAME.
    """
    table_stmts = []
    index_stmts = []
    for stmt in _split_sql(_SCHEMA_V1):
        if stmt.upper().startswith("CREATE INDEX"):
            index_stmts.append(stmt)
        else:
            table_stmts.append(stmt)
    for stmt in table_stmts:
        db.execute(stmt)

    _apply_additive_columns(db)
    _backfill_partner_social_urls(db)

    for stmt in index_stmts:
        db.execute(stmt)
    db.execute("CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner_key)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_deals_parent ON deals(parent_deal_id)")


# version number -> migration applied when moving *to* that version
MIGRATIONS = {
    1: migrate_001,
}


def _seed_runtime_data() -> None:
    from app.services.offers import seed_starter_offers

    seed_starter_offers()

    # Seed the default pipeline once. Existing behavior (new -> contacted ->
    # qualified -> nurture -> proposal -> won/lost) becomes the starting
    # configuration rather than a hardcoded constant; every field below is
    # editable afterwards through /admin/stages or /api/v1/stages.
    with get_db() as db:
        seeded = db.execute("SELECT COUNT(*) AS n FROM pipeline_stages").fetchone()["n"]
        if not seeded:
            db.executemany(
                """
                INSERT INTO pipeline_stages
                    (key, label, position, is_default, is_qualified_pool, triggers_nurture, is_won, is_lost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("new", "New", 0, 1, 0, 0, 0, 0),
                    ("contacted", "Contacted", 1, 0, 0, 0, 0, 0),
                    ("qualified", "Qualified", 2, 0, 1, 0, 0, 0),
                    ("nurture", "Nurture", 3, 0, 0, 1, 0, 0),
                    ("proposal", "Proposal", 4, 0, 0, 0, 0, 0),
                    ("won", "Won", 5, 0, 0, 0, 1, 0),
                    ("lost", "Lost", 6, 0, 0, 0, 0, 1),
                ],
            )
            db.commit()


def init_db():
    """Create or migrate the schema. Safe for app and staleness-cron together.

    * PRAGMA user_version is the source of truth (legacy files are version 0).
    * A DB newer than SCHEMA_VERSION refuses to start.
    * Numbered migrations run inside BEGIN IMMEDIATE.
    * An online backup is taken before migrating a database that already
      has application tables. Fresh empty files skip the backup.
    * A file lock plus the sqlite write lock serializes concurrent callers.
    """
    db_path = get_db_path()
    with _migrate_lock(db_path):
        with get_db() as db:
            current = get_user_version(db)
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(_schema_too_new_message(current))
            needs_backup = current < SCHEMA_VERSION and _has_user_tables(db)
        if needs_backup:
            _backup_before_migrate(current, SCHEMA_VERSION)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = get_user_version(db)
                if current > SCHEMA_VERSION:
                    db.execute("ROLLBACK")
                    raise SchemaVersionError(_schema_too_new_message(current))
                if current < SCHEMA_VERSION:
                    for ver in range(current + 1, SCHEMA_VERSION + 1):
                        migrate = MIGRATIONS.get(ver)
                        if migrate is None:
                            raise SchemaVersionError(
                                f"No migration defined for schema version {ver}"
                            )
                        logger.info("Applying schema migration %s", ver)
                        migrate(db)
                    db.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
                db.commit()
            except SchemaVersionError:
                raise
            except Exception:
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                raise
        _seed_runtime_data()


def _backfill_partner_social_urls(db):
    """Copy leftover partners.social_url into the matching platform column.

    Imported here (not at module load) so database.py does not import
    partners at import time — partners already imports get_db from here.
    """
    from app.services.partners import SOCIAL_FIELDS, apply_social_fields

    rows = db.execute(
        "SELECT id, social_url, linkedin_url, x_url, instagram_url, facebook_url, youtube_url FROM partners"
    ).fetchall()
    changed = False
    for row in rows:
        partner = dict(row)
        if not partner.get("social_url"):
            continue
        if any(partner.get(field) for field in SOCIAL_FIELDS):
            continue
        applied = apply_social_fields(partner, {"social_url": partner["social_url"]})
        db.execute(
            """UPDATE partners
               SET social_url = ?, linkedin_url = ?, x_url = ?, instagram_url = ?,
                   facebook_url = ?, youtube_url = ?
               WHERE id = ?""",
            (
                applied["social_url"],
                applied["linkedin_url"],
                applied["x_url"],
                applied["instagram_url"],
                applied["facebook_url"],
                applied["youtube_url"],
                partner["id"],
            ),
        )
        changed = True
    return changed
