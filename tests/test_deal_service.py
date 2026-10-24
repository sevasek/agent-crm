"""Changing a deal's service from update_deal, bulk_update_deals, and the edit form.

An offer tied to a different service blocks the move unless the same call
passes an offer for the new service or clears the offer. An offer with no
service is kept when offer_id is omitted. Stage, tags, and next action stay.
"""

from app.services.activities import list_activities_for_deal
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service, update_service
from app.services.deals import (
    assign_deals_service, create_deal, get_deal, set_deal_stage, update_deal_fields,
)
from app.services.offers import create_offer
from app.services.partners import create_partner
from tests.test_mcp_tools import _enable, call


def _pair(db):
    consulting = create_service("Consulting", "consulting")
    rebuild = create_service("Website Rebuild", "website-rebuild")
    partner = create_partner("Acme Retail", is_company=True, phone="0400100000")
    return consulting, rebuild, partner


def _deal(service_id, partner_id, **fields):
    return create_deal(
        partner_id, service_id, attach_default_offer=False,
        next_action="Call Jane", next_action_date="2026-09-01",
        tags=["clinic"], **fields,
    )


def _service_logs(deal_id):
    return [
        row for row in list_activities_for_deal(deal_id)
        if (row.get("body") or "").startswith("Service changed:")
    ]


def _unchanged_aside_from_service(deal_id, service_id, offer_id=None):
    deal = get_deal(deal_id)
    assert deal["service_id"] == service_id
    assert deal["offer_id"] == offer_id
    assert deal["stage"] == "qualified"
    assert deal["tags"] == ["clinic"]
    assert deal["next_action"] == "Call Jane"
    assert deal["next_action_date"] == "2026-09-01"
    return deal


def test_update_deal_by_service_id_logs_old_then_new(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)
    set_deal_stage(deal_id, "qualified")

    payload, is_error = call(client, "update_deal", {
        "deal_id": deal_id,
        "service_id": rebuild,
        "source": "website",
    })
    assert is_error is False
    assert payload["ok"] is True
    assert payload["deal"]["service_id"] == rebuild
    assert payload["deal"]["source"] == "website"
    _unchanged_aside_from_service(deal_id, rebuild)
    assert get_deal(deal_id)["source"] == "website"

    logs = _service_logs(deal_id)
    assert len(logs) == 1
    assert logs[0]["type"] == "system"
    assert logs[0]["body"] == "Service changed: consulting -> website-rebuild"
    stage_logs = [
        row["body"] for row in list_activities_for_deal(deal_id)
        if (row.get("body") or "").startswith("Stage changed:")
    ]
    assert stage_logs == ["Stage changed: new -> qualified"]


def test_update_deal_by_service_slug(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, _rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)
    payload, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_slug": "website-rebuild",
    })
    assert is_error is False
    assert payload["deal"]["service_id"] != consulting
    assert get_deal(deal_id)["next_action"] == "Call Jane"
    assert get_deal(deal_id)["tags"] == ["clinic"]
    assert get_deal(deal_id)["stage"] == "new"
    assert _service_logs(deal_id)[0]["body"] == "Service changed: consulting -> website-rebuild"


def test_update_deal_rejects_both_service_args(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)
    payload, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild, "service_slug": "website-rebuild",
    })
    assert is_error is True
    assert payload["error"] == "invalid_service"
    assert "not both" in payload["message"]
    assert get_deal(deal_id)["service_id"] == consulting
    assert _service_logs(deal_id) == []


def test_update_deal_rejects_missing_and_inactive_service(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)

    missing_id, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": 999999,
    })
    assert is_error is True
    assert missing_id["error"] == "invalid_service"
    assert "999999" in missing_id["message"]

    missing_slug, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_slug": "not-a-service",
    })
    assert is_error is True
    assert missing_slug["error"] == "invalid_service"
    assert "not-a-service" in missing_slug["message"]

    update_service(rebuild, active=False)
    inactive, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_slug": "website-rebuild",
    })
    assert is_error is True
    assert inactive["error"] == "inactive_service"
    assert "not active" in inactive["message"]
    assert get_deal(deal_id)["service_id"] == consulting
    assert _service_logs(deal_id) == []


