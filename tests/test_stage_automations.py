"""Stage automations: operator-configured, empty until someone creates one."""
from datetime import timedelta

import pytest

from app.services.auth import generate_csrf_token
from app.services.catalog import create_service, get_service_by_slug
from app.services.deals import create_deal, get_deal, list_deals, set_deal_stage
from app.services.delegated_tasks import complete_task, get_task, list_tasks
from app.services.offers import create_offer
from app.services.partners import create_partner
from app.services.pipeline_stages import create_stage
from app.services.stage_automations import (
    automation_source,
    create_automation,
    delete_automation,
    get_automation,
    list_automations,
    update_automation,
)
from app.services.staleness import operator_today
from scripts.seed_sevasek_automations import seed_sevasek_automations


def _catalog():
    create_stage("hc_booked", "HC booked", position=10)
    create_stage("hc_paid", "HC paid", position=11)
    create_stage("hc_in_delivery", "HC in delivery", position=12)
    create_stage("hc_presented", "HC presented", position=13)
    create_stage("hc_won", "HC won", position=14, is_won=True)
    services = {}
    for name, slug in (
        ("Health Check", "health-check"),
        ("Automation Delivery", "automation-delivery"),
        ("Automations Support", "automations-support"),
        ("IT Support", "it-support"),
        ("GEO", "geo"),
    ):
        services[slug] = create_service(name, slug)
    offer, _error = create_offer(
        "Health Check",
        service_id=services["health-check"],
        price=700,
        currency="AUD",
        is_default=True,
    )
    other, _error = create_offer(
        "GEO review",
        service_id=services["geo"],
        price=100,
        currency="AUD",
    )
    return services, offer, other


def _deal(services, slug="health-check", stage="new", offer_id=None, value=700):
    partner_id = create_partner("North Church", email="ada@north.example")
    deal_id = create_deal(
        partner_id,
        services[slug],
        stage=stage,
        offer_id=offer_id,
        value_estimate=value,
        attach_default_offer=offer_id is None,
    )
    return partner_id, deal_id


def _kickoff(**overrides):
    fields = {
        "name": "Send the kickoff",
        "stage_key": "hc_paid",
        "service_slug": "health-check",
        "actions": [{
            "type": "delegated_task",
            "owner": "willow",
            "title": "Send HC kickoff",
            "brief": "Paid Health Check. Send the kickoff.",
        }],
    }
    fields.update(overrides)
    automation, error = create_automation(**fields)
    assert error is None, error
    return automation


def test_fresh_database_runs_nothing(db):
    assert list_automations() == []
    service_id = create_service("Health Check", "health-check")
    create_stage("hc_paid", "HC paid")
    partner_id = create_partner("Ada")
    deal_id = create_deal(partner_id, service_id, stage="new")
    assert set_deal_stage(deal_id, "hc_paid") is True
    assert list_tasks(deal_id=deal_id) == []


def test_rename_disable_do_not_fire_until_enabled(db):
    services, _offer, _other = _catalog()
    automation = _kickoff(name="Before rename", actions=[{
        "type": "delegated_task",
        "owner": "willow",
        "title": "Hello {automation_name}",
        "brief": "For {partner_name}",
    }])
    renamed, error = update_automation(automation["id"], name="After rename")
    assert error is None
    taken, error = create_automation(
        "after rename",
        "hc_paid",
        actions=[{
            "type": "delegated_task",
            "owner": "willow",
            "title": "Other",
        }],
    )
    assert taken is None
    assert error == "name_taken"

    update_automation(automation["id"], enabled=False)
    _partner, deal_id = _deal(services)
    set_deal_stage(deal_id, "hc_paid")
    assert list_tasks(deal_id=deal_id) == []

    update_automation(automation["id"], enabled=True)
    set_deal_stage(deal_id, "new")
    set_deal_stage(deal_id, "hc_paid")
    tasks = list_tasks(deal_id=deal_id)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Hello After rename"
    assert tasks[0]["brief"] == "For North Church"
    assert tasks[0]["owner"] == "willow"
    assert tasks[0]["status"] == "delegated"
    assert get_automation(automation["id"])["name"] == "After rename"

    deleted, error = delete_automation(automation["id"])
    assert deleted is True and error is None
    assert list_automations() == []


