from app.services import offers
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, update_deal_fields


def test_no_offers_by_default(db):
    assert offers.list_offers() == []
    assert offers.default_offer() is None
    assert offers.default_offer_id() is None


def test_create_offer(db):
    offer, error = offers.create_offer(
        "Starter", pitch="A free review", proof_point="Saved 10 hrs/week", price_anchor="$500/mo",
    )
    assert error is None
    assert offer["name"] == "Starter"
    assert offer["pitch"] == "A free review"
    assert offer["is_default"] == 0


def test_create_offer_requires_name(db):
    offer, error = offers.create_offer("   ")
    assert offer is None
    assert error == "name_required"


def test_create_default_offer_clears_previous_default(db):
    offers.create_offer("Starter", is_default=True)
    offer2, _ = offers.create_offer("Premium", is_default=True)
    assert offers.get_offer(offer2["id"])["is_default"] == 1
    starter = next(o for o in offers.list_offers() if o["name"] == "Starter")
    assert starter["is_default"] == 0
    assert offers.default_offer_id() == offer2["id"]


def test_update_offer(db):
    offer, _ = offers.create_offer("Starter")
    updated, error = offers.update_offer(offer["id"], pitch="New pitch", is_default=True)
    assert error is None
    assert updated["pitch"] == "New pitch"
    assert updated["is_default"] == 1


def test_update_unknown_offer_not_found(db):
    updated, error = offers.update_offer(999, pitch="x")
    assert updated is None
    assert error == "not_found"


def test_delete_offer_refused_when_in_use(db):
    offer, _ = offers.create_offer("Starter")
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid, offer_id=offer["id"])
    ok, error = offers.delete_offer(offer["id"])
    assert ok is False
    assert error == "offer_in_use"


def test_delete_unused_offer_succeeds(db):
    offer, _ = offers.create_offer("Starter")
    ok, error = offers.delete_offer(offer["id"])
    assert ok is True
    assert error is None
    assert offers.get_offer(offer["id"]) is None


def test_new_deal_uses_default_offer(db):
    offer, _ = offers.create_offer("Starter", is_default=True)
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    assert get_deal(deal_id)["offer_id"] == offer["id"]


def test_new_deal_with_no_default_offer_has_none(db):
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    assert get_deal(deal_id)["offer_id"] is None


def test_deal_can_switch_offer(db):
    offer1, _ = offers.create_offer("Starter")
    offer2, _ = offers.create_offer("Premium")
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, offer_id=offer1["id"])
    assert update_deal_fields(deal_id, offer_id=offer2["id"])
    assert get_deal(deal_id)["offer_id"] == offer2["id"]


def test_deal_offer_id_ignored_if_invalid(db):
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, offer_id=999)
    assert get_deal(deal_id)["offer_id"] is None


def test_create_offer_with_service_and_aud_price(db):
    sid = create_service("Consulting", "consulting")
    offer, error = offers.create_offer(
        "Consulting", service_id=sid, price=350, currency="USD",
        description="Discovery / deposit",
    )
    assert error is None
    assert offer["service_id"] == sid
    assert offer["price"] == 350
    assert offer["currency"] == "USD"
    assert offer["price_anchor"] == "$350"


def test_create_offer_same_name_and_service_is_idempotent(db):
    sid = create_service("Consulting", "consulting")
    first, _ = offers.create_offer("Consulting", service_id=sid, price=350)
    second, error = offers.create_offer("Consulting", service_id=sid, price=999)
    assert error is None
    assert second["id"] == first["id"]
    assert second["price"] == 350


def test_seed_starter_offers_when_services_exist(db):
    create_service("Consulting", "consulting")
    create_service("Website Rebuild", "website-rebuild")
    create_service("Monthly Retainer", "monthly-retainer")
    created = offers.seed_starter_offers()
    assert len(created) == 3
    names = {o["name"] for o in offers.list_offers()}
    assert names == {"Discovery Session", "Website Rebuild", "Monthly Retainer"}
    assert offers.seed_starter_offers() == []