def test_update_deal_same_service_does_not_log(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, _rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)
    payload, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": consulting,
    })
    assert is_error is False
    assert payload["ok"] is True
    assert get_deal(deal_id)["service_id"] == consulting
    assert _service_logs(deal_id) == []


def test_offer_tied_to_another_service_blocks_until_replaced_or_cleared(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    old_offer, _err = create_offer("Discovery", service_id=consulting, price=350)
    new_offer, _err = create_offer("Rebuild", service_id=rebuild, price=1200)
    other_offer, _err = create_offer("Retainer", service_id=consulting, price=900)
    loose, _err = create_offer("General", price=10)
    deal_id = _deal(consulting, partner, offer_id=old_offer["id"])
    set_deal_stage(deal_id, "qualified")

    blocked, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_slug": "website-rebuild",
    })
    assert is_error is True
    assert blocked["error"] == "offer_service_mismatch"
    assert "clear" in blocked["message"]
    _unchanged_aside_from_service(deal_id, consulting, old_offer["id"])
    assert _service_logs(deal_id) == []

    wrong, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild, "offer_id": other_offer["id"],
    })
    assert is_error is True
    assert wrong["error"] == "offer_service_mismatch"
    assert str(other_offer["id"]) in wrong["message"]
    _unchanged_aside_from_service(deal_id, consulting, old_offer["id"])

    missing, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild, "offer_id": 999999,
    })
    assert is_error is True
    assert missing["error"] == "invalid_offer"
    _unchanged_aside_from_service(deal_id, consulting, old_offer["id"])

    # A different offer with no service does not belong to the new service.
    loose_explicit, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild, "offer_id": loose["id"],
    })
    assert is_error is True
    assert loose_explicit["error"] == "offer_service_mismatch"
    _unchanged_aside_from_service(deal_id, consulting, old_offer["id"])

    replaced, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_slug": "website-rebuild", "offer_id": new_offer["id"],
    })
    assert is_error is False
    deal = _unchanged_aside_from_service(deal_id, rebuild, new_offer["id"])
    assert deal["offer_id"] == new_offer["id"]
    assert _service_logs(deal_id)[0]["body"] == "Service changed: consulting -> website-rebuild"

    # Put the consulting offer back by moving home with a matching offer, then clear.
    call(client, "update_deal", {
        "deal_id": deal_id, "service_id": consulting, "offer_id": old_offer["id"],
    })
    cleared, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild, "offer_id": None,
    })
    assert is_error is False
    assert get_deal(deal_id)["service_id"] == rebuild
    assert get_deal(deal_id)["offer_id"] is None
    assert get_deal(deal_id)["stage"] == "qualified"
    assert get_deal(deal_id)["tags"] == ["clinic"]
    assert get_deal(deal_id)["next_action"] == "Call Jane"


def test_unscoped_offer_is_kept_when_offer_id_omitted(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    loose, _err = create_offer("General", price=10)
    deal_id = _deal(consulting, partner, offer_id=loose["id"])
    payload, is_error = call(client, "update_deal", {
        "deal_id": deal_id, "service_id": rebuild,
    })
    assert is_error is False
    assert payload["ok"] is True
    deal = get_deal(deal_id)
    assert deal["service_id"] == rebuild
    assert deal["offer_id"] == loose["id"]
    assert deal["next_action"] == "Call Jane"
    assert _service_logs(deal_id)[0]["body"] == "Service changed: consulting -> website-rebuild"


def test_bulk_update_deals_sets_service_by_slug_and_id(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    retainer = create_service("Monthly Retainer", "monthly-retainer")
    first = _deal(consulting, partner)
    second = _deal(consulting, create_partner("Other Co", is_company=True, phone="0400100001"))
    already = _deal(rebuild, create_partner("Already There", is_company=True, phone="0400100002"))
    outsider = _deal(consulting, create_partner("Leave Me", is_company=True, phone="0400100003"))
    set_deal_stage(first, "qualified")

    by_slug, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [first, second, already],
        "set_service_slug": "website-rebuild",
    })
    assert is_error is False
    assert by_slug["ok"] is True
    assert by_slug["count"] == 3
    assert get_deal(first)["service_id"] == rebuild
    assert get_deal(second)["service_id"] == rebuild
    assert get_deal(outsider)["service_id"] == consulting
    assert get_deal(first)["stage"] == "qualified"
    assert get_deal(first)["tags"] == ["clinic"]
    assert get_deal(first)["next_action"] == "Call Jane"
    assert get_deal(second)["stage"] == "new"
    assert _service_logs(first)[0]["body"] == "Service changed: consulting -> website-rebuild"
    assert _service_logs(second)[0]["body"] == "Service changed: consulting -> website-rebuild"
    assert _service_logs(already) == []
    assert _service_logs(outsider) == []

    by_id, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [first, second],
        "set_service_id": retainer,
    })
    assert is_error is False
    assert get_deal(first)["service_id"] == retainer
    assert get_deal(second)["service_id"] == retainer
    assert get_deal(already)["service_id"] == rebuild
    assert get_deal(first)["stage"] == "qualified"
    assert get_deal(first)["tags"] == ["clinic"]
    assert _service_logs(first)[0]["body"] == "Service changed: website-rebuild -> monthly-retainer"


