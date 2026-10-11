"""ADR 0001 P1: schema 8 rebuild, null-safe reads, stages is_lost deprecation."""
import logging
import sqlite3

import pytest

from app.database import (
    LOST_REASON_SEEDS,
    SCHEMA_VERSION,
    get_db,
    get_user_version,
    init_db,
)
from app.services.catalog import create_service, get_service
from app.services.deals import get_deal, get_open_deal_for_partner_service, list_deals
from app.services.partners import create_partner, get_partner
from app.services import pipeline_stages
from tests.lead_model_fixtures import (
    apply_through_v7,
    build_v0_reordered_deals,
    insert_lead,
    populate_schema_7_sample,
    sqlite_master_snapshot,
)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_schema_7_fixture_preserves_ids_and_backfills_lost(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    apply_through_v7(__import__("app.database", fromlist=["*"]))
    sample = populate_schema_7_sample(__import__("app.database", fromlist=["*"]))

    with sqlite3.connect(path) as conn:
        before = {
            "deals": _count(conn, "deals"),
            "activities": _count(conn, "activities"),
            "delegated_tasks": _count(conn, "delegated_tasks"),
            "deal_tags": _count(conn, "deal_tags"),
        }
        before_ids = {
            row[0] for row in conn.execute("SELECT id FROM deals")
        }
        tag_pairs = {
            (row[0], row[1])
            for row in conn.execute("SELECT deal_id, tag FROM deal_tags")
        }

    init_db()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _count(conn, "deals") == before["deals"]
        assert _count(conn, "activities") == before["activities"]
        assert _count(conn, "delegated_tasks") == before["delegated_tasks"]
        assert _count(conn, "deal_tags") == before["deal_tags"]
        assert {row[0] for row in conn.execute("SELECT id FROM deals")} == before_ids
        assert {
            (row[0], row[1])
            for row in conn.execute("SELECT deal_id, tag FROM deal_tags")
        } == tag_pairs
        assert tag_pairs == {(sample["open_id"], "campaign:p1-fixture")}

        open_deal = dict(conn.execute(
            "SELECT * FROM deals WHERE id = ?", (sample["open_id"],)
        ).fetchone())
        assert open_deal["type"] == "opportunity"
        assert open_deal["active"] == 1
        assert open_deal["priority"] == 0
        assert open_deal["partner_id"] == sample["partner_id"]
        assert open_deal["service_id"] == sample["service_id"]

        lost = dict(conn.execute(
            "SELECT * FROM deals WHERE id = ?", (sample["lost_id"],)
        ).fetchone())
        assert lost["active"] == 0
        reason = conn.execute(
            "SELECT name FROM lost_reasons WHERE id = ?", (lost["lost_reason_id"],)
        ).fetchone()
        assert reason["name"] == "Lost stage (migrated)"
        assert lost["stage"] == "lost"

        null_closed = dict(conn.execute(
            "SELECT * FROM deals WHERE id = ?", (sample["null_closed_id"],)
        ).fetchone())
        assert null_closed["active"] == 0
        assert null_closed["closed_at"] == sample["null_closed_updated_at"]

        names = {row[0] for row in conn.execute("SELECT name FROM lost_reasons")}
        assert names == {name for name, _nurture in LOST_REASON_SEEDS}

        for table in ("deals", "activities", "delegated_tasks", "deal_tags"):
            assert conn.execute(f"PRAGMA foreign_key_check({table})").fetchall() == []

        activity = conn.execute(
            "SELECT partner_id, deal_id FROM activities WHERE deal_id = ?",
            (sample["open_id"],),
        ).fetchone()
        assert activity["partner_id"] == sample["partner_id"]
        task = conn.execute(
            "SELECT deal_id, partner_id FROM delegated_tasks WHERE id = ?",
            (sample["task_id"],),
        ).fetchone()
        assert task["deal_id"] == sample["open_id"]
        assert task["partner_id"] == sample["partner_id"]

    backups = list((tmp_path / "backups").glob("pre-migrate-v7-to-v8-*.db"))
    assert backups, "expected pre-migrate-v7-to-v8 backup"


def test_version_0_column_order_maps_by_name(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    build_v0_reordered_deals(path)
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    init_db()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        deal = dict(conn.execute("SELECT * FROM deals WHERE id = 1").fetchone())
    assert deal["source"] == "SRC_UNIQUE"
    assert deal["value_estimate"] == 12345.67
    assert deal["pain_points"] == "PAIN_UNIQUE"
    assert deal["goals"] == "GOAL_UNIQUE"
    assert deal["closed_at"] == "2020-01-01T00:00:00"
    assert deal["owner_key"] == "owner-a"
    assert deal["external_ref"] == "REF_UNIQUE"
    assert deal["stage"] == "contacted"
    assert deal["type"] == "opportunity"
    assert deal["partner_id"] == 1
    assert deal["service_id"] == 1


def test_deleted_highest_id_is_not_reused(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    database = __import__("app.database", fromlist=["*"])
    apply_through_v7(database)
    sample = populate_schema_7_sample(database, null_closed_lost=False)
    highest = max(sample["open_id"], sample["lost_id"])
    with database.get_db() as db:
        db.execute("DELETE FROM deals WHERE id = ?", (highest,))
        db.commit()
        seq = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'deals'"
        ).fetchone()[0]
    assert seq >= highest
    init_db()
    from app.services.deals import create_deal
    new_id = create_deal(sample["partner_id"], sample["service_id"])
    assert new_id > highest


def test_empty_table_keeps_sqlite_sequence(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    database = __import__("app.database", fromlist=["*"])
    apply_through_v7(database)
    sample = populate_schema_7_sample(database, null_closed_lost=False)
    with database.get_db() as db:
        db.execute("DELETE FROM deal_tags")
        db.execute("DELETE FROM activities")
        db.execute("DELETE FROM delegated_tasks")
        db.execute("DELETE FROM deals")
        db.commit()
        seq = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'deals'"
        ).fetchone()[0]
    assert seq >= sample["lost_id"]
    init_db()
    from app.services.deals import create_deal
    new_id = create_deal(sample["partner_id"], sample["service_id"])
    assert new_id > seq


def test_dangling_offer_id_warns_but_completes(tmp_path, monkeypatch, caplog):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    database = __import__("app.database", fromlist=["*"])
    apply_through_v7(database)
    sample = populate_schema_7_sample(database, dangling_offer=True, null_closed_lost=False)
    with caplog.at_level(logging.WARNING, logger="app.database"):
        init_db()
    assert "pre-existing foreign key violations" in caplog.text
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        deal = conn.execute(
            "SELECT offer_id, type FROM deals WHERE id = ?",
            (sample["dangling_id"],),
        ).fetchone()
        assert deal["offer_id"] == 9999
        assert deal["type"] == "opportunity"


def test_forced_error_after_drop_rolls_back(tmp_path, monkeypatch):
    import app.database as database

    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    apply_through_v7(database)
    populate_schema_7_sample(database, null_closed_lost=False)

    def boom(table):
        raise RuntimeError(f"forced error after step 5 ({table})")

    database._migrate_008_after_drop = boom
    try:
        with pytest.raises(RuntimeError, match="forced error after step 5"):
            init_db()
    finally:
        database._migrate_008_after_drop = None

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(deals)")}
        assert "type" not in cols
        assert cols["partner_id"]["notnull"] == 1
        assert cols["service_id"]["notnull"] == 1
        assert "lost_reasons" not in {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_init_db_twice_is_noop_after_schema_8(db):
    init_db()
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        first = [dict(r) for r in conn.execute("SELECT * FROM lost_reasons ORDER BY id")]
    init_db()
    with get_db() as conn:
        second = [dict(r) for r in conn.execute("SELECT * FROM lost_reasons ORDER BY id")]
    assert first == second


def test_fresh_and_migrated_schemas_match(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh" / "crm.db"
    migrated = tmp_path / "migrated" / "crm.db"
    fresh.parent.mkdir()
    migrated.parent.mkdir()
    monkeypatch.delenv("BACKUP_DIR", raising=False)

    monkeypatch.setattr("app.database.DB_PATH", str(fresh))
    init_db()
    fresh_snap = sqlite_master_snapshot(fresh)

    monkeypatch.setattr("app.database.DB_PATH", str(migrated))
    apply_through_v7(__import__("app.database", fromlist=["*"]))
    init_db()
    migrated_snap = sqlite_master_snapshot(migrated)

    assert fresh_snap == migrated_snap
    with sqlite3.connect(fresh) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def test_list_deals_defaults_exclude_lost_and_leads(db):
    partner_id = create_partner("Jane Doe", email="jane@x.example")
    service_id = create_service("Consulting", "consulting-list")
    from app.services.deals import create_deal

    open_id = create_deal(partner_id, service_id)
    other_id = create_deal(partner_id, service_id)
    lead_id = insert_lead(db, company_name="Harbour Physio")
    with get_db() as conn:
        conn.execute(
            """UPDATE deals SET active = 0, lost_reason_id = (
                   SELECT id FROM lost_reasons WHERE name = 'Not a fit'
               ) WHERE id = ?""",
            (open_id,),
        )
        conn.commit()

    default_rows = list_deals()
    assert {row["id"] for row in default_rows} == {other_id}
    assert all(row["type"] == "opportunity" for row in default_rows)
    assert all(row["active"] == 1 for row in default_rows)

    leads = list_deals(type="lead")
    assert [row["id"] for row in leads] == [lead_id]
    assert leads[0]["partner_id"] is None
    assert leads[0]["service_id"] is None
    assert leads[0]["partner_name"] is None
    assert leads[0]["service_name"] is None
    assert leads[0]["company_name"] == "Harbour Physio"

    everyone = list_deals(type="all", include_lost=True)
    assert {row["id"] for row in everyone} == {open_id, lead_id, other_id}

    lost_only = list_deals(include_lost=True)
    assert {row["id"] for row in lost_only} == {open_id, other_id}


def test_get_open_deal_ignores_active_zero(db):
    partner_id = create_partner("Jane Doe", email="jane@x.example")
    service_id = create_service("Consulting", "consulting-open")
    from app.services.deals import create_deal

    deal_id = create_deal(partner_id, service_id)
    assert get_open_deal_for_partner_service(partner_id, service_id)["id"] == deal_id
    with get_db() as conn:
        conn.execute(
            """UPDATE deals SET active = 0, lost_reason_id = (
                   SELECT id FROM lost_reasons WHERE name = 'Not a fit'
               ) WHERE id = ?""",
            (deal_id,),
        )
        conn.commit()
    assert get_open_deal_for_partner_service(partner_id, service_id) is None


def test_get_partner_and_service_accept_null():
    assert get_partner(None) is None
    assert get_partner(0) is None
    assert get_service(None) is None
    assert get_service(0) is None


def test_get_deal_tool_and_delegated_task_tolerate_null_partner(db):
    from app.mcp.tools import create_delegated_task_tool, get_deal_tool

    lead_id = insert_lead(db, company_name="Harbour Physio")
    got = get_deal_tool({"deal_id": lead_id})
    assert got["ok"] is True
    assert got["partner"] is None
    assert got["service"] is None
    assert got["deal"]["id"] == lead_id

    created = create_delegated_task_tool({
        "deal_id": lead_id, "title": "Research the website",
    })
    assert created["ok"] is True
    assert created["partner"] is None
    assert created["task"]["partner_id"] is None


def test_lost_child_does_not_block_new_child(db):
    from app.services.deals import create_deal, set_deal_stage
    from app.services.stage_automations import create_automation

    pipeline_stages.create_stage("hc_presented", "HC presented", position=20)
    parent_service = create_service("Health Check", "health-check-p1")
    child_service = create_service("Automation Delivery", "automation-delivery-p1")
    automation, error = create_automation(
        "Spawn delivery",
        "hc_presented",
        actions=[{
            "type": "spawn_child_deals",
            "service_ids": [child_service],
        }],
    )
    assert error is None
    partner_id = create_partner("Clinic", email="clinic@x.example")
    parent_id = create_deal(partner_id, parent_service)
    assert set_deal_stage(parent_id, "hc_presented") is True
    children = list_deals(parent_deal_id=parent_id)
    assert len(children) == 1
    child_id = children[0]["id"]
    with get_db() as conn:
        conn.execute(
            """UPDATE deals SET active = 0, lost_reason_id = (
                   SELECT id FROM lost_reasons WHERE name = 'Not a fit'
               ) WHERE id = ?""",
            (child_id,),
        )
        conn.commit()
    assert list_deals(parent_deal_id=parent_id) == []
    assert set_deal_stage(parent_id, "qualified") is True
    assert set_deal_stage(parent_id, "hc_presented") is True
    after = list_deals(parent_deal_id=parent_id)
    assert len(after) == 1
    assert after[0]["id"] != child_id
    lost_rows = list_deals(parent_deal_id=parent_id, include_lost=True)
    assert {row["id"] for row in lost_rows} == {child_id, after[0]["id"]}


def test_create_stage_refuses_new_is_lost(db):
    stage, error = pipeline_stages.create_stage("dead", "Dead", is_lost=True)
    assert stage is None
    assert error == "invalid_stage_role"
    assert pipeline_stages.get_stage("dead") is None


def test_update_stage_refuses_is_lost_zero_to_one(db):
    stage, error = pipeline_stages.update_stage("proposal", is_lost=True)
    assert stage is None
    assert error == "invalid_stage_role"
    assert pipeline_stages.get_stage("proposal")["is_lost"] == 0


def test_update_stage_accepts_is_lost_on_existing_lost(db):
    stage, error = pipeline_stages.update_stage("lost", label="Closed lost", is_lost=True)
    assert error is None
    assert stage["label"] == "Closed lost"
    assert stage["is_lost"] == 1


def test_migrated_lost_deal_fixture_shape(tmp_path, monkeypatch):
    """P2 restore_deal can reuse this: lost-stage backfill leaves stage='lost'."""
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    database = __import__("app.database", fromlist=["*"])
    apply_through_v7(database)
    sample = populate_schema_7_sample(database, null_closed_lost=False)
    init_db()
    deal = get_deal(sample["lost_id"])
    assert deal["active"] == 0
    assert deal["stage"] == "lost"
    assert deal["type"] == "opportunity"
    assert deal["lost_reason_id"]
