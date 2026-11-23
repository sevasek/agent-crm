"""Tag definitions (`tags`): auto-create on every write path, the
Definitions -> Tags page (rename, merge, delete), the form datalist, and
the MCP tools."""
import re

from app.database import get_db
from app.services.auth import generate_csrf_token
from app.services.campaigns import create_campaign, update_campaign
from app.services.catalog import create_service
from app.services.deal_tags import (
    apply_deal_tags, define_tag, delete_tag, list_tag_definitions, rename_tag, tags_for_deal,
)
from app.services.deals import create_deal
from app.services.leads import ingest_lead
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


def _defs():
    return {t["tag"]: t for t in list_tag_definitions()}


def _deal(tags=(), email="jane@acme.example"):
    pid = create_partner("Jane Doe", email=email)
    sid = create_service("Enrolment", f"enrolment-{email.split('@')[0]}")
    return create_deal(pid, sid, tags=list(tags))


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


# ---------- auto-create ----------

def test_apply_deal_tags_defines_new_tags_and_keeps_existing_ones(db):
    define_tag("missing:bond", description="Bond not paid yet", color="yellow", set_color=True)
    deal_id = _deal()
    apply_deal_tags(deal_id, replace=["missing:bond", "clinic"], add=["referral"])
    defs = _defs()
    assert set(defs) == {"missing:bond", "clinic", "referral"}
    assert defs["missing:bond"]["description"] == "Bond not paid yet"
    assert defs["missing:bond"]["color"] == "yellow"
    assert defs["clinic"]["description"] == "" and defs["clinic"]["color"] is None


def test_create_deal_defines_tags(db):
    _deal(tags=["priority:p1"])
    assert "priority:p1" in _defs()