def test_bulk_service_change_validates_before_writing(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    offer, _err = create_offer("Discovery", service_id=consulting, price=350)
    blocked_id = _deal(consulting, partner, offer_id=offer["id"])
    clean_id = _deal(consulting, create_partner("Clean Co", is_company=True, phone="0400100009"))
    set_deal_stage(clean_id, "qualified")

    both, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [blocked_id, clean_id],
        "set_service_id": rebuild,
        "set_service_slug": "website-rebuild",
    })
    assert is_error is True
    assert both["error"] == "invalid_service"
    assert get_deal(blocked_id)["service_id"] == consulting
    assert get_deal(clean_id)["service_id"] == consulting

    mismatch, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [blocked_id, clean_id],
        "set_service_slug": "website-rebuild",
    })
    assert is_error is True
    assert mismatch["error"] == "offer_service_mismatch"
    assert str(blocked_id) in mismatch["message"]
    assert mismatch["deal_ids"] == [blocked_id]
    assert "Nothing was changed" in mismatch["message"]
    assert get_deal(blocked_id)["service_id"] == consulting
    assert get_deal(blocked_id)["offer_id"] == offer["id"]
    assert get_deal(clean_id)["service_id"] == consulting
    assert get_deal(clean_id)["stage"] == "qualified"
    assert get_deal(clean_id)["tags"] == ["clinic"]
    assert get_deal(clean_id)["next_action"] == "Call Jane"
    assert _service_logs(blocked_id) == []
    assert _service_logs(clean_id) == []

    update_service(rebuild, active=False)
    inactive, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [clean_id], "set_service_id": rebuild,
    })
    assert is_error is True
    assert inactive["error"] == "inactive_service"
    assert get_deal(clean_id)["service_id"] == consulting

    update_service(rebuild, active=True)
    missing, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [clean_id], "set_service_slug": "no-such-service",
    })
    assert is_error is True
    assert missing["error"] == "invalid_service"
    assert get_deal(clean_id)["service_id"] == consulting


def test_bulk_service_change_can_set_or_clear_offer(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    old_offer, _err = create_offer("Discovery", service_id=consulting, price=350)
    new_offer, _err = create_offer("Rebuild", service_id=rebuild, price=1200)
    first = _deal(consulting, partner, offer_id=old_offer["id"])
    second = _deal(consulting, create_partner("Second", is_company=True, phone="0400100011"), offer_id=old_offer["id"])

    replaced, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [first, second],
        "set_service_id": rebuild,
        "offer_id": new_offer["id"],
    })
    assert is_error is False
    assert replaced["count"] == 2
    assert get_deal(first)["service_id"] == rebuild
    assert get_deal(first)["offer_id"] == new_offer["id"]
    assert get_deal(second)["offer_id"] == new_offer["id"]
    assert get_deal(first)["next_action"] == "Call Jane"
    assert get_deal(first)["tags"] == ["clinic"]
    assert len(_service_logs(first)) == 1
    assert len(_service_logs(second)) == 1

    cleared, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [first, second],
        "set_service_slug": "consulting",
        "offer_id": None,
    })
    assert is_error is False
    assert get_deal(first)["service_id"] == consulting
    assert get_deal(first)["offer_id"] is None
    assert get_deal(second)["service_id"] == consulting
    assert get_deal(second)["offer_id"] is None
    assert get_deal(first)["stage"] == "new"
    assert _service_logs(first)[0]["body"] == "Service changed: website-rebuild -> consulting"


