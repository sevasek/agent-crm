from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, update_deal_fields


def _deal(db, **fields):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral", value_estimate=8000, **fields)
    return pid, sid, deal_id


def test_edit_deal_requires_login(client, db):
    resp = client.get("/deals/1/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_edit_deal_form_shows_existing_fields(logged_in_client, db):
    _, _, deal_id = _deal(
        db,
        pain_points="scattergun",
        goals="clarity first",
        next_action="Call Jane",
        next_action_date="2026-09-01T09:00:00",
    )
    resp = logged_in_client.get(f"/deals/{deal_id}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert "Edit deal" in html
    assert "Jane Doe" in html
    assert "Consulting" in html
    assert 'value="referral"' in html
    assert "scattergun" in html
    assert "clarity first" in html
    assert 'value="Call Jane"' in html
    assert 'value="2026-09-01"' in html
    assert 'name="partner_id"' not in html
    assert 'name="service_id"' not in html
    assert 'name="stage"' not in html


def test_edit_deal_saves_and_returns_to_partner(logged_in_client, db):
    pid, _, deal_id = _deal(db, pain_points="old pain", next_action="Ping")
    resp = logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(),
            "source": "website",
            "value_estimate": "1500",
            "pain_points": "Updated pain point",
            "goals": "Updated goal",
            "next_action": "Send outline",
            "next_action_date": "2026-09-15",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/partners/{pid}"

    deal = get_deal(deal_id)
    assert deal["source"] == "website"
    assert deal["value_estimate"] == 1500
    assert deal["pain_points"] == "Updated pain point"
    assert deal["goals"] == "Updated goal"
    assert deal["next_action"] == "Send outline"
    assert deal["next_action_date"] == "2026-09-15"
    assert deal["partner_id"] == pid
    assert deal["stage"] == "new"


def test_edit_deal_can_clear_optional_fields(logged_in_client, db):
    _, _, deal_id = _deal(
        db, pain_points="gone soon", goals="also gone",
        next_action="Call", next_action_date="2026-09-01",
    )
    logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(),
            "source": "referral",
            "value_estimate": "",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
        },
        follow_redirects=False,
    )
    deal = get_deal(deal_id)
    assert deal["value_estimate"] is None
    assert deal["pain_points"] is None
    assert deal["goals"] is None
    assert deal["next_action"] is None
    assert deal["next_action_date"] is None


def test_edit_deal_bad_csrf_does_not_save(logged_in_client, db):
    _, _, deal_id = _deal(db, pain_points="original")
    logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": "not-a-token",
            "source": "website",
            "pain_points": "should not land",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
            "value_estimate": "",
        },
        follow_redirects=False,
    )
    assert get_deal(deal_id)["pain_points"] == "original"
    assert get_deal(deal_id)["source"] == "referral"


def test_edit_deal_missing_redirects(logged_in_client, db):
    resp = logged_in_client.get("/deals/999/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/deals"


def test_edit_deal_invalid_value_keeps_existing(logged_in_client, db):
    _, _, deal_id = _deal(db, pain_points="keep me")
    logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(),
            "source": "website",
            "value_estimate": "not-a-number",
            "pain_points": "keep me",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
        },
        follow_redirects=False,
    )
    deal = get_deal(deal_id)
    assert deal["value_estimate"] == 8000
    assert deal["source"] == "website"


def test_edit_deal_null_source_does_not_render_none(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.get(f"/deals/{deal_id}/edit")
    assert resp.status_code == 200
    assert 'id="source"' in resp.text
    assert 'value="None"' not in resp.text
    assert 'id="next_action"' in resp.text


def test_edit_deal_zero_value_displays_and_persists(logged_in_client, db):
    _, _, deal_id = _deal(db)
    update_deal_fields(deal_id, value_estimate=0)
    page = logged_in_client.get(f"/deals/{deal_id}/edit")
    assert 'id="value_estimate"' in page.text
    assert 'value="0"' in page.text

    logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(),
            "source": "referral",
            "value_estimate": "0",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
        },
        follow_redirects=False,
    )
    assert get_deal(deal_id)["value_estimate"] == 0


def test_edit_deal_orphan_partner_post_does_not_save(logged_in_client, db):
    from app.database import get_db
    _, _, deal_id = _deal(db, pain_points="original")
    with get_db() as conn:
        conn.execute("UPDATE deals SET partner_id = 999 WHERE id = ?", (deal_id,))
        conn.commit()
    resp = logged_in_client.post(
        f"/deals/{deal_id}/edit",
        data={
            "csrf_token": generate_csrf_token(),
            "source": "website",
            "value_estimate": "1",
            "pain_points": "should not land",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/deals"
    assert get_deal(deal_id)["pain_points"] == "original"
    assert get_deal(deal_id)["source"] == "referral"


def test_edit_deal_orphan_partner_redirects(logged_in_client, db):
    from app.database import get_db
    _, _, deal_id = _deal(db)
    with get_db() as conn:
        conn.execute("UPDATE deals SET partner_id = 999 WHERE id = ?", (deal_id,))
        conn.commit()
    resp = logged_in_client.get(f"/deals/{deal_id}/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/deals"


def test_partner_and_deals_list_link_to_edit(logged_in_client, db):
    pid, _, deal_id = _deal(db)
    partner_page = logged_in_client.get(f"/partners/{pid}")
    assert f"/deals/{deal_id}/edit" in partner_page.text
    deals_page = logged_in_client.get("/deals")
    assert f"/deals/{deal_id}/edit" in deals_page.text
    assert "/deals/new" in deals_page.text
