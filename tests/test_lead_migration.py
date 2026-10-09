"""Schema 7 → 8 rebuild of deals, activities and delegated_tasks (ADR §6.8)."""
import logging
import sqlite3

import pytest

from app import database
from app.database import SCHEMA_VERSION, get_db, get_user_version, init_db
from app.mcp.tools import create_delegated_task_tool, get_deal_tool
from app.services.activities import log_activity
from app.services.catalog import create_service
from app.services.deal_tags import apply_deal_tags
from app.services.deals import create_deal, get_deal, list_deals, set_deal_stage
from app.services.delegated_tasks import create_task
from app.services.partners import create_partner


def _objects(path):
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    return sorted(rows)


def _at_schema_7(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 7)
    init_db()
    return path


def _finish_migration(monkeypatch):
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION)
    init_db()


def test_schema7_fixture_keeps_ids_and_marks_lost_stage(tmp_path, monkeypatch):
    path = _at_schema_7(tmp_path, monkeypatch)
    partner_id = create_partner("Ada North", email="ada@north.example")
    service_id = create_service("Consulting", "consulting")
    open_id = create_deal(partner_id, service_id, source="referral", value_estimate=700)
    lost_id = create_deal(partner_id, service_id, source="old")
    kept_closed_id = create_deal(partner_id, service_id, source="older")
    set_deal_stage(lost_id, "lost")
    set_deal_stage(kept_closed_id, "lost")
    with get_db() as conn:
        conn.execute(
            "UPDATE deals SET closed_at = NULL, updated_at = ? WHERE id = ?",
            ("2026-03-04T05:06:07", lost_id),
        )
        conn.execute(
            "UPDATE deals SET closed_at = ?, updated_at = ? WHERE id = ?",
            ("2026-02-02T00:00:00", "2026-04-04T00:00:00", kept_closed_id),
        )
        conn.commit()
    apply_deal_tags(open_id, replace=["campaign:physio"])
    log_activity(partner_id, "note", "Called once", deal_id=open_id)
    task, error, created = create_task(open_id, "Send the brief", owner="willow")
    assert error is None and created is True

    with get_db() as conn:
        before = {
            "deals": [row["id"] for row in conn.execute("SELECT id FROM deals ORDER BY id")],
            "activities": [row["id"] for row in conn.execute("SELECT id FROM activities ORDER BY id")],
            "tasks": [row["id"] for row in conn.execute("SELECT id FROM delegated_tasks ORDER BY id")],
            "tags": [
                (row["deal_id"], row["tag"])
                for row in conn.execute("SELECT deal_id, tag FROM deal_tags ORDER BY deal_id, tag")
            ],
        }

    _finish_migration(monkeypatch)

    with get_db() as conn:
        assert get_user_version(conn) == 8
        assert [row["id"] for row in conn.execute("SELECT id FROM deals ORDER BY id")] == before["deals"]
        assert [row["id"] for row in conn.execute("SELECT id FROM activities ORDER BY id")] == before["activities"]
        assert [row["id"] for row in conn.execute("SELECT id FROM delegated_tasks ORDER BY id")] == before["tasks"]
        tags = [
            (row["deal_id"], row["tag"])
            for row in conn.execute("SELECT deal_id, tag FROM deal_tags ORDER BY deal_id, tag")
        ]
        assert tags == before["tags"]
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert broken == []

        open_row = conn.execute("SELECT * FROM deals WHERE id = ?", (open_id,)).fetchone()
        assert open_row["type"] == "opportunity"
        assert open_row["active"] == 1
        assert open_row["priority"] == 0
        assert open_row["partner_id"] == partner_id
        assert open_row["service_id"] == service_id
        assert open_row["company_name"] is None
        assert open_row["email"] is None

        lost_row = conn.execute("SELECT * FROM deals WHERE id = ?", (lost_id,)).fetchone()
        assert lost_row["active"] == 0
        assert lost_row["stage"] == "lost"
        assert lost_row["closed_at"] == "2026-03-04T05:06:07"
        reason = conn.execute(
            "SELECT name FROM lost_reasons WHERE id = ?", (lost_row["lost_reason_id"],)
        ).fetchone()["name"]
        assert reason == "Lost stage (migrated)"

        kept = conn.execute("SELECT closed_at, active FROM deals WHERE id = ?", (kept_closed_id,)).fetchone()
        assert kept["active"] == 0
        assert kept["closed_at"] == "2026-02-02T00:00:00"

        names = [row["name"] for row in conn.execute("SELECT name FROM lost_reasons ORDER BY id")]
        assert names == [
            "Not a fit", "No budget", "Not now", "No response",
            "Lost to competitor", "Duplicate", "Lost stage (migrated)",
        ]
        nurture = conn.execute(
            "SELECT triggers_nurture FROM lost_reasons WHERE name = 'Not now'"
        ).fetchone()["triggers_nurture"]
        assert nurture == 1
        activity = conn.execute(
            "SELECT source_url, partner_id FROM activities WHERE deal_id = ?", (open_id,)
        ).fetchall()
        assert activity
        assert all(row["source_url"] is None for row in activity)
        task_row = conn.execute(
            "SELECT partner_id, title FROM delegated_tasks WHERE id = ?", (task["id"],)
        ).fetchone()
        assert task_row["title"] == "Send the brief"
        assert task_row["partner_id"] == partner_id

    partner_info = conn_nullable = None
    info = sqlite3.connect(path).execute("PRAGMA table_info(deals)").fetchall()
    by_name = {row[1]: row for row in info}
    assert by_name["partner_id"][3] == 0
    assert by_name["service_id"][3] == 0
    assert by_name["type"][3] == 1
    assert by_name["active"][3] == 1
    assert conn_nullable is None and partner_info is None


