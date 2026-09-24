import sqlite3
import os
from contextlib import contextmanager
from contextvars import ContextVar
import logging

IntegrityConflict = sqlite3.IntegrityError

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


def row_to_dict(row):
    return dict(row) if row else None


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


def init_db():
    with get_db() as db:
        db.executescript("""
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
        """)
        db.commit()

        # deals.offer_id was added after the initial deals table shipped —
        # SQLite has no "ADD COLUMN IF NOT EXISTS", so guard with PRAGMA
        # table_info the same way any later ALTER on this table should.
        existing_deal_cols = {row["name"] for row in db.execute("PRAGMA table_info(deals)").fetchall()}
        if "offer_id" not in existing_deal_cols:
            db.execute("ALTER TABLE deals ADD COLUMN offer_id INTEGER REFERENCES offers(id)")
            db.commit()

        # Per-platform social URLs were added after partners.social_url shipped.
        existing_partner_cols = {row["name"] for row in db.execute("PRAGMA table_info(partners)").fetchall()}
        added_social = False
        for col in ("linkedin_url", "x_url", "instagram_url", "facebook_url", "youtube_url"):
            if col not in existing_partner_cols:
                db.execute(f"ALTER TABLE partners ADD COLUMN {col} TEXT")
                added_social = True
        if added_social:
            db.commit()
        _backfill_partner_social_urls(db)

        existing_offer_cols = {row["name"] for row in db.execute("PRAGMA table_info(offers)").fetchall()}
        for col, ddl in (
            ("service_id", "service_id INTEGER REFERENCES services(id)"),
            ("price", "price REAL"),
            ("currency", "currency TEXT"),
            ("description", "description TEXT"),
        ):
            if col not in existing_offer_cols:
                db.execute(f"ALTER TABLE offers ADD COLUMN {ddl}")
        db.commit()

        existing_deal_cols = {row["name"] for row in db.execute("PRAGMA table_info(deals)").fetchall()}
        if "owner_key" not in existing_deal_cols:
            db.execute("ALTER TABLE deals ADD COLUMN owner_key TEXT")
        if "external_ref" not in existing_deal_cols:
            db.execute("ALTER TABLE deals ADD COLUMN external_ref TEXT")
        if "parent_deal_id" not in existing_deal_cols:
            db.execute("ALTER TABLE deals ADD COLUMN parent_deal_id INTEGER REFERENCES deals(id)")
        db.commit()
        db.execute("CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_deals_parent ON deals(parent_deal_id)")
        db.commit()

        existing_partner_cols = {row["name"] for row in db.execute("PRAGMA table_info(partners)").fetchall()}
        if "owner_key" not in existing_partner_cols:
            db.execute("ALTER TABLE partners ADD COLUMN owner_key TEXT")
            db.commit()

        existing_task_cols = {
            row["name"] for row in db.execute("PRAGMA table_info(delegated_tasks)").fetchall()
        }
        if existing_task_cols:
            if "webhook_last_attempt_at" not in existing_task_cols:
                db.execute("ALTER TABLE delegated_tasks ADD COLUMN webhook_last_attempt_at TEXT")
            if "webhook_last_error" not in existing_task_cols:
                db.execute("ALTER TABLE delegated_tasks ADD COLUMN webhook_last_error TEXT")
            db.commit()

        from app.services.offers import seed_starter_offers
        seed_starter_offers()

        # Seed the default pipeline once. Existing behavior (new -> contacted ->
        # qualified -> nurture -> proposal -> won/lost) becomes the starting
        # configuration rather than a hardcoded constant; every field below is
        # editable afterwards through /admin/stages or /api/v1/stages.
        seeded = db.execute("SELECT COUNT(*) AS n FROM pipeline_stages").fetchone()["n"]
        if not seeded:
            db.executemany("""
                INSERT INTO pipeline_stages
                    (key, label, position, is_default, is_qualified_pool, triggers_nurture, is_won, is_lost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                ("new", "New", 0, 1, 0, 0, 0, 0),
                ("contacted", "Contacted", 1, 0, 0, 0, 0, 0),
                ("qualified", "Qualified", 2, 0, 1, 0, 0, 0),
                ("nurture", "Nurture", 3, 0, 0, 1, 0, 0),
                ("proposal", "Proposal", 4, 0, 0, 0, 0, 0),
                ("won", "Won", 5, 0, 0, 0, 1, 0),
                ("lost", "Lost", 6, 0, 0, 0, 0, 1),
            ])
            db.commit()


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
    if changed:
        db.commit()