def test_paid_task_is_idempotent_while_open_and_not_after_done(db):
    services, _offer, _other = _catalog()
    _kickoff()
    _partner, deal_id = _deal(services)
    assert set_deal_stage(deal_id, "hc_paid") is True
    assert len(list_tasks(deal_id=deal_id)) == 1
    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_paid")
    assert len(list_tasks(deal_id=deal_id)) == 1

    complete_task(list_tasks(deal_id=deal_id)[0]["id"], result_notes="sent")
    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_paid")
    assert len(list_tasks(deal_id=deal_id)) == 2
    assert len([task for task in list_tasks(deal_id=deal_id) if task["status"] != "done"]) == 1


def test_due_in_days_and_create_in_stage_does_not_fire(db):
    services, _offer, _other = _catalog()
    _kickoff(actions=[{
        "type": "delegated_task",
        "owner": "willow",
        "title": "Send HC kickoff",
        "brief": "Paid.",
        "due_in_days": 3,
    }])
    partner_id = create_partner("Already there")
    parked = create_deal(partner_id, services["health-check"], stage="hc_paid")
    assert list_tasks(deal_id=parked) == []

    _partner, deal_id = _deal(services, stage="hc_booked")
    set_deal_stage(deal_id, "hc_paid")
    task = list_tasks(deal_id=deal_id)[0]
    assert task["due_date"] == (operator_today() + timedelta(days=3)).isoformat()


def test_service_and_offer_scope(db):
    services, offer, other = _catalog()
    _kickoff()
    scoped, error = create_automation(
        "Offer only",
        "hc_presented",
        service_slug="health-check",
        offer_id=offer["id"],
        actions=[{
            "type": "delegated_task",
            "owner": "willow",
            "title": "Offer task",
            "brief": "",
        }],
    )
    assert error is None

    _partner, geo_id = _deal(services, slug="geo", value=None)
    set_deal_stage(geo_id, "hc_paid")
    set_deal_stage(geo_id, "hc_presented")
    assert list_tasks(deal_id=geo_id) == []

    _partner, bare_id = _deal(services, stage="hc_in_delivery", offer_id=other["id"], value=None)
    set_deal_stage(bare_id, "hc_presented")
    assert list_tasks(deal_id=bare_id) == []

    _partner, matched_id = _deal(services, stage="hc_in_delivery", offer_id=offer["id"])
    set_deal_stage(matched_id, "hc_presented")
    assert [task["title"] for task in list_tasks(deal_id=matched_id)] == ["Offer task"]


def test_children_are_scoped_idempotent_and_skip_a_missing_service(db):
    services, offer, _other = _catalog()
    automation, error = create_automation(
        "Spawn follow-ons",
        "hc_presented",
        service_slug="health-check",
        actions=[{
            "type": "spawn_child_deals",
            "service_slugs": ["automation-delivery", "automations-support"],
        }],
    )
    assert error is None
    assert "it-support" not in automation["actions"][0]["service_slugs"]
    partner_id, deal_id = _deal(services, stage="hc_in_delivery", offer_id=offer["id"], value=700)
    assert set_deal_stage(deal_id, "hc_presented") is True
    children = list_deals(parent_deal_id=deal_id)
    assert sorted(child["service_slug"] for child in children) == [
        "automation-delivery",
        "automations-support",
    ]
    for child in children:
        assert child["partner_id"] == partner_id
        assert child["parent_deal_id"] == deal_id
        assert child["source"] == automation_source(automation["id"])
        assert child["stage"] == "new"
        assert child["offer_id"] is None
        assert child["value_estimate"] is None
    parent = get_deal(deal_id)
    assert parent["value_estimate"] == 700
    assert parent["offer_id"] == offer["id"]

    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_presented")
    assert len(list_deals(parent_deal_id=deal_id)) == 2

    delivery = next(child for child in children if child["service_slug"] == "automation-delivery")
    set_deal_stage(delivery["id"], "won")
    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_presented")
    delivery_rows = [
        child for child in list_deals(parent_deal_id=deal_id)
        if child["service_slug"] == "automation-delivery"
    ]
    assert len(delivery_rows) == 2
    assert sum(1 for row in delivery_rows if row["stage"] != "won") == 1
    support_rows = [
        child for child in list_deals(parent_deal_id=deal_id)
        if child["service_slug"] == "automations-support"
    ]
    assert len(support_rows) == 1

    with db.get_db() as conn:
        conn.execute("DELETE FROM services WHERE slug = ?", ("automations-support",))
        conn.commit()
    _partner, fresh_id = _deal(services, stage="hc_in_delivery")
    assert set_deal_stage(fresh_id, "hc_presented") is True
    spawned = [child["service_slug"] for child in list_deals(parent_deal_id=fresh_id)]
    assert spawned == ["automation-delivery"]
    assert get_service_by_slug("automations-support") is None
    assert get_service_by_slug("it-support") is not None


