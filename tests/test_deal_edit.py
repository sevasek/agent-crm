from app.services.auth import generate_csrf_token
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, list_deals, update_deal_fields


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


def _two_deals(db):
    pid = create_partner("Jane Doe", email="jane@acme.example")
    consulting = create_service("Consulting", "consulting")
    support = create_service("Support", "support")
    parent_id = create_deal(pid, consulting)
    child_id = create_deal(pid, support)
    return pid, consulting, support, parent_id, child_id


def _edit_payload(parent_deal_id="", **extra):
    data = {
        "csrf_token": generate_csrf_token(),
        "source": "referral",
        "value_estimate": "",
        "pain_points": "",
        "goals": "",
        "next_action": "",
        "next_action_date": "",
        "parent_deal_id": parent_deal_id,
    }
    data.update(extra)
    return data


def test_new_deal_form_has_parent_picker(logged_in_client, db):
    pid, consulting, _support, parent_id, child_id = _two_deals(db)
    resp = logged_in_client.get("/deals/new")
    assert resp.status_code == 200
    assert 'name="parent_deal_id"' in resp.text
    assert f'value="{parent_id}"' in resp.text
    assert f'value="{child_id}"' in resp.text
    assert f'data-partner-id="{pid}"' in resp.text
    assert "Consulting" in resp.text


def test_edit_deal_form_lists_parent_candidates_and_children(logged_in_client, db):
    _pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    update_deal_fields(child_id, parent_deal_id=parent_id)

    parent_page = logged_in_client.get(f"/deals/{parent_id}/edit")
    assert parent_page.status_code == 200
    assert 'name="parent_deal_id"' in parent_page.text
    assert f'<option value="{parent_id}"' not in parent_page.text
    assert f"/deals/{child_id}/edit" in parent_page.text
    assert "Follow-on deals" in parent_page.text
    assert "Support" in parent_page.text

    child_page = logged_in_client.get(f"/deals/{child_id}/edit")
    assert child_page.status_code == 200
    assert f'<option value="{parent_id}"' in child_page.text
    assert 'selected' in child_page.text
    assert f'<option value="{child_id}"' not in child_page.text
    assert "Follow-on deals" not in child_page.text


def test_edit_deal_sets_parent(logged_in_client, db):
    pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    resp = logged_in_client.post(
        f"/deals/{child_id}/edit",
        data=_edit_payload(str(parent_id)),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/partners/{pid}"
    assert get_deal(child_id)["parent_deal_id"] == parent_id


def test_edit_deal_clears_parent(logged_in_client, db):
    _pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    update_deal_fields(child_id, parent_deal_id=parent_id)
    resp = logged_in_client.post(
        f"/deals/{child_id}/edit",
        data=_edit_payload(""),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert get_deal(child_id)["parent_deal_id"] is None


def test_edit_deal_rejects_other_partner_parent(logged_in_client, db):
    _pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    other = create_partner("Other Co", email="other@acme.example")
    stranger_id = create_deal(other, create_service("Audit", "audit"))

    resp = logged_in_client.post(
        f"/deals/{child_id}/edit",
        data=_edit_payload(str(stranger_id)),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "same partner" in resp.text
    assert get_deal(child_id)["parent_deal_id"] is None


def test_edit_deal_rejects_parent_cycle(logged_in_client, db):
    _pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    update_deal_fields(child_id, parent_deal_id=parent_id)
    resp = logged_in_client.post(
        f"/deals/{parent_id}/edit",
        data=_edit_payload(str(child_id)),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "cycle" in resp.text
    assert get_deal(parent_id)["parent_deal_id"] is None
    assert get_deal(child_id)["parent_deal_id"] == parent_id


def test_new_deal_sets_parent(logged_in_client, db):
    pid, _consulting, support, parent_id, _child_id = _two_deals(db)
    resp = logged_in_client.post(
        "/deals/new",
        data={
            "csrf_token": generate_csrf_token(),
            "partner_id": str(pid),
            "service_id": str(support),
            "source": "repeat",
            "value_estimate": "",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
            "parent_deal_id": str(parent_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/partners/{pid}"
    follow_ons = [d for d in list_deals(partner_id=pid) if d.get("parent_deal_id") == parent_id]
    assert follow_ons
    assert follow_ons[0]["source"] == "repeat"


def test_new_deal_rejects_other_partner_parent(logged_in_client, db):
    pid, consulting, _support, _parent_id, _child_id = _two_deals(db)
    other = create_partner("Other Co", email="other@acme.example")
    stranger_id = create_deal(other, create_service("Audit", "audit"))
    resp = logged_in_client.post(
        "/deals/new",
        data={
            "csrf_token": generate_csrf_token(),
            "partner_id": str(pid),
            "service_id": str(consulting),
            "source": "",
            "value_estimate": "",
            "pain_points": "",
            "goals": "",
            "next_action": "",
            "next_action_date": "",
            "parent_deal_id": str(stranger_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "same partner" in resp.text
    assert all(d.get("parent_deal_id") is None for d in list_deals(partner_id=pid))


def test_pipeline_and_deals_list_show_follow_on(logged_in_client, db):
    _pid, _consulting, _support, parent_id, child_id = _two_deals(db)
    update_deal_fields(child_id, parent_deal_id=parent_id)

    board = logged_in_client.get("/pipeline")
    assert board.status_code == 200
    assert f"follow-on of #{parent_id}" in board.text
    assert "1 child" in board.text

    deals_page = logged_in_client.get("/deals")
    assert f"follow-on of #{parent_id}" in deals_page.text
