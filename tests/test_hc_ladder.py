"""Health Check ladder. Off unless HC_LADDER_ENABLED is set."""
from app.services.catalog import create_service, get_service_by_slug
from app.services.deals import create_deal, get_deal, list_deals, set_deal_stage
from app.services.delegated_tasks import get_task, list_tasks
from app.services.hc_ladder import (
    CHILD_SERVICE_SLUGS,
    EXCLUDED_SERVICE_SLUG,
    KICKOFF_OWNER,
    KICKOFF_TITLE,
    ladder_enabled,
    spawn_source,
)
from app.services.offers import create_offer
from app.services.partners import create_partner
from app.services.pipeline_stages import create_stage


def _enable(monkeypatch, value="true"):
    monkeypatch.setenv("HC_LADDER_ENABLED", value)


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
    assert offer["is_default"]
    return services


def _hc_deal(services, stage="hc_booked"):
    partner_id = create_partner("North Church", email="office@north.example")
    deal_id = create_deal(
        partner_id,
        services["health-check"],
        stage=stage,
        source="referral",
        value_estimate=700,
    )
    return partner_id, deal_id


def test_ladder_is_off_by_default(db, monkeypatch):
    monkeypatch.delenv("HC_LADDER_ENABLED", raising=False)
    assert ladder_enabled() is False
    services = _catalog()
    _partner, deal_id = _hc_deal(services)
    assert set_deal_stage(deal_id, "hc_paid") is True
    assert list_tasks(deal_id=deal_id) == []
    assert list_deals(parent_deal_id=deal_id) == []


def test_false_string_stays_off(db, monkeypatch):
    _enable(monkeypatch, "false")
    assert ladder_enabled() is False


def test_hc_paid_creates_willow_kickoff_once_while_open(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    _partner, deal_id = _hc_deal(services)
    assert set_deal_stage(deal_id, "hc_paid") is True
    tasks = list_tasks(deal_id=deal_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["title"] == KICKOFF_TITLE
    assert task["owner"] == KICKOFF_OWNER
    assert task["status"] == "delegated"
    assert task["brief"] == "Paid Health Check. Send the kickoff."

    assert set_deal_stage(deal_id, "hc_in_delivery") is True
    assert set_deal_stage(deal_id, "hc_paid") is True
    assert len(list_tasks(deal_id=deal_id)) == 1
    assert list_deals(parent_deal_id=deal_id) == []


def test_completed_kickoff_is_not_reused(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    _partner, deal_id = _hc_deal(services)
    set_deal_stage(deal_id, "hc_paid")
    from app.services.delegated_tasks import complete_task
    complete_task(list_tasks(deal_id=deal_id)[0]["id"], result_notes="sent")
    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_paid")
    open_tasks = [t for t in list_tasks(deal_id=deal_id) if t["status"] != "done"]
    assert len(open_tasks) == 1
    assert len(list_tasks(deal_id=deal_id)) == 2


def test_hc_presented_spawns_delivery_and_support_without_prices(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    partner_id, deal_id = _hc_deal(services, stage="hc_in_delivery")
    assert set_deal_stage(deal_id, "hc_presented") is True
    children = list_deals(parent_deal_id=deal_id)
    slugs = sorted(child["service_slug"] for child in children)
    assert slugs == sorted(CHILD_SERVICE_SLUGS)
    assert EXCLUDED_SERVICE_SLUG not in slugs
    for child in children:
        assert child["partner_id"] == partner_id
        assert child["parent_deal_id"] == deal_id
        assert child["source"] == spawn_source(deal_id)
        assert child["stage"] == "new"
        assert child["offer_id"] is None
        assert child["value_estimate"] is None
    # Parent kept its own estimate; the default offer was not copied down.
    parent = get_deal(deal_id)
    assert parent["value_estimate"] == 700
    assert parent["offer_id"] is not None

    set_deal_stage(deal_id, "hc_in_delivery")
    set_deal_stage(deal_id, "hc_presented")
    assert len(list_deals(parent_deal_id=deal_id)) == 2


def test_closed_child_does_not_block_a_new_open_child(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    _partner, deal_id = _hc_deal(services, stage="hc_in_delivery")
    set_deal_stage(deal_id, "hc_presented")
    delivery = next(
        child for child in list_deals(parent_deal_id=deal_id)
        if child["service_slug"] == "automation-delivery"
    )
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


def test_missing_child_service_does_not_block_the_stage_or_invent_a_service(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    with db.get_db() as conn:
        conn.execute("DELETE FROM services WHERE slug = ?", ("automations-support",))
        conn.commit()
    _partner, deal_id = _hc_deal(services, stage="hc_in_delivery")
    assert set_deal_stage(deal_id, "hc_presented") is True
    assert get_deal(deal_id)["stage"] == "hc_presented"
    children = list_deals(parent_deal_id=deal_id)
    assert [child["service_slug"] for child in children] == ["automation-delivery"]
    assert get_service_by_slug("automations-support") is None
    assert get_service_by_slug("it-support") is not None


def test_other_services_do_not_trigger_the_ladder(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    partner_id = create_partner("GEO Lead", email="geo@example.com")
    deal_id = create_deal(partner_id, services["geo"], stage="new")
    assert set_deal_stage(deal_id, "hc_paid") is True
    assert set_deal_stage(deal_id, "hc_presented") is True
    assert list_tasks(deal_id=deal_id) == []
    assert list_deals(parent_deal_id=deal_id) == []


def test_jumping_to_presented_does_not_also_create_the_kickoff(db, monkeypatch):
    _enable(monkeypatch)
    services = _catalog()
    _partner, deal_id = _hc_deal(services, stage="hc_booked")
    set_deal_stage(deal_id, "hc_presented")
    assert list_tasks(deal_id=deal_id) == []
    assert len(list_deals(parent_deal_id=deal_id)) == 2


def test_kickoff_webhook_follows_delegate_default_owner(db, monkeypatch):
    _enable(monkeypatch)
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
    services = _catalog()
    _partner, deal_id = _hc_deal(services)
    set_deal_stage(deal_id, "hc_paid")
    assert len(calls) == 1
    assert calls[0]["json"]["title"] == KICKOFF_TITLE
    assert calls[0]["json"]["owner"] == KICKOFF_OWNER
    assert calls[0]["headers"]["Authorization"] == "Bearer willow-secret"
    task = get_task(calls[0]["json"]["task_id"])
    assert task["webhook_notified_at"]
    assert task["webhook_last_error"] is None


def test_mcp_instructions_mention_the_ladder_only_when_enabled(client, db, monkeypatch):
    from tests.test_mcp import _enable as enable_mcp
    from tests.test_mcp import rpc

    enable_mcp(monkeypatch)
    off = rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
    instructions = off.json()["result"]["instructions"]
    assert "Send HC kickoff" not in instructions
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    stage = next(tool for tool in tools if tool["name"] == "set_deal_stage")
    assert "Send HC kickoff" not in stage["description"]

    _enable(monkeypatch)
    on = rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
    assert "Send HC kickoff" in on.json()["result"]["instructions"]
    assert "hc-spawn:" in on.json()["result"]["instructions"]
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    stage = next(tool for tool in tools if tool["name"] == "set_deal_stage")
    assert "IT Support is not auto-spawned" in stage["description"]
    assert "NURTURE_WEBHOOK_URL" in stage["description"]