def test_only_the_entered_stage_runs(db):
    services, _offer, _other = _catalog()
    _kickoff()
    create_automation(
        "Spawn follow-ons",
        "hc_presented",
        service_slug="health-check",
        actions=[{
            "type": "spawn_child_deals",
            "service_ids": [services["automation-delivery"], services["automations-support"]],
        }],
    )
    _partner, deal_id = _deal(services, stage="hc_booked")
    set_deal_stage(deal_id, "hc_presented")
    assert list_tasks(deal_id=deal_id) == []
    assert len(list_deals(parent_deal_id=deal_id)) == 2


def test_unknown_child_service_is_rejected_at_save_time(db):
    _catalog()
    automation, error = create_automation(
        "Bad children",
        "hc_presented",
        actions=[{"type": "spawn_child_deals", "service_slugs": ["does-not-exist"]}],
    )
    assert automation is None
    assert error == "unknown_service:does-not-exist"
    assert list_automations() == []


def test_webhook_fires_when_owner_is_the_default(db, monkeypatch):
    monkeypatch.setenv("DELEGATE_DEFAULT_OWNER", "willow")
    monkeypatch.setenv("TASK_WEBHOOK_URL", "http://willow.test/tasks")
    monkeypatch.setenv("TASK_WEBHOOK_TOKEN", "willow-secret")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers})

        class _Response:
            def raise_for_status(self):
                return None

        return _Response()

    monkeypatch.setattr("app.services.delegated_tasks.httpx.post", fake_post)
    services, _offer, _other = _catalog()
    _kickoff()
    _partner, deal_id = _deal(services)
    set_deal_stage(deal_id, "hc_paid")
    assert len(calls) == 1
    assert calls[0]["json"]["title"] == "Send HC kickoff"
    assert calls[0]["json"]["owner"] == "willow"
    assert calls[0]["headers"]["Authorization"] == "Bearer willow-secret"
    task = get_task(calls[0]["json"]["task_id"])
    assert task["webhook_notified_at"]
    assert task["webhook_last_error"] is None


def test_seed_is_idempotent_and_recreates_the_sevasek_pair(db):
    services, _offer, _other = _catalog()
    first = seed_sevasek_automations()
    assert first["created"] == [
        "HC paid: send kickoff",
        "HC presented: spawn delivery and support",
    ]
    second = seed_sevasek_automations()
    assert second["created"] == []
    assert second["existing"] == first["created"]
    assert len(list_automations()) == 2

    _partner, deal_id = _deal(services, stage="hc_booked")
    set_deal_stage(deal_id, "hc_paid")
    tasks = list_tasks(deal_id=deal_id)
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Send HC kickoff"
    assert tasks[0]["owner"] == "willow"
    assert tasks[0]["brief"] == "Paid Health Check. Send the kickoff."
    assert list_deals(parent_deal_id=deal_id) == []

    set_deal_stage(deal_id, "hc_presented")
    children = list_deals(parent_deal_id=deal_id)
    assert sorted(child["service_slug"] for child in children) == [
        "automation-delivery",
        "automations-support",
    ]
    presented = next(row for row in list_automations() if row["stage_key"] == "hc_presented")
    assert children[0]["source"] == automation_source(presented["id"])
    assert "it-support" not in [child["service_slug"] for child in children]


def test_seed_refuses_to_invent_catalog_rows(db):
    create_service("Health Check", "health-check")
    with pytest.raises(SystemExit, match="stage hc_paid"):
        seed_sevasek_automations()
    assert list_automations() == []
    assert get_service_by_slug("automation-delivery") is None