def test_deal_and_lead_forms_define_tags(logged_in_client, db):
    deal_id = _deal()
    resp = logged_in_client.post(f"/crm/deals/{deal_id}/edit", data={
        "tags": "missing:immunisation, clinic", "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    resp = logged_in_client.post("/crm/leads/new", data={
        "company_name": "Acme", "tags": "missing:date-of-birth",
        "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert {"missing:immunisation", "clinic", "missing:date-of-birth"} <= set(_defs())


def test_lead_ingest_defines_tags(client, db, monkeypatch):
    create_service("Consulting", "consulting")
    ingest_lead({"type": "opportunity", "name": "Jane", "email": "jane@acme.example",
                 "service_slug": "consulting", "tags": ["from-ingest"]})
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={"leads": [{
        "type": "opportunity", "name": "Bob", "email": "bob@acme.example",
        "service_slug": "consulting", "tags": ["from-api"],
    }]}, headers={"X-API-Key": "test-crm-api-key"})
    assert resp.status_code == 200
    assert {"from-ingest", "from-api"} <= set(_defs())


def test_mcp_writes_define_tags(client, db, monkeypatch):
    _enable(monkeypatch)
    first, second = _deal(), _deal(email="bob@acme.example")
    payload, _ = call(client, "update_deal", {"deal_id": first, "add_tags": ["mcp-update"]})
    assert payload["ok"] is True
    payload, _ = call(client, "bulk_update_deals", {"deal_ids": [first, second], "add_tags": ["mcp-bulk"]})
    assert payload["ok"] is True
    assert {"mcp-update", "mcp-bulk"} <= set(_defs())


def test_campaign_slug_rename_renames_the_definition(db):
    campaign, _ = create_campaign(name="Spring", slug="spring")
    deal_id = _deal(tags=["campaign:spring"])
    define_tag("campaign:spring", description="Spring push")
    update_campaign(campaign["id"], slug="spring-2026")
    assert tags_for_deal(deal_id) == ["campaign:spring-2026"]
    defs = _defs()
    assert "campaign:spring" not in defs
    assert defs["campaign:spring-2026"]["description"] == "Spring push"


# ---------- rename / merge / delete (service) ----------

def test_rename_keeps_definition_and_updates_deals(db):
    deal_id = _deal(tags=["missing:dob"])
    define_tag("missing:dob", description="DOB", color="yellow", set_color=True)
    assert rename_tag("missing:dob", "missing:date-of-birth") == (False, None)
    assert tags_for_deal(deal_id) == ["missing:date-of-birth"]
    defs = _defs()
    assert "missing:dob" not in defs
    assert defs["missing:date-of-birth"]["color"] == "yellow"


def test_rename_onto_existing_needs_merge_and_leaves_no_duplicates(db):
    both = _deal(tags=["missing:imms", "missing:immunisation"])
    typo_only = _deal(tags=["missing:imms"], email="bob@acme.example")
    define_tag("missing:immunisation", description="Immunisation record", color="yellow", set_color=True)
    assert rename_tag("missing:imms", "missing:immunisation") == (False, "exists")
    assert "missing:imms" in tags_for_deal(both)  # nothing changed
    assert rename_tag("missing:imms", "missing:immunisation", merge=True) == (True, None)
    assert tags_for_deal(both) == ["missing:immunisation"]
    assert tags_for_deal(typo_only) == ["missing:immunisation"]
    defs = _defs()
    assert "missing:imms" not in defs
    assert defs["missing:immunisation"]["description"] == "Immunisation record"
    assert defs["missing:immunisation"]["total"] == 2


def test_rename_rejects_unknown_and_invalid(db):
    assert rename_tag("nope", "other") == (False, "not_found")
    _deal(tags=["clinic"])
    assert rename_tag("clinic", "Not A Tag!") == (False, "invalid_tag")


def test_delete_removes_from_every_deal(db):
    a, b = _deal(tags=["missing:bond", "clinic"]), _deal(tags=["missing:bond"], email="bob@acme.example")
    assert delete_tag("missing:bond") == 2
    assert tags_for_deal(a) == ["clinic"] and tags_for_deal(b) == []
    assert "missing:bond" not in _defs()


def test_define_tag_validates_colour_and_slug(db):
    assert define_tag("Bad Tag", description="x")[1] == "invalid_tag"
    assert define_tag("ok", color="mauve", set_color=True)[1] == "invalid_color"
    assert define_tag("ok", color=["yellow"], set_color=True)[1] == "invalid_color"
    tag, error = define_tag("ok", color="violet", set_color=True)
    assert error is None and tag["color"] == "violet"
    tag, _ = define_tag("ok", description="Described")  # colour left alone
    assert tag == {"tag": "ok", "description": "Described", "color": "violet", "count": 0, "total": 0}


# ---------- Definitions -> Tags page ----------

def test_tags_page_lists_definitions(logged_in_client, db):
    _deal(tags=["missing:bond"])
    define_tag("missing:bond", description="Bond not paid yet", color="yellow", set_color=True)
    html = logged_in_client.get("/definitions/tags").text
    assert 'href="/definitions/tags" aria-current="page"' in html
    assert "missing:bond" in html and 'value="Bond not paid yet"' in html
    assert '<option value="yellow" selected>Yellow</option>' in html
    assert 'data-confirm="Delete missing:bond? It is removed from 1 deal."' in html


def test_page_create_edit_rename_merge_delete(logged_in_client, db):
    c = logged_in_client
    deal_id = _deal(tags=["missing:imms", "missing:immunisation"])
    post = lambda url, **data: c.post(url, data={**data, "csrf_token": _csrf(c)}, follow_redirects=False)

    assert post("/definitions/tags/save", tag="priority:p1", description="Call today", color="orange").status_code == 303
    assert _defs()["priority:p1"]["color"] == "orange"
    assert post("/definitions/tags/save", tag="priority:p1", description="Call first", color="").status_code == 303
    assert _defs()["priority:p1"]["color"] is None
    assert post("/definitions/tags/save", tag="priority:p1", color="mauve").status_code == 400

    # Rename onto an existing tag: confirm panel, nothing changed yet.
    resp = post("/definitions/tags/rename", tag="missing:imms", new_tag="missing:immunisation")
    assert resp.status_code == 200
    assert "Merge into missing:immunisation" in resp.text
    assert "missing:imms" in tags_for_deal(deal_id)
    resp = post("/definitions/tags/rename", tag="missing:imms", new_tag="missing:immunisation", merge="1")
    assert resp.status_code == 303
    assert tags_for_deal(deal_id) == ["missing:immunisation"]

    assert post("/definitions/tags/rename", tag="priority:p1", new_tag="priority:high").status_code == 303
    assert "priority:high" in _defs()

    assert post("/definitions/tags/delete", tag="missing:immunisation").status_code == 303
    assert tags_for_deal(deal_id) == []


def test_tag_posts_require_csrf(logged_in_client, db):
    deal_id = _deal(tags=["clinic"])
    for url, data in (
        ("/definitions/tags/save", {"tag": "new-tag"}),
        ("/definitions/tags/rename", {"tag": "clinic", "new_tag": "clinics"}),
        ("/definitions/tags/delete", {"tag": "clinic"}),
    ):
        assert logged_in_client.post(url, data=data).status_code == 422, url
        resp = logged_in_client.post(url, data={**data, "csrf_token": "bad"}, follow_redirects=False)
        assert resp.status_code == 303, url
    assert tags_for_deal(deal_id) == ["clinic"]
    assert set(_defs()) == {"clinic"}


def test_tag_pages_require_login(client, db):
    deal_id = _deal(tags=["clinic"])
    assert client.get("/definitions/tags", follow_redirects=False).headers["location"].startswith("/auth/login")
    for url in ("/definitions/tags/save", "/definitions/tags/rename", "/definitions/tags/delete"):
        resp = client.post(url, data={"tag": "clinic", "new_tag": "x", "csrf_token": "x"}, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/login"), url
    assert tags_for_deal(deal_id) == ["clinic"]


def test_deal_and_lead_forms_have_tag_datalist(logged_in_client, db):
    deal_id = _deal()
    define_tag("missing:bond", description="Bond not paid yet")
    for url in (f"/crm/deals/{deal_id}/edit", "/crm/leads/new"):
        html = logged_in_client.get(url).text
        assert re.search(r'<input type="text" id="tags" name="tags" list="tag-suggestions" data-tag-suggest', html), url
        assert '<option value="missing:bond" data-tag="missing:bond">Bond not paid yet</option>' in html, url


# ---------- MCP ----------

def test_mcp_list_tags_and_define_tag(client, db, monkeypatch):
    _enable(monkeypatch)
    _deal(tags=["clinic"])
    payload, _ = call(client, "define_tag", {"tag": "missing:bond", "description": "Bond not paid yet", "color": "yellow"})
    assert payload["ok"] is True
    assert payload["tag"] == {"tag": "missing:bond", "description": "Bond not paid yet", "color": "yellow", "count": 0}
    payload, _ = call(client, "define_tag", {"tag": "missing:bond", "color": None})
    assert payload["tag"]["color"] is None and payload["tag"]["description"] == "Bond not paid yet"
    bad, _ = call(client, "define_tag", {"tag": "missing:bond", "color": "mauve"})
    assert bad["ok"] is False and bad["error"] == "invalid_color"
    bad, _ = call(client, "define_tag", {"tag": "Bad Tag"})
    assert bad["ok"] is False and bad["error"] == "invalid_tag"
    listed, _ = call(client, "list_tags")
    assert listed["tags"] == [
        {"tag": "clinic", "description": "", "color": None, "count": 1},
        {"tag": "missing:bond", "description": "Bond not paid yet", "color": None, "count": 0},
    ]
