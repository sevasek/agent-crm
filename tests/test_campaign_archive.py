"""Archiving campaigns: hidden from lists, optional close of untouched records."""
from app.services.activities import log_activity
from app.services.auth import generate_csrf_token
from app.services.campaigns import (
    archive_campaign, create_campaign, get_campaign, list_campaigns, unarchive_campaign,
    untouched_open_deals,
)
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal
from app.services.partners import create_partner

TAG = "campaign:spring"


def _campaign_with_deals():
    campaign, err = create_campaign("Spring push", slug="spring", status="active")
    assert err is None
    sid = create_service("Consulting", "consulting")
    untouched = create_deal(create_partner("Quiet Church"), sid, tags=[TAG])
    worked = create_deal(create_partner("Busy Church"), sid, tags=[TAG])
    log_activity(get_deal(worked)["partner_id"], "call", "Spoke to the office", deal_id=worked)
    return campaign, untouched, worked


def _mcp(client, name, arguments):
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers={"X-API-Key": "test-mcp-key", "Accept": "application/json",
                "Content-Type": "application/json"})
    result = resp.json()["result"]
    return result["structuredContent"], result["isError"]


def test_untouched_ignores_records_with_human_activity(db):
    campaign, untouched, worked = _campaign_with_deals()
    assert [d["id"] for d in untouched_open_deals("spring")] == [untouched]


def test_archive_hides_campaign_and_keeps_records_open_by_default(db):
    campaign, untouched, worked = _campaign_with_deals()
    archived, closed, err = archive_campaign(campaign["id"])
    assert err is None and closed == []
    assert archived["status"] == "archived"
    assert campaign["id"] not in [c["id"] for c in list_campaigns()]
    assert campaign["id"] in [c["id"] for c in list_campaigns(status="archived")]
    assert get_deal(untouched)["active"] and get_deal(worked)["active"]


def test_archive_close_untouched_marks_only_untouched_lost(db):
    campaign, untouched, worked = _campaign_with_deals()
    _archived, closed, err = archive_campaign(campaign["id"], close_untouched=True)
    assert err is None and closed == [untouched]
    assert not get_deal(untouched)["active"]
    assert get_deal(worked)["active"]
    # Running it again reuses the reason instead of failing on a duplicate name.
    _archived, closed, err = archive_campaign(campaign["id"], close_untouched=True)
    assert err is None and closed == []


def test_unarchive_returns_to_paused(db):
    campaign, *_ = _campaign_with_deals()
    archive_campaign(campaign["id"])
    restored, err = unarchive_campaign(campaign["id"])
    assert err is None and restored["status"] == "paused"


def test_campaign_pages_archive_flow(logged_in_client):
    campaign, untouched, worked = _campaign_with_deals()
    page = logged_in_client.get(f"/crm/campaigns/{campaign['id']}")
    assert "Also mark the 1 open record with no updates as lost" in page.text
    assert 'data-confirm="Archive this campaign?"' in page.text
    csrf = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post(
        f"/crm/campaigns/{campaign['id']}/archive",
        data={"csrf_token": csrf, "close_untouched": "1"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert get_campaign(campaign["id"])["status"] == "archived"
    assert not get_deal(untouched)["active"] and get_deal(worked)["active"]

    listing = logged_in_client.get("/crm/campaigns")
    assert "Spring push" not in listing.text
    assert "Show archived (1)" in listing.text
    assert "Spring push" in logged_in_client.get("/crm/campaigns?archived=1").text
    archived_page = logged_in_client.get(f"/crm/campaigns/{campaign['id']}").text
    assert "This campaign is archived." in archived_page
    assert "Start calls" not in archived_page

    logged_in_client.post(f"/crm/campaigns/{campaign['id']}/unarchive", data={"csrf_token": csrf})
    assert get_campaign(campaign["id"])["status"] == "paused"


def test_archive_rejects_bad_csrf(logged_in_client):
    campaign, *_ = _campaign_with_deals()
    logged_in_client.post(f"/crm/campaigns/{campaign['id']}/archive", data={"csrf_token": "nope"})
    assert get_campaign(campaign["id"])["status"] == "active"


def test_mcp_archive_and_list(logged_in_client, monkeypatch):
    monkeypatch.setenv("CRM_MCP_API_KEY", "test-mcp-key")
    campaign, untouched, worked = _campaign_with_deals()
    payload, is_error = _mcp(logged_in_client, "archive_campaign",
                             {"slug": "spring", "close_untouched": True})
    assert not is_error
    assert payload["campaign"]["status"] == "archived"
    assert payload["closed_deal_ids"] == [untouched]
    listed, _ = _mcp(logged_in_client, "list_campaigns", {})
    assert listed["count"] == 0
    listed, _ = _mcp(logged_in_client, "list_campaigns", {"include_archived": True})
    assert [c["slug"] for c in listed["campaigns"]] == ["spring"]
    payload, _ = _mcp(logged_in_client, "archive_campaign", {"slug": "spring", "unarchive": True})
    assert payload["campaign"]["status"] == "paused"