def test_admin_create_rename_disable_delete(logged_in_client, db):
    services, _offer, _other = _catalog()
    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    created = logged_in_client.post("/automations/new", data={
        "name": "Kickoff",
        "stage_key": "hc_paid",
        "service_id": str(services["health-check"]),
        "task_title": "Send HC kickoff",
        "task_owner": "willow",
        "task_brief": "Paid Health Check. Send the kickoff.",
        "enabled": "on",
        "csrf_token": token,
    }, follow_redirects=False)
    assert created.status_code == 303
    page = logged_in_client.get("/automations")
    assert page.status_code == 200
    assert "Kickoff" in page.text
    assert "Send HC kickoff" in page.text
    automation = list_automations()[0]

    renamed = logged_in_client.post(f"/automations/{automation['id']}/edit", data={
        "name": "Kickoff renamed",
        "stage_key": "hc_paid",
        "service_id": str(services["health-check"]),
        "task_title": "Send HC kickoff",
        "task_owner": "willow",
        "task_brief": "Paid Health Check. Send the kickoff.",
        "enabled": "on",
        "csrf_token": token,
    }, follow_redirects=False)
    assert renamed.status_code == 303
    assert get_automation(automation["id"])["name"] == "Kickoff renamed"

    disabled = logged_in_client.post(f"/automations/{automation['id']}/enabled", data={
        "enabled": "0",
        "csrf_token": token,
    }, follow_redirects=False)
    assert disabled.status_code == 303
    assert get_automation(automation["id"])["enabled"] is False

    missing = logged_in_client.post("/automations/new", data={
        "name": "",
        "stage_key": "hc_paid",
        "task_title": "Nope",
        "task_owner": "willow",
        "csrf_token": token,
    })
    assert missing.status_code == 400
    assert "Name is required." in missing.text

    logged_in_client.post(f"/automations/{automation['id']}/delete", data={"csrf_token": token})
    assert list_automations() == []


def test_automations_page_requires_login(client, db):
    resp = client.get("/automations", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_mcp_lists_creates_updates_and_triggers(client, db, monkeypatch):
    from tests.test_mcp_tools import _enable, call

    _enable(monkeypatch)
    services, _offer, _other = _catalog()
    listed, is_error = call(client, "list_stage_automations")
    assert is_error is False
    assert listed["automations"] == []

    created, is_error = call(client, "create_stage_automation", {
        "name": "Kickoff",
        "stage_key": "hc_paid",
        "service_slug": "health-check",
        "actions": [{
            "type": "delegated_task",
            "owner": "willow",
            "title": "Send HC kickoff",
            "brief": "Paid Health Check. Send the kickoff.",
        }],
    })
    assert is_error is False, created
    assert created["ok"] is True
    automation_id = created["automation"]["id"]

    renamed, _is_error = call(client, "update_stage_automation", {
        "automation_id": automation_id,
        "name": "Kickoff renamed",
        "enabled": False,
    })
    assert renamed["automation"]["name"] == "Kickoff renamed"
    assert renamed["automation"]["enabled"] is False

    _partner, deal_id = _deal(services)
    set_deal_stage(deal_id, "hc_paid")
    assert list_tasks(deal_id=deal_id) == []

    call(client, "update_stage_automation", {
        "automation_id": automation_id,
        "enabled": True,
    })
    set_deal_stage(deal_id, "new")
    set_deal_stage(deal_id, "hc_paid")
    assert len(list_tasks(deal_id=deal_id)) == 1

    duplicate, is_error = call(client, "create_stage_automation", {
        "name": "kickoff renamed",
        "stage_key": "hc_paid",
        "actions": [{
            "type": "delegated_task",
            "owner": "willow",
            "title": "Again",
        }],
    })
    assert is_error is True
    assert duplicate["ok"] is False
    assert duplicate["error"] == "name_taken"

    deleted, _is_error = call(client, "delete_stage_automation", {"automation_id": automation_id})
    assert deleted["status"] == "deleted"
    listed, _is_error = call(client, "list_stage_automations")
    assert listed["count"] == 0


def test_instructions_do_not_hardcode_the_health_check_ladder(client, db, monkeypatch):
    from tests.test_mcp import _enable, rpc

    _enable(monkeypatch)
    instructions = rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
    }).json()["result"]["instructions"]
    assert "list_stage_automations" in instructions
    assert "Send HC kickoff" not in instructions
    assert "hc-spawn:" not in instructions
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    stage = next(tool for tool in tools if tool["name"] == "set_deal_stage")
    assert "NURTURE_WEBHOOK_URL" in stage["description"]
    assert "DEAL_WON_WEBHOOK_URL" in stage["description"]
    assert "list_stage_automations" in stage["description"]
    assert "IT Support is not auto-spawned" not in stage["description"]