def test_version0_column_order_lands_in_the_right_columns(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL DEFAULT 'new',
            value_estimate REAL,
            source TEXT,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            service_id INTEGER NOT NULL REFERENCES services(id),
            pain_points TEXT,
            goals TEXT,
            next_action TEXT,
            next_action_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            closed_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO deals (
            id, stage, value_estimate, source, partner_id, service_id,
            pain_points, goals, next_action, next_action_date,
            created_at, updated_at, closed_at
        ) VALUES (4, 'qualified', 42.5, 'directory', 3, 9, 'phone only',
                  'second clinic', 'call', '2026-10-08',
                  '2026-01-01T00:00:00', '2026-01-02T00:00:00', NULL)
        """
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM deals WHERE id = 4").fetchone()
    assert row["stage"] == "qualified"
    assert row["value_estimate"] == 42.5
    assert row["source"] == "directory"
    assert row["partner_id"] == 3
    assert row["service_id"] == 9
    assert row["pain_points"] == "phone only"
    assert row["goals"] == "second clinic"
    assert row["next_action"] == "call"
    assert row["next_action_date"] == "2026-10-08"
    assert row["type"] == "opportunity"
    assert row["active"] == 1
    assert get_user_version(conn) == SCHEMA_VERSION
    conn.close()


def test_deleted_highest_id_is_not_reused(tmp_path, monkeypatch):
    _at_schema_7(tmp_path, monkeypatch)
    partner_id = create_partner("Ada", email="ada@x.example")
    service_id = create_service("Consulting", "consulting")
    first = create_deal(partner_id, service_id)
    highest = create_deal(partner_id, service_id)
    assert highest > first
    with get_db() as conn:
        conn.execute("DELETE FROM deals WHERE id = ?", (highest,))
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'deals'"
        ).fetchone()["seq"]
    assert seq >= highest
    _finish_migration(monkeypatch)
    new_id = create_deal(partner_id, service_id)
    assert new_id > highest
    assert new_id > seq


def test_empty_table_keeps_sqlite_sequence(tmp_path, monkeypatch):
    _at_schema_7(tmp_path, monkeypatch)
    partner_id = create_partner("Ada", email="ada@x.example")
    service_id = create_service("Consulting", "consulting")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO deals (id, partner_id, service_id, stage)
               VALUES (40, ?, ?, 'new')""",
            (partner_id, service_id),
        )
        conn.execute("DELETE FROM deals")
        conn.commit()
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'deals'"
        ).fetchone()["seq"]
    assert seq >= 40
    _finish_migration(monkeypatch)
    new_id = create_deal(partner_id, service_id)
    assert new_id > seq