def test_bulk_service_slug_without_deal_ids_still_filters(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    matched = _deal(rebuild, partner)
    other = _deal(consulting, create_partner("Other", is_company=True, phone="0400100012"))
    payload, is_error = call(client, "bulk_update_deals", {
        "service_slug": "website-rebuild",
        "set_stage": "contacted",
    })
    assert is_error is False
    assert payload["updated_ids"] == [matched]
    assert get_deal(matched)["stage"] == "contacted"
    assert get_deal(matched)["service_id"] == rebuild
    assert get_deal(other)["stage"] == "new"
    assert get_deal(other)["service_id"] == consulting
    assert _service_logs(matched) == []


def test_bulk_service_change_stops_at_50(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    ids = [_deal(consulting, partner) for _ in range(51)]
    payload, is_error = call(client, "bulk_update_deals", {
        "deal_ids": ids,
        "set_service_id": rebuild,
    })
    assert is_error is False
    assert payload["count"] == 50
    assert get_deal(ids[49])["service_id"] == rebuild
    assert get_deal(ids[50])["service_id"] == consulting
    assert _service_logs(ids[50]) == []


def _csrf(logged_in_client):
    return generate_csrf_token(logged_in_client.cookies.get("session"))


def _edit(logged_in_client, deal_id, **extra):
    data = {
        "csrf_token": _csrf(logged_in_client),
        "source": "referral",
        "value_estimate": "",
        "pain_points": "",
        "goals": "",
        "next_action": "Call Jane",
        "next_action_date": "2026-09-01",
        "tags": "clinic",
    }
    data.update(extra)
    return logged_in_client.post(
        f"/crm/deals/{deal_id}/edit", data=data, follow_redirects=False,
    )


def test_edit_form_has_service_select_and_saves_it(logged_in_client, db):
    consulting, rebuild, partner = _pair(db)
    deal_id = _deal(consulting, partner)
    set_deal_stage(deal_id, "qualified")
    page = logged_in_client.get(f"/crm/deals/{deal_id}/edit")
    assert page.status_code == 200
    assert '<select id="service_id" name="service_id" required>' in page.text
    assert f'<option value="{consulting}" selected>Consulting</option>' in page.text
    assert 'name="offer_id"' in page.text
    assert 'name="stage"' not in page.text

    saved = _edit(logged_in_client, deal_id, service_id=str(rebuild), offer_id="")
    assert saved.status_code == 303
    deal = get_deal(deal_id)
    assert deal["service_id"] == rebuild
    assert deal["stage"] == "qualified"
    assert deal["tags"] == ["clinic"]
    assert deal["next_action"] == "Call Jane"
    assert deal["next_action_date"] == "2026-09-01"
    assert _service_logs(deal_id)[0]["body"] == "Service changed: consulting -> website-rebuild"


def test_edit_form_rejects_offer_mismatch_and_inactive_service(logged_in_client, db):
    consulting, rebuild, partner = _pair(db)
    offer, _err = create_offer("Discovery", service_id=consulting, price=350)
    deal_id = _deal(consulting, partner, offer_id=offer["id"])
    set_deal_stage(deal_id, "qualified")

    mismatch = _edit(
        logged_in_client, deal_id,
        service_id=str(rebuild), offer_id=str(offer["id"]),
        pain_points="should not land",
    )
    assert mismatch.status_code == 400
    assert "different service" in mismatch.text
    deal = get_deal(deal_id)
    assert deal["service_id"] == consulting
    assert deal["offer_id"] == offer["id"]
    assert deal["pain_points"] is None
    assert deal["stage"] == "qualified"
    assert deal["tags"] == ["clinic"]
    assert deal["next_action"] == "Call Jane"
    assert _service_logs(deal_id) == []

    update_service(rebuild, active=False)
    inactive = _edit(logged_in_client, deal_id, service_id=str(rebuild), offer_id="")
    assert inactive.status_code == 400
    assert "not active" in inactive.text
    assert get_deal(deal_id)["service_id"] == consulting
    assert get_deal(deal_id)["offer_id"] == offer["id"]


def test_edit_form_can_clear_offer_while_changing_service(logged_in_client, db):
    consulting, rebuild, partner = _pair(db)
    offer, _err = create_offer("Discovery", service_id=consulting, price=350)
    new_offer, _err = create_offer("Rebuild", service_id=rebuild, price=1200)
    deal_id = _deal(consulting, partner, offer_id=offer["id"])

    cleared = _edit(logged_in_client, deal_id, service_id=str(rebuild), offer_id="")
    assert cleared.status_code == 303
    assert get_deal(deal_id)["service_id"] == rebuild
    assert get_deal(deal_id)["offer_id"] is None
    assert get_deal(deal_id)["tags"] == ["clinic"]

    # Move back with an offer that belongs to consulting, then onto rebuild's offer.
    home = _edit(
        logged_in_client, deal_id,
        service_id=str(consulting), offer_id=str(offer["id"]),
    )
    assert home.status_code == 303
    moved = _edit(
        logged_in_client, deal_id,
        service_id=str(rebuild), offer_id=str(new_offer["id"]),
    )
    assert moved.status_code == 303
    assert get_deal(deal_id)["service_id"] == rebuild
    assert get_deal(deal_id)["offer_id"] == new_offer["id"]
    assert get_deal(deal_id)["next_action"] == "Call Jane"


def test_bulk_service_slug_with_deal_ids_only_filters(client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    on_rebuild = _deal(rebuild, partner)
    on_consulting = _deal(consulting, create_partner("Other", is_company=True, phone="0400100013"))
    payload, is_error = call(client, "bulk_update_deals", {
        "deal_ids": [on_rebuild, on_consulting],
        "service_slug": "website-rebuild",
        "set_stage": "contacted",
    })
    assert is_error is False
    assert payload["updated_ids"] == [on_rebuild]
    assert get_deal(on_consulting)["service_id"] == consulting
    assert get_deal(on_consulting)["stage"] == "new"
    assert _service_logs(on_consulting) == []


def test_assign_deals_service_rolls_back_on_missing_deal(db):
    consulting, rebuild, partner = _pair(db)
    first = _deal(consulting, partner)
    gone = dict(get_deal(first), id=999999)
    _ids, error, _blocked = assign_deals_service([get_deal(first), gone], rebuild)
    assert error == "not_found"
    assert get_deal(first)["service_id"] == consulting
    assert _service_logs(first) == []


def test_offer_only_change_rejects_other_services_offer(client, logged_in_client, db, monkeypatch):
    _enable(monkeypatch)
    consulting, rebuild, partner = _pair(db)
    theirs, _err = create_offer("Rebuild", service_id=rebuild, price=1200)
    loose, _err = create_offer("Loose", price=10)
    deal_id = _deal(consulting, partner)

    form = _edit(logged_in_client, deal_id, service_id=str(consulting), offer_id=str(theirs["id"]))
    assert form.status_code == 400
    assert "different service" in form.text
    missing = _edit(logged_in_client, deal_id, service_id=str(consulting), offer_id="999999")
    assert missing.status_code == 400
    assert get_deal(deal_id)["offer_id"] is None

    mcp, is_error = call(client, "update_deal", {"deal_id": deal_id, "offer_id": theirs["id"]})
    assert is_error is True
    assert mcp["error"] == "offer_service_mismatch"
    assert get_deal(deal_id)["offer_id"] is None

    ok = _edit(logged_in_client, deal_id, service_id=str(consulting), offer_id=str(loose["id"]))
    assert ok.status_code == 303
    assert get_deal(deal_id)["offer_id"] == loose["id"]
