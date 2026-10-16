"""Fixtures for ADR 0001 lead-model migrations (schema 7 → 8).

The migrated lost-stage deal helper is here so P2 restore_deal can reuse it.
"""
import sqlite3

from app.database import (
    migrate_001,
    migrate_002,
    migrate_003,
    migrate_004,
    migrate_005,
    migrate_006,
    migrate_007,
)


def apply_through_v7(database):
    """Bring the current DB_PATH to schema 7 without running migrate_008."""
    with database.get_db() as db:
        migrate_001(db)
        migrate_002(db)
        migrate_003(db)
        migrate_004(db)
        migrate_005(db)
        migrate_006(db)
        migrate_007(db)
        db.execute("PRAGMA user_version = 7")
        db.commit()
    database._seed_runtime_data()


def populate_schema_7_sample(database, *, dangling_offer=False, null_closed_lost=True):
    """Insert deals, activities, tasks, tags and a lost-stage deal on schema 7.

    Returns a dict of ids and the expected closed_at for the null-closed lost deal.
    """
    from app.services.activities import log_activity
    from app.services.catalog import create_service
    from app.services.deal_tags import apply_deal_tags
    from app.services.deals import create_deal, set_deal_stage
    from app.services.delegated_tasks import create_task
    from app.services.partners import create_partner

    partner_id = create_partner("Acme Co", email="ops@acme.example", phone="0400111222")
    service_id = create_service("Consulting", "consulting-p1")
    open_id = create_deal(
        partner_id, service_id, source="referral", value_estimate=1800,
        pain_points="Manual intake", goals="Book online",
    )
    lost_id = create_deal(partner_id, service_id, source="cold")
    set_deal_stage(lost_id, "lost")
    log_activity(partner_id, "note", "Kickoff notes", deal_id=open_id)
    task, error, _created = create_task(open_id, "Send pack", owner="agent")
    assert error is None
    apply_deal_tags(open_id, replace=["campaign:p1-fixture"])

    null_closed_id = None
    null_closed_updated_at = "2026-01-15T12:00:00"
    dangling_id = None
    with database.get_db() as db:
        if null_closed_lost:
            db.execute(
                """INSERT INTO deals
                       (partner_id, service_id, stage, source, updated_at, closed_at)
                   VALUES (?, ?, 'lost', 'manual-lost', ?, NULL)""",
                (partner_id, service_id, null_closed_updated_at),
            )
            null_closed_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        if dangling_offer:
            db.execute(
                """INSERT INTO deals (partner_id, service_id, stage, offer_id)
                   VALUES (?, ?, 'new', 9999)""",
                (partner_id, service_id),
            )
            dangling_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()

    return {
        "partner_id": partner_id,
        "service_id": service_id,
        "open_id": open_id,
        "lost_id": lost_id,
        "null_closed_id": null_closed_id,
        "null_closed_updated_at": null_closed_updated_at,
        "dangling_id": dangling_id,
        "task_id": task["id"],
    }


def build_v0_reordered_deals(path):
    """Version-0 file whose deals columns are not in _SCHEMA_V1 order."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL
        );
        CREATE TABLE deals (
            closed_at TEXT,
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_key TEXT,
            partner_id INTEGER NOT NULL,
            offer_id INTEGER,
            service_id INTEGER NOT NULL,
            stage TEXT NOT NULL DEFAULT 'new',
            source TEXT,
            value_estimate REAL,
            pain_points TEXT,
            goals TEXT,
            next_action TEXT,
            next_action_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            external_ref TEXT,
            parent_deal_id INTEGER
        );
        """
    )
    conn.execute("INSERT INTO partners (id, name) VALUES (1, 'Order Co')")
    conn.execute(
        "INSERT INTO services (id, name, slug) VALUES (1, 'Consulting', 'consulting-order')"
    )
    conn.execute(
        """INSERT INTO deals (
               closed_at, id, owner_key, partner_id, offer_id, service_id, stage,
               source, value_estimate, pain_points, goals, next_action,
               next_action_date, created_at, updated_at, external_ref, parent_deal_id
           ) VALUES (
               '2020-01-01T00:00:00', 1, 'owner-a', 1, NULL, 1, 'contacted',
               'SRC_UNIQUE', 12345.67, 'PAIN_UNIQUE', 'GOAL_UNIQUE', 'Call',
               '2026-02-01', '2019-01-01T00:00:00', '2019-06-01T00:00:00',
               'REF_UNIQUE', NULL
           )"""
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


def sqlite_master_snapshot(path):
    """Comparable sqlite_master rows (no sqlite_* internals)."""
    conn = sqlite3.connect(path)
    rows = conn.execute(
        """SELECT type, name, tbl_name, sql FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    ).fetchall()
    conn.close()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def insert_lead(database, *, company_name="Harbour Physio", email=None, active=1):
    """Insert a lead row with null partner/service (no create_lead yet)."""
    with database.get_db() as db:
        db.execute(
            """INSERT INTO deals
                   (partner_id, service_id, stage, type, company_name, email,
                    active, priority)
               VALUES (NULL, NULL, 'new', 'lead', ?, ?, ?, 0)""",
            (company_name, email, active),
        )
        db.commit()
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