def test_dangling_offer_warns_and_completes(tmp_path, monkeypatch, caplog):
    _at_schema_7(tmp_path, monkeypatch)
    partner_id = create_partner("Ada", email="ada@x.example")
    service_id = create_service("Consulting", "consulting")
    deal_id = create_deal(partner_id, service_id)
    with get_db() as conn:
        conn.execute("UPDATE deals SET offer_id = 99999 WHERE id = ?", (deal_id,))
        conn.commit()
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION)
    with caplog.at_level(logging.WARNING, logger="app.database"):
        init_db()
    assert "foreign_key_check" in caplog.text
    assert "offers" in caplog.text
    with get_db() as conn:
        assert get_user_version(conn) == 8
        assert conn.execute(
            "SELECT offer_id FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()["offer_id"] == 99999


def test_forced_error_after_drop_rolls_back(tmp_path, monkeypatch):
    path = _at_schema_7(tmp_path, monkeypatch)
    partner_id = create_partner("Ada", email="ada@x.example")
    service_id = create_service("Consulting", "consulting")
    deal_id = create_deal(partner_id, service_id, pain_points="keep me")
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION)
    monkeypatch.setattr(database, "REBUILD_FAIL_AFTER_STEP", 5)
    with pytest.raises(RuntimeError, match="forced failure after step 5"):
        init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    names = {row["name"] for row in conn.execute("PRAGMA table_info(deals)")}
    assert "type" not in names
    assert "active" not in names
    partner = next(row for row in conn.execute("PRAGMA table_info(deals)") if row["name"] == "partner_id")
    assert partner["notnull"] == 1
    assert conn.execute(
        "SELECT pain_points FROM deals WHERE id = ?", (deal_id,)
    ).fetchone()["pain_points"] == "keep me"
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "lost_reasons" not in tables
    conn.close()


def test_init_db_twice_is_a_noop(db):
    with get_db() as conn:
        first = conn.execute("SELECT COUNT(*) AS n FROM lost_reasons").fetchone()["n"]
        version = get_user_version(conn)
    assert version == 8
    assert first == 7
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == 8
        assert conn.execute("SELECT COUNT(*) AS n FROM lost_reasons").fetchone()["n"] == 7


def test_fresh_schema_matches_migrated_schema(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh" / "data" / "crm.db"
    fresh.parent.mkdir(parents=True)
    migrated = tmp_path / "migrated" / "data" / "crm.db"
    migrated.parent.mkdir(parents=True)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setattr(database, "DB_PATH", str(fresh))
    init_db()
    monkeypatch.setattr(database, "SCHEMA_VERSION", 7)
    monkeypatch.setattr(database, "DB_PATH", str(migrated))
    init_db()
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION)
    init_db()
    assert _objects(fresh) == _objects(migrated)
    conn = sqlite3.connect(fresh)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'deals'"
        )
    }
    assert {
        "idx_deals_partner", "idx_deals_stage", "idx_deals_owner", "idx_deals_parent",
        "idx_deals_type_active", "idx_deals_email", "idx_deals_phone",
        "idx_deals_website", "idx_deals_lost_reason", "idx_deals_merged_into",
    } <= indexes
    conn.close()


def test_list_deals_hides_leads_and_lost_by_default(db):
    partner_id = create_partner("Ada", email="ada@x.example")
    service_id = create_service("Consulting", "consulting")
    open_id = create_deal(partner_id, service_id)
    lost_id = create_deal(partner_id, service_id, source="old")
    with get_db() as conn:
        conn.execute("UPDATE deals SET active = 0 WHERE id = ?", (lost_id,))
        conn.execute(
            """INSERT INTO deals (stage, type, company_name, email, active)
               VALUES ('new', 'lead', 'Harbour Physio', 'pm@h.example', 1)"""
        )
        conn.commit()
        lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert [row["id"] for row in list_deals()] == [open_id]
    leads = list_deals(type="lead")
    assert [row["id"] for row in leads] == [lead_id]
    assert leads[0]["partner_name"] is None
    assert leads[0]["service_name"] is None
    assert leads[0]["company_name"] == "Harbour Physio"
    assert {row["id"] for row in list_deals(include_lost=True)} == {open_id, lost_id}
    assert {row["id"] for row in list_deals(type="all", include_lost=True)} == {
        open_id, lost_id, lead_id,
    }
    assert get_deal(lead_id)["type"] == "lead"
    assert get_deal(lead_id)["partner_id"] is None


def test_get_deal_and_task_tolerate_a_null_partner(db):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO deals (stage, type, company_name, website, active)
               VALUES ('new', 'lead', 'Harbour Physio', 'https://harbour.example', 1)"""
        )
        conn.commit()
        lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    got = get_deal_tool({"deal_id": lead_id})
    assert got["ok"] is True
    assert got["partner"] is None
    assert got["service"] is None
    assert got["deal"]["type"] == "lead"
    assert got["deal"]["company_name"] == "Harbour Physio"
    created = create_delegated_task_tool({"deal_id": lead_id, "title": "Read the website"})
    assert created["ok"] is True
    assert created["partner"] is None
    assert created["task"]["partner_id"] is None


def test_backup_name_is_v7_to_v8(tmp_path, monkeypatch):
    _at_schema_7(tmp_path, monkeypatch)
    _finish_migration(monkeypatch)
    backups = list((tmp_path / "backups").glob("pre-migrate-v7-to-v8-*.db"))
    assert len(backups) == 1
