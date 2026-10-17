"""Deal tags: set, replace, merge, bulk, filter, list_tags, ingest, backfill."""

from app.database import SCHEMA_VERSION, get_db, get_user_version, init_db
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service, get_or_create_service
from app.services.deal_tags import (
    apply_deal_tags,
    backfill_campaign_tags,
    list_tags,
)
from app.services.deals import create_deal, get_deal, list_deals
from app.services.leads import ingest_lead
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call

CAMPAIGN = "campaign:example-2026-09"


def _deal(db, name="Jane Doe", email="jane@acme.example", **fields):
    pid = create_partner(name, email=email)
    service, _created = get_or_create_service("Consulting", "consulting")
    sid = service["id"]
    deal_id = create_deal(pid, sid, source="referral", **fields)
    return pid, sid, deal_id


def test_get_and_list_return_empty_tags(db):
    _, _, deal_id = _deal(db)
    assert get_deal(deal_id)["tags"] == []
    assert list_deals()[0]["tags"] == []


def test_create_deal_sets_tags_normalized(db):
    _, _, deal_id = _deal(db, tags=["Clinic", CAMPAIGN])
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "clinic"]


def test_replace_add_and_remove(db):
    _, _, deal_id = _deal(db, tags=["clinic", "referral"])

    assert apply_deal_tags(deal_id, replace=["clinic"]) is None
    assert get_deal(deal_id)["tags"] == ["clinic"]

    assert apply_deal_tags(deal_id, add=["referral", CAMPAIGN]) is None
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "clinic", "referral"]

    assert apply_deal_tags(deal_id, remove=["clinic"]) is None
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "referral"]

    # replace runs first, then add, then remove (remove wins on overlap)
    assert apply_deal_tags(
        deal_id, replace=["clinic"], add=[CAMPAIGN, "referral"], remove=["clinic"],
    ) is None
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "referral"]

    assert apply_deal_tags(deal_id, replace=[]) is None
    assert get_deal(deal_id)["tags"] == []


def test_omitted_tag_ops_leave_the_set_alone(db):
    _, _, deal_id = _deal(db, tags=["clinic"])
    assert apply_deal_tags(deal_id) is None
    assert get_deal(deal_id)["tags"] == ["clinic"]


def test_list_filter_matches_all_and_combines_with_stage(db):
    pid, sid, first = _deal(db, tags=["clinic", "referral"])
    second = create_deal(pid, sid, tags=["clinic"], stage="contacted")
    third = create_deal(
        create_partner("Other", email="other@acme.example"),
        sid,
        tags=[CAMPAIGN],
    )

    assert {d["id"] for d in list_deals(tags=["clinic"])} == {first, second}
    assert [d["id"] for d in list_deals(tags=["clinic", "referral"])] == [first]
    assert [d["id"] for d in list_deals(tags=["clinic"], stage="contacted")] == [second]
    assert list_deals(tags=["not a tag"]) == []
    assert list_deals(tags=[])[0]["id"] in {first, second, third}
    assert {d["id"] for d in list_deals()} == {first, second, third}


def test_list_tags_counts(db):
    pid, sid, _ = _deal(db, tags=["clinic", "referral"])
    create_deal(pid, sid, tags=["clinic"], stage="contacted")
    assert list_tags() == [
        {"tag": "clinic", "count": 2},
        {"tag": "referral", "count": 1},
    ]


def test_bulk_add_and_remove_by_deal_ids(client, db, monkeypatch):
    _enable(monkeypatch)
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    first = create_deal(pid, sid, tags=["clinic"])
    second = create_deal(
        create_partner("Other", email="other@acme.example"), sid, tags=["clinic"],
    )
    untouched = create_deal(
        create_partner("Skip", email="skip@acme.example"), sid, tags=["clinic"],
    )

    payload, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [first, second],
        "add_tags": [CAMPAIGN, "Referral"],
    })
    assert is_error is False
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert get_deal(first)["tags"] == [CAMPAIGN, "clinic", "referral"]
    assert get_deal(second)["tags"] == [CAMPAIGN, "clinic", "referral"]
    assert get_deal(untouched)["tags"] == ["clinic"]

    payload, _ = call(client, "bulk_update_deals", {
        "deal_ids": [first],
        "remove_tags": ["clinic"],
    })
    assert payload["ok"] is True
    assert get_deal(first)["tags"] == [CAMPAIGN, "referral"]
    assert get_deal(second)["tags"] == [CAMPAIGN, "clinic", "referral"]


def test_mcp_set_replace_merge_filter_and_list_tags(client, db, monkeypatch):
    _enable(monkeypatch)
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")

    created, _ = call(client, "create_deal", {
        "partner_id": pid,
        "service_id": sid,
        "tags": ["Clinic", CAMPAIGN],
    })
    assert created["ok"] is True
    deal_id = created["deal"]["id"]
    assert created["deal"]["tags"] == [CAMPAIGN, "clinic"]

    got, _ = call(client, "get_deal", {"deal_id": deal_id})
    assert got["deal"]["tags"] == [CAMPAIGN, "clinic"]

    replaced, _ = call(client, "update_deal", {
        "deal_id": deal_id,
        "tags": ["clinic"],
    })
    assert replaced["deal"]["tags"] == ["clinic"]

    merged, _ = call(client, "update_deal", {
        "deal_id": deal_id,
        "add_tags": ["referral"],
        "remove_tags": ["missing"],
    })
    assert merged["deal"]["tags"] == ["clinic", "referral"]

    listed, _ = call(client, "list_deals", {"tags": ["clinic", "referral"], "stage": "new"})
    assert [d["id"] for d in listed["deals"]] == [deal_id]

    single, _ = call(client, "list_deals", {"tag": "clinic"})
    assert [d["id"] for d in single["deals"]] == [deal_id]

    tags, _ = call(client, "list_tags")
    assert tags["ok"] is True
    assert {row["tag"]: row["count"] for row in tags["tags"]} == {"clinic": 1, "referral": 1}

    bad, _ = call(client, "update_deal", {"deal_id": deal_id, "add_tags": ["has space"]})
    assert bad["ok"] is False
    assert bad["error"] == "invalid_tag"
    assert get_deal(deal_id)["tags"] == ["clinic", "referral"]


