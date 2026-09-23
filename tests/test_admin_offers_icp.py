from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, set_deal_stage
from app.services import offers, icp


def _qualified_deal(db, phone="0400 111 222"):
    pid = create_partner("Jane Doe", email="jane@x.example", phone=phone, industry="Childcare", team_size=25)
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral")
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    return pid, sid, deal_id


# ==================== Offers admin ====================

def test_offers_settings_requires_login(client, db):
    resp = client.get("/offers", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_create_offer_via_admin_form(logged_in_client, db):
    resp = logged_in_client.post("/offers/new", data={
        "name": "Starter", "pitch": "A free review", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert any(o["name"] == "Starter" for o in offers.list_offers())


def test_create_offer_missing_name_shows_error(logged_in_client, db):
    resp = logged_in_client.post("/offers/new", data={
        "name": "", "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert "Name is required" in resp.text


def test_create_offer_unchecked_active_is_inactive(logged_in_client, db):
    # Regression: FastAPI's Form("on") default meant an omitted checkbox
    # (unchecked, not just missing from a raw POST) still created an active
    # offer — there was no way to add an inactive one from this form.
    resp = logged_in_client.post("/offers/new", data={
        "name": "Retired pitch", "csrf_token": generate_csrf_token(),
        # "active" omitted entirely, as an unchecked checkbox would send
    }, follow_redirects=False)
    assert resp.status_code == 303
    offer = next(o for o in offers.list_offers() if o["name"] == "Retired pitch")
    assert offer["active"] == 0


def test_edit_offer_via_admin_form(logged_in_client, db):
    offer, _ = offers.create_offer("Starter")
    resp = logged_in_client.post(f"/offers/{offer['id']}/edit", data={
        "name": "Starter Plus", "pitch": "New pitch", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert offers.get_offer(offer["id"])["name"] == "Starter Plus"


def test_delete_offer_in_use_shows_error(logged_in_client, db):
    offer, _ = offers.create_offer("Starter")
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid, offer_id=offer["id"])
    resp = logged_in_client.post(f"/offers/{offer['id']}/delete", data={
        "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert offers.get_offer(offer["id"]) is not None


def test_delete_unused_offer(logged_in_client, db):
    offer, _ = offers.create_offer("Starter")
    resp = logged_in_client.post(f"/offers/{offer['id']}/delete", data={
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert offers.get_offer(offer["id"]) is None


# ==================== ICP admin ====================

def test_icp_settings_requires_login(client, db):
    resp = client.get("/icp", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_create_icp_criterion_via_admin_form(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "label": "Enterprise-sized", "field": "team_size", "operator": "gt", "value": "10",
        "weight": "5", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    criteria = icp.list_criteria()
    assert len(criteria) == 1
    assert criteria[0]["weight"] == 5


def test_create_icp_criterion_invalid_value_shows_error(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "label": "", "field": "team_size", "operator": "gt", "value": "not-a-number",
        "weight": "5", "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert icp.list_criteria() == []


def test_create_icp_criterion_bad_weight_does_not_500(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "label": "", "field": "team_size", "operator": "gt", "value": "10",
        "weight": "not-a-number", "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert "Weight must be a whole number" in resp.text
    assert icp.list_criteria() == []


def test_create_icp_criterion_out_of_range_weight_does_not_500(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "label": "", "field": "team_size", "operator": "gt", "value": "10",
        "weight": str(2 ** 63), "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert icp.list_criteria() == []


def test_create_icp_criterion_unchecked_active_is_inactive(logged_in_client, db):
    resp = logged_in_client.post("/icp/new", data={
        "label": "", "field": "team_size", "operator": "gt", "value": "10",
        "weight": "5", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert icp.list_criteria()[0]["active"] == 0


def test_create_icp_criterion_rejects_infinite_value(logged_in_client, db):
    # Python's float() accepts the literal string "Infinity"/"inf" directly
    # (unlike JSON, no raw-bytes trick needed to exercise this).
    resp = logged_in_client.post("/icp/new", data={
        "label": "", "field": "team_size", "operator": "gt", "value": "Infinity",
        "weight": "5", "csrf_token": generate_csrf_token(),
    })
    assert resp.status_code == 400
    assert icp.list_criteria() == []


def test_edit_icp_criterion(logged_in_client, db):
    c, _ = icp.create_criterion("team_size", "gt", "10", weight=5)
    resp = logged_in_client.post(f"/icp/{c['id']}/edit", data={
        "label": "Big teams", "field": "team_size", "operator": "gt", "value": "10",
        "weight": "8", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert icp.get_criterion(c["id"])["weight"] == 8


def test_delete_icp_criterion(logged_in_client, db):
    c, _ = icp.create_criterion("team_size", "gt", "10")
    resp = logged_in_client.post(f"/icp/{c['id']}/delete", data={
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert icp.get_criterion(c["id"]) is None


# ==================== Call view integration ====================

def test_call_view_shows_fit_score(logged_in_client, db):
    icp.create_criterion("team_size", "gt", "10", weight=5, label="Big team")
    icp.create_criterion("industry", "fuzzy_match", "childcare", weight=3, label="Childcare")
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    assert "Fit 8/8" in resp.text
    assert "Big team" in resp.text
    assert "Childcare" in resp.text


def test_call_view_no_fit_section_without_criteria(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert "Fit " not in resp.text


def test_call_view_shows_offer_pitch(logged_in_client, db):
    offer, _ = offers.create_offer(
        "Starter", pitch="A free 30-minute review", proof_point="Saved 10 hrs/week",
        price_anchor="$500/mo", is_default=True,
    )
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert "A free 30-minute review" in resp.text
    assert "Saved 10 hrs/week" in resp.text
    assert "$500/mo" in resp.text


def test_set_deal_offer(logged_in_client, db):
    offer1, _ = offers.create_offer("Starter")
    offer2, _ = offers.create_offer("Premium")
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.post(f"/deals/{deal_id}/offer", data={
        "offer_id": str(offer2["id"]), "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/deals/{deal_id}/call"
    from app.services.deals import get_deal
    assert get_deal(deal_id)["offer_id"] == offer2["id"]


def test_set_deal_offer_garbage_value_leaves_existing_offer(logged_in_client, db):
    # Garbage is not the same as the select's "— none —" option: a
    # tampered/unparseable value should not silently wipe an assignment.
    offer, _ = offers.create_offer("Starter")
    pid, _, deal_id = _qualified_deal(db)
    from app.services.deals import update_deal_fields, get_deal
    update_deal_fields(deal_id, offer_id=offer["id"])

    resp = logged_in_client.post(f"/deals/{deal_id}/offer", data={
        "offer_id": "not-a-number", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(deal_id)["offer_id"] == offer["id"]


def test_set_deal_offer_out_of_range_value_leaves_existing_offer(logged_in_client, db):
    offer, _ = offers.create_offer("Starter")
    pid, _, deal_id = _qualified_deal(db)
    from app.services.deals import update_deal_fields, get_deal
    update_deal_fields(deal_id, offer_id=offer["id"])

    resp = logged_in_client.post(f"/deals/{deal_id}/offer", data={
        "offer_id": str(2 ** 63), "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(deal_id)["offer_id"] == offer["id"]


def test_set_deal_offer_explicit_none_clears_offer(logged_in_client, db):
    offer, _ = offers.create_offer("Starter")
    pid, _, deal_id = _qualified_deal(db)
    from app.services.deals import update_deal_fields, get_deal
    update_deal_fields(deal_id, offer_id=offer["id"])

    resp = logged_in_client.post(f"/deals/{deal_id}/offer", data={
        "offer_id": "", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert get_deal(deal_id)["offer_id"] is None


def test_call_view_offer_select_has_submit_button_for_no_js(logged_in_client, db):
    offers.create_offer("Starter")
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert "<select name=\"offer_id\"" in resp.text
    assert "<button type=\"submit\">Save</button>" in resp.text


def test_create_offer_from_call_view(logged_in_client, db):
    pid, _, deal_id = _qualified_deal(db)
    resp = logged_in_client.post(f"/deals/{deal_id}/offer/new", data={
        "name": "On-the-spot Offer", "pitch": "New pitch", "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303
    from app.services.deals import get_deal
    deal = get_deal(deal_id)
    assert deal["offer_id"] is not None
    assert offers.get_offer(deal["offer_id"])["name"] == "On-the-spot Offer"
