"""Campaign records, the metrics adapter, the pages, and the MCP tools."""
from datetime import date

from app.database import get_db, get_user_version, init_db, migrate_001, migrate_002
from app.database import migrate_003, migrate_004, migrate_005, migrate_006, migrate_007
from app.database import migrate_008, migrate_009, migrate_010, migrate_011
from app.mcp.tools import TOOLS
from app.services.activities import log_activity
from app.services.auth import generate_csrf_token
from app.services.call_queue import list_todays_calls
from app.services.campaign_metrics_adapter import classify_deal, metrics_for_campaign
from app.services.campaigns import (
    campaign_links_for_tags,
    create_campaign,
    get_campaign,
    list_campaign_deals,
    update_campaign,
)
from app.services.catalog import create_service, get_service_by_slug
from app.services.deal_tags import apply_deal_tags
from app.services.deals import create_deal, mark_lost, set_deal_stage
from app.services.lead_records import create_lead
from app.services.partners import create_partner


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def _enable(monkeypatch, key="test-mcp-key"):
    monkeypatch.setenv("CRM_MCP_API_KEY", key)


def _mcp(client, name, arguments=None, key="test-mcp-key"):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }, headers={
        "X-API-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return result["structuredContent"], result["isError"]


def _consulting_id():
    existing = get_service_by_slug("consulting")
    if existing:
        return existing["id"]
    return create_service("Consulting", "consulting")


def _deal(tags=None, *, phone="0400 111 222", stage="new", email=None, value=800):
    partner_id = create_partner(
        "Jane Doe", email=email or f"{phone.replace(' ', '')}@acme.example", phone=phone,
    )
    service_id = _consulting_id()
    deal_id = create_deal(partner_id, service_id, value_estimate=value)
    if stage != "new":
        set_deal_stage(deal_id, stage)
    if tags:
        apply_deal_tags(deal_id, replace=tags)
    return deal_id


def test_campaigns_page_requires_login(client):
    anonymous = client.get("/crm/campaigns", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/auth/login"


def test_campaigns_nav_marks_the_current_page(logged_in_client):
    page = logged_in_client.get("/crm/campaigns")
    assert page.status_code == 200
    assert 'class="crm-theme"' in page.text
    assert 'href="/crm/campaigns" aria-current="page">Campaigns</a>' in page.text
    assert "No campaigns yet." in page.text


def test_create_edit_and_link_existing_tag(logged_in_client, db):
    deal_id = _deal(tags=["campaign:spring-2026", "referral"])
    resp = logged_in_client.post("/crm/campaigns/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Spring outreach",
        "slug": "",
        "audience": "Independent clinics",
        "service_id": "",
        "offer_id": "",
        "angle": "One angle for every lead",
        "opener": "Hello, this is the shared opener.",
        "channel": "phone",
        "status": "active",
        "start_date": "2026-09-01",
        "deadline": "2026-12-01",
        "target_leads": "4",
        "target_contacts": "",
        "target_conversations": "",
        "target_advanced": "",
        "target_won": "",
        "target_won_value": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    campaign = get_campaign(slug="spring-outreach")
    assert campaign["tag"] == "campaign:spring-outreach"
    assert resp.headers["location"] == f"/crm/campaigns/{campaign['id']}"

    # A pre-existing tag links as soon as the slug matches. This one does not.
    detail = logged_in_client.get(f"/crm/campaigns/{campaign['id']}")
    assert detail.status_code == 200
    assert "One angle for every lead" in detail.text
    assert "Hello, this is the shared opener." in detail.text
    assert "No deals carry" in detail.text
    assert 'href="/actions/calls?campaign=spring-outreach&amp;include_new=1"' in detail.text or (
        'href="/actions/calls?campaign=spring-outreach&include_new=1"' in detail.text
    )
    assert "Period breakdown" in detail.text
    assert "(deadline - start_date).days + 1" in detail.text

    renamed, error = update_campaign(campaign["id"], slug="spring-2026")
    assert error is None
    assert renamed["slug"] == "spring-2026"
    deals = list_campaign_deals("spring-2026")
    assert [deal["id"] for deal in deals] == [deal_id]
    page = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert f'href="/crm/campaigns/{campaign["id"]}"' in page.text
    assert "Spring outreach" in page.text

    listed = logged_in_client.get("/crm/campaigns")
    assert "Spring outreach" in listed.text
    assert "25%" in listed.text  # 1 deal / target 4
    assert "no closed deals" in listed.text


def test_slug_rename_rewrites_tags_and_keeps_other_tags(db):
    deal_id = _deal(tags=["campaign:old-slug", "referral"])
    campaign, error = create_campaign("Old", slug="old-slug")
    assert error is None
    updated, error = update_campaign(campaign["id"], slug="new-slug")
    assert error is None
    assert updated["tag"] == "campaign:new-slug"
    with get_db() as conn:
        tags = [row["tag"] for row in conn.execute(
            "SELECT tag FROM deal_tags WHERE deal_id = ? ORDER BY tag", (deal_id,)
        )]
    assert tags == ["campaign:new-slug", "referral"]
    assert get_campaign(slug="old-slug") is None


def test_form_rejects_a_deadline_before_the_start(logged_in_client, db):
    resp = logged_in_client.post("/crm/campaigns/new", data={
        "csrf_token": _csrf(logged_in_client),
        "name": "Backwards",
        "slug": "backwards",
        "audience": "", "service_id": "", "offer_id": "",
        "angle": "", "opener": "", "channel": "",
        "status": "draft",
        "start_date": "2026-12-01",
        "deadline": "2026-09-01",
        "target_leads": "", "target_contacts": "", "target_conversations": "",
        "target_advanced": "", "target_won": "", "target_won_value": "",
    })
    assert resp.status_code == 400
    assert "Deadline must be on or after the start date." in resp.text
    assert get_campaign(slug="backwards") is None


def test_outcome_uses_the_shared_helper(db):
    from app.services.deal_outcome import deal_outcome
    from app.services.campaign_metrics_adapter import _load_shared_outcome

    assert _load_shared_outcome() is deal_outcome
    assert classify_deal({"active": 1, "stage": "qualified"}) == "open"
    assert classify_deal({"active": 1, "stage": "won"}) == "won"
    assert classify_deal({
        "active": 0,
        "stage": "qualified",
        "lost_reason": {"name": "Not a fit", "is_disqualification": 0},
    }) == "lost"
    assert classify_deal({
        "active": 0,
        "stage": "qualified",
        "lost_reason": {"name": "Disqualified", "is_disqualification": 1},
    }) == "disqualified"


def test_real_metrics_exclude_disqualified_and_fill_rates(db):
    from app.services.campaign_metrics import compute_campaign_metrics
    from app.services.campaign_metrics_adapter import _load_compute

    function, engine_name = _load_compute()
    assert function is compute_campaign_metrics
    assert engine_name == "campaign_metrics"

    won_id = _deal(tags=["campaign:push"], phone="0400 111 001")
    set_deal_stage(won_id, "won")
    lost_id = _deal(tags=["campaign:push"], phone="0400 111 002")
    mark_lost(lost_id, lost_reason="Not a fit")
    dropped_id = _deal(tags=["campaign:push"], phone="0400 111 003", value=999)
    marked, error = mark_lost(dropped_id, lost_reason="Disqualified")
    assert error is None
    log_activity(None, "call", "Called once", deal_id=won_id)
    campaign, error = create_campaign(
        "Push", slug="push", status="active",
        start_date="2026-10-01", deadline="2026-10-31",
        target_leads=4,
    )
    assert error is None
    metrics = metrics_for_campaign(campaign, period="month", today=date(2026, 10, 24))
    assert metrics["engine"] == "campaign_metrics"
    assert metrics["results"]["deals"]["value"] == 3
    assert metrics["results"]["leads"]["value"] == 2
    assert metrics["results"]["leads"]["inputs"]["disqualified"] == 1
    assert metrics["results"]["contacts"]["value"] == 1
    assert metrics["results"]["disqualified"]["value"] == 1
    assert metrics["results"]["disqualified_value"]["value"] == 999
    assert metrics["win_loss"]["ratio"]["value"] == "1 : 1"
    assert metrics["win_loss"]["win_rate"]["value"] == 50
    assert metrics["win_loss"]["disqualified"]["value"] == 1
    assert metrics["win_loss"]["lost_value"]["value"] == 800
    leads = metrics["targets"]["leads"]
    assert leads["actual"]["value"] == 2
    assert leads["percent_complete"]["value"] == 50
    assert leads["required_rate"]["week"]["value"] is not None
    assert leads["required_rate"]["week"]["formula"]
    assert leads["projection"]["value"] is not None
    assert metrics["window"]["days_total"]["value"] == 31
    assert metrics["window"]["days_total"]["formula"] == "(deadline - start_date).days + 1"
    assert metrics["window"]["deadline_passed"] is False
    assert metrics["periods"]["grouping"] == "month"
    assert metrics["periods"]["rows"]
    assert metrics["pacing_status"] in ("ahead", "on_track", "behind")

    open_only, _ = create_campaign("Quiet", slug="quiet")
    quiet = metrics_for_campaign(open_only, today=date(2026, 10, 24))
    assert quiet["win_loss"]["ratio"]["value"] is None
    assert quiet["win_loss"]["ratio"]["note"] == "no closed deals"
    assert quiet["win_loss"]["win_rate"]["value"] is None
    assert quiet["periods"] is None


def test_page_and_mcp_share_the_metrics_function(logged_in_client, db, monkeypatch):
    _enable(monkeypatch)
    _deal(tags=["campaign:shared"])
    campaign, _ = create_campaign("Shared", slug="shared", target_leads=10)

    def figure(value, formula, inputs=None, note=None):
        return {"value": value, "formula": formula, "inputs": inputs or {}, "note": note}

    def spy(campaign_payload, deals, events, today, tz, period=None):
        assert campaign_payload["slug"] == "shared"
        assert campaign_payload["targets"]["leads"] == 10
        assert deals and deals[0]["outcome"] in ("open", "won", "lost", "disqualified")
        assert "value" in deals[0]
        assert hasattr(today, "isoformat")
        assert getattr(tz, "key", None)
        return {
            "results": {
                "deals": figure(7, "seven deals"),
                "leads": figure(7, "leads"),
                "by_stage": figure({}, "by stage"),
                "contacted": figure(1, "contacted"),
                "not_touched": figure(6, "not touched"),
                "contacts": figure(1, "contacts"),
                "conversations": figure(0, "conversations"),
                "advanced": figure(0, "advanced"),
                "open": figure(3, "open"),
                "won": figure(3, "won"),
                "lost": figure(1, "lost"),
                "disqualified": figure(0, "disqualified"),
                "won_value": figure(0, "won value"),
                "lost_value": figure(0, "lost value"),
                "disqualified_value": figure(0, "disqualified value"),
            },
            "win_loss": {
                "won": figure(3, "won"),
                "lost": figure(1, "lost"),
                "open": figure(3, "open"),
                "disqualified": figure(0, "disqualified"),
                "ratio": figure("3 : 1", "won 3 lost 1"),
                "win_rate": figure(75, "win rate"),
                "disqualification_rate": figure(0, "dq rate"),
                "lost_by_reason": figure([], "reasons"),
                "won_value": figure(0, "won value"),
                "lost_value": figure(0, "lost value"),
            },
            "window": {
                "today": "2026-10-24",
                "timezone": "UTC",
                "state": "in_progress",
                "deadline_passed": False,
                "not_started": False,
                "days_total": figure(31, "window length"),
                "days_elapsed": figure(10, "elapsed"),
                "days_remaining": figure(21, "remaining"),
                "percent_elapsed": figure(32, "elapsed percent"),
            },
            "targets": {
                "leads": {
                    "name": "leads",
                    "target": figure(10, "target"),
                    "actual": figure(7, "actual"),
                    "remaining": figure(3, "remaining"),
                    "percent_complete": figure(70, "percent complete"),
                    "required_rate": {
                        "week": figure(1, "four tens"),
                        "month": figure(4, "per month"),
                        "year": figure(52, "per year"),
                    },
                    "actual_rate": {
                        "week": figure(2, "actual week"),
                        "month": figure(8, "actual month"),
                        "year": figure(90, "actual year"),
                    },
                    "status": "ahead",
                    "status_inputs": {"percent_complete": 70, "percent_elapsed": 32},
                    "projection": figure(12, "projection"),
                },
            },
            "pacing_status": "ahead",
            "periods": None if not period else {
                "grouping": period,
                "rows": [{
                    "key": "2026-10",
                    "start": "2026-10-01",
                    "end": "2026-10-31",
                    "actuals": {"leads": figure(7, "created in October")},
                    "required": {"leads": figure(4, "required")},
                    "cumulative_actual": {"leads": figure(7, "cumulative")},
                    "cumulative_target": {"leads": figure(4, "cumulative target")},
                    "won": figure(3, "period won"),
                    "lost": figure(1, "period lost"),
                    "disqualified": figure(0, "period dq"),
                    "win_rate": figure(75, "period win rate"),
                }],
                "undated": {},
            },
        }

    monkeypatch.setattr(
        "app.services.campaign_metrics_adapter._load_compute",
        lambda: (spy, "campaign_metrics"),
    )
    page = logged_in_client.get(f"/crm/campaigns/{campaign['id']}?period=month")
    assert page.status_code == 200
    assert "seven deals" in page.text
    assert "four tens" in page.text
    assert "created in October" in page.text
    assert "2026-10" in page.text
    assert "Ahead" in page.text
    payload, is_error = _mcp(logged_in_client, "get_campaign", {
        "slug": "shared", "period": "month",
    })
    assert is_error is False
    assert payload["metrics"]["engine"] == "campaign_metrics"
    assert payload["metrics"]["results"]["deals"]["value"] == 7
    assert payload["metrics"]["targets"]["leads"]["required_rate"]["week"]["formula"] == "four tens"
    assert payload["deals"][0]["id"]
    listed, _ = _mcp(logged_in_client, "list_campaigns", {})
    row = next(item for item in listed["campaigns"] if item["slug"] == "shared")
    assert row["win_loss"]["label"] == "3 : 1"
    assert row["pace"] == "ahead"
    assert "3 : 1" in page.text


def test_mcp_create_update_and_unknown_period(client, db, monkeypatch):
    _enable(monkeypatch)
    names = [tool["name"] for tool in TOOLS]
    for name in ("list_campaigns", "get_campaign", "create_campaign", "update_campaign"):
        assert name in names
    created, is_error = _mcp(client, "create_campaign", {
        "name": "Spring Outreach",
        "channel": "phone",
        "target_leads": 5,
    })
    assert is_error is False
    assert created["campaign"]["slug"] == "spring-outreach"
    updated, is_error = _mcp(client, "update_campaign", {
        "slug": "spring-outreach",
        "new_slug": "spring-2026",
        "audience": "Shops",
    })
    assert is_error is False
    assert updated["campaign"]["slug"] == "spring-2026"
    assert updated["campaign"]["audience"] == "Shops"
    bad, is_error = _mcp(client, "get_campaign", {"slug": "spring-2026", "period": "day"})
    assert is_error is True
    assert bad["ok"] is False
    assert bad["error"] == "invalid_period"
    missing, _ = _mcp(client, "get_campaign", {})
    assert missing["error"] == "id_required"


def test_call_queue_filters_by_campaign_and_the_page_starts_there(logged_in_client, db, monkeypatch):
    _enable(monkeypatch)
    inside = _deal(tags=["campaign:queue"], phone="0400 200 001", stage="qualified")
    outside = _deal(tags=["referral"], phone="0400 200 002", stage="qualified")
    early = _deal(tags=["campaign:queue"], phone="0400 200 003", stage="new")
    campaign, _ = create_campaign("Queue", slug="queue", status="active")
    assert [row["id"] for row in list_todays_calls(campaign_slug="queue")] == [inside]
    assert outside not in [row["id"] for row in list_todays_calls(campaign_slug="queue")]
    both = [row["id"] for row in list_todays_calls(campaign_slug="queue", include_new=True)]
    assert inside in both and early in both and outside not in both

    page = logged_in_client.get("/actions/calls?campaign=queue&include_new=1")
    assert page.status_code == 200
    assert "Queue" in page.text
    assert f"/actions/deals/{inside}/call" in page.text
    assert f"/actions/deals/{outside}/call" not in page.text
    missing = logged_in_client.get("/actions/calls?campaign=missing")
    assert "No campaign with that slug." in missing.text

    payload, is_error = _mcp(logged_in_client, "get_call_queue", {"campaign": "queue"})
    assert is_error is False
    assert payload["campaign"] == "queue"
    assert [row["deal"]["id"] for row in payload["calls"]] == [inside]
    unknown, _ = _mcp(logged_in_client, "get_call_queue", {"campaign_id": 999})
    assert unknown["error"] == "not_found"
    assert campaign["id"]


def test_lead_page_links_its_campaign(logged_in_client, db):
    _consulting_id()
    lead, error = create_lead(
        company_name="North Cafe", phone="0400 300 001",
        service_slug="consulting", tags=["campaign:cafes"],
    )
    assert error is None
    campaign, _ = create_campaign("Cafes", slug="cafes")
    page = logged_in_client.get(f"/crm/leads/{lead['id']}")
    assert f'href="/crm/campaigns/{campaign["id"]}"' in page.text
    assert "Cafes" in page.text
    detail = logged_in_client.get(f"/crm/campaigns/{campaign['id']}")
    assert "North Cafe" in detail.text
    assert f'href="/crm/leads/{lead["id"]}"' in detail.text


def test_two_campaigns_on_one_deal_and_a_tag_without_a_row(db):
    _deal(tags=["campaign:one", "campaign:two", "campaign:later"])
    create_campaign("One", slug="one")
    create_campaign("Two", slug="two")
    links = campaign_links_for_tags(["campaign:one", "referral", "campaign:later", "campaign:two"])
    assert [link["slug"] for link in links] == ["one", "later", "two"]
    assert links[0]["campaign"]["name"] == "One"
    assert links[1]["campaign"] is None
    assert links[2]["campaign"]["name"] == "Two"


def test_v11_database_backfills_campaign_rows(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    with get_db() as conn:
        migrate_001(conn)
        migrate_002(conn)
        migrate_003(conn)
        migrate_004(conn)
        migrate_005(conn)
        migrate_006(conn)
        migrate_007(conn)
        migrate_008(conn)
        migrate_009(conn)
        migrate_010(conn)
        migrate_011(conn)
        conn.execute("INSERT INTO partners (name) VALUES ('Acme')")
        conn.execute("INSERT INTO services (name, slug) VALUES ('Consulting', 'consulting')")
        conn.execute(
            "INSERT INTO deals (partner_id, service_id, stage) VALUES (1, 1, 'new')"
        )
        conn.execute(
            "INSERT INTO deal_tags (deal_id, tag) VALUES (1, 'campaign:spring-2026')"
        )
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
    init_db()
    with get_db() as conn:
        assert get_user_version(conn) == 13
        row = conn.execute(
            "SELECT name, status FROM campaigns WHERE slug = 'spring-2026'"
        ).fetchone()
        assert row["name"] == "Spring 2026"
        assert row["status"] == "active"