def test_ingest_merges_tags_and_omit_leaves_them(db):
    create_service("Consulting", "consulting")
    lead = {
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "tags": ["Clinic", "not a tag", CAMPAIGN],
    }
    created = ingest_lead(lead)
    assert created["status"] == "created"
    assert get_deal(created["deal_id"])["tags"] == [CAMPAIGN, "clinic"]

    again = ingest_lead({
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "tags": ["referral"],
    })
    assert again["status"] == "duplicate_open_deal"
    assert get_deal(created["deal_id"])["tags"] == [CAMPAIGN, "clinic", "referral"]

    omitted = ingest_lead({
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "source": "referral",
    })
    assert omitted["status"] == "duplicate_open_deal"
    assert get_deal(created["deal_id"])["tags"] == [CAMPAIGN, "clinic", "referral"]


def test_ingest_api_accepts_tags(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")
    resp = client.post("/api/v1/leads", json={"leads": [{
        "type": "opportunity",
        "name": "Jane Doe",
        "email": "jane@acme.example",
        "service_slug": "consulting",
        "tags": ["referral"],
    }]}, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    deal_id = resp.json()["results"][0]["deal_id"]
    assert get_deal(deal_id)["tags"] == ["referral"]


def test_backfill_copies_campaign_external_ref_and_leaves_it(db):
    _, _, campaign_id = _deal(db, external_ref=f"  {CAMPAIGN.upper()}  ")
    # create_deal sanitizes and strips; mixed case is kept on the column.
    _, _, other_id = _deal(
        db, name="Other", email="other@acme.example", external_ref="acme-retail-2026",
    )
    _, _, blank_id = _deal(db, name="Blank", email="blank@acme.example", external_ref="campaign:")
    assert get_deal(campaign_id)["tags"] == []

    with get_db() as conn:
        inserted = backfill_campaign_tags(conn)
        conn.commit()
    assert inserted == 1

    campaign = get_deal(campaign_id)
    assert campaign["tags"] == [CAMPAIGN]
    assert campaign["external_ref"] == CAMPAIGN.upper()
    assert get_deal(other_id)["tags"] == []
    assert get_deal(blank_id)["tags"] == []
    assert get_deal(other_id)["external_ref"] == "acme-retail-2026"

    with get_db() as conn:
        assert backfill_campaign_tags(conn) == 0
        conn.commit()
    assert get_deal(campaign_id)["tags"] == [CAMPAIGN]


def test_migrate_002_adds_deal_tags_on_v1_and_does_not_restore_a_removed_tag(db):
    _, _, deal_id = _deal(db, external_ref=CAMPAIGN)
    with get_db() as conn:
        conn.execute("DROP TABLE deal_tags")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == SCHEMA_VERSION
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "deal_tags" in names
    assert get_deal(deal_id)["tags"] == [CAMPAIGN]
    assert get_deal(deal_id)["external_ref"] == CAMPAIGN

    apply_deal_tags(deal_id, replace=[])
    assert get_deal(deal_id)["tags"] == []
    init_db()
    assert get_deal(deal_id)["tags"] == []
    assert get_deal(deal_id)["external_ref"] == CAMPAIGN


def test_deals_page_shows_tags_and_filters(logged_in_client, db):
    pid, sid, deal_id = _deal(db, tags=["clinic", "referral"])
    create_deal(
        create_partner("Other", email="other@acme.example"),
        sid,
        tags=["clinic"],
    )

    page = logged_in_client.get("/crm/deals")
    assert page.status_code == 200
    assert "clinic" in page.text
    assert "referral" in page.text
    assert page.text.count("Other") == 1

    filtered = logged_in_client.get("/crm/deals?tag=referral")
    assert "Jane Doe" in filtered.text
    assert "Other" not in filtered.text
    assert "pill-tag-on" in filtered.text

    both = logged_in_client.get("/crm/deals?tag=clinic&stage=new")
    assert "Jane Doe" in both.text
    assert "Other" in both.text

    partner = logged_in_client.get(f"/contacts/partners/{pid}")
    assert "clinic" in partner.text
    assert f"/crm/deals?tag=clinic" in partner.text

    board = logged_in_client.get("/crm/pipeline?tag=referral")
    assert "Jane Doe" in board.text
    assert "Other" not in board.text

    edit = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert 'name="tags"' in edit.text
    assert "clinic, referral" in edit.text

    saved = logged_in_client.post(
        f"/crm/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
            "source": "referral",
            "value_estimate": "",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
            "tags": "CLINIC, " + CAMPAIGN,
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "clinic"]

    rejected = logged_in_client.post(
        f"/crm/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(logged_in_client.cookies.get("session")),
            "source": "referral",
            "value_estimate": "",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
            "tags": "has space",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    assert "Invalid tag" in rejected.text
    assert get_deal(deal_id)["tags"] == [CAMPAIGN, "clinic"]
