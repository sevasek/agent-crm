"""Custom deal fields (Definitions -> Deal fields), the card title, title-aware
duplicate checks (several open deals per partner + service), and the
configurable organisation label."""
from app.services import deal_fields
from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, get_open_deal_for_partner_service, list_deals
from app.services.partners import create_partner
from app.services.workspace import org_label, set_org_label
from tests.test_mcp_tools import _enable, call


def _csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def _family_setup():
    """Preschool + OSHC services, a title field and a few child fields."""
    preschool = create_service("Preschool", "preschool")
    oshc = create_service("OSHC", "oshc")
    deal_fields.define_field(label="First name", type="text", is_title=True)
    deal_fields.define_field(label="Date of birth", type="date", required=True)
    deal_fields.define_field(label="Room", type="choice", choices=["Neville", "Rosa"],
                             service_ids=[preschool], show_on_card=True)
    deal_fields.define_field(label="Days", type="multi_choice", choices=["Mon", "Tue", "Wed"])
    deal_fields.define_field(label="Sibling enrolled", type="yes_no")
    deal_fields.define_field(label="Fee", type="number")
    partner = create_partner("Hannah Whitfield", email="hannah@example.com")
    return partner, preschool, oshc


# ---------- definitions ----------

def test_define_field_makes_key_from_label_and_locks_type(db):
    field, error = deal_fields.define_field(label="Child's first name", type="text")
    assert error is None and field["key"] == "childs_first_name"
    _f, error = deal_fields.define_field("childs_first_name", type="number")
    assert error == "type_locked"
    _f, error = deal_fields.define_field(label="Room", type="choice")
    assert error == "choices_required"
    _f, error = deal_fields.define_field(label="Start", type="date", is_title=True)
    assert error == "title_must_be_text"
    _f, error = deal_fields.define_field(label="2027", type="text")
    assert error == "invalid_key"


def test_only_one_title_field(db):
    deal_fields.define_field(label="First name", type="text", is_title=True)
    deal_fields.define_field(label="Nickname", type="text", is_title=True)
    assert deal_fields.title_field()["key"] == "nickname"
    assert not deal_fields.get_field("first_name")["is_title"]


def test_archiving_a_field_hides_it_and_keeps_values(db):
    partner, preschool, _oshc = _family_setup()
    values, _ = deal_fields.validate_values(
        {"first_name": "Olivia", "date_of_birth": "2022-08-14"}, preschool)
    deal_id = create_deal(partner, preschool, custom_fields=values)
    deal_fields.define_field("first_name", archived=True)
    assert "first_name" not in {f["key"] for f in deal_fields.list_fields()}
    assert deal_fields.title_field() is None
    assert get_deal(deal_id)["custom_fields"]["first_name"] == "Olivia"


# ---------- values ----------

def test_validate_values_coerces_each_type(db):
    _partner, preschool, _oshc = _family_setup()
    values, error = deal_fields.validate_values({
        "first_name": "  Olivia ", "date_of_birth": "2022-08-14", "room": "rosa",
        "days": "mon, Wed, mon", "sibling_enrolled": "yes", "fee": "120.0",
    }, preschool)
    assert error is None
    assert values == {
        "first_name": "Olivia", "date_of_birth": "2022-08-14", "room": "Rosa",
        "days": ["Mon", "Wed"], "sibling_enrolled": True, "fee": 120,
    }


def test_validate_values_errors_name_the_field(db):
    _partner, preschool, oshc = _family_setup()
    dob = {"date_of_birth": "2022-08-14"}
    cases = [
        ({**dob, "room": "Kitchen"}, preschool, "invalid_choice:room"),
        ({**dob, "room": "Rosa"}, oshc, "field_not_for_service:room"),
        ({"date_of_birth": "14/08/2022"}, preschool, "invalid_value:date_of_birth"),
        ({**dob, "fee": "lots"}, preschool, "invalid_value:fee"),
        ({**dob, "nope": "x"}, preschool, "unknown_field:nope"),
        ({"first_name": "Olivia"}, preschool, "required:date_of_birth"),
    ]
    for values, service, expected in cases:
        assert deal_fields.validate_values(values, service) == (None, expected)


def test_update_cannot_clear_a_required_field(db):
    _partner, preschool, _oshc = _family_setup()
    existing = {"date_of_birth": "2022-08-14"}
    assert deal_fields.validate_values(
        {"date_of_birth": None}, preschool, existing=existing, check_required=False,
    ) == (None, "required:date_of_birth")
    merged, error = deal_fields.validate_values(
        {"fee": 10}, preschool, existing=existing, check_required=False)
    assert error is None and merged == {"date_of_birth": "2022-08-14", "fee": 10}


# ---------- siblings / duplicate check ----------

def test_siblings_get_separate_open_deals_for_one_service(client, db, monkeypatch):
    _enable(monkeypatch)
    partner, _preschool, _oshc = _family_setup()
    base = {"partner_id": partner, "service_slug": "preschool"}
    olivia, err = call(client, "create_deal", {**base, "custom_fields": {
        "first_name": "Olivia", "date_of_birth": "2022-08-14"}})
    assert not err and olivia["status"] == "created"
    leo, err = call(client, "create_deal", {**base, "custom_fields": {
        "first_name": "Leo", "date_of_birth": "2023-03-02"}})
    assert not err and leo["status"] == "created"
    again, err = call(client, "create_deal", {**base, "custom_fields": {
        "first_name": " olivia ", "date_of_birth": "2022-08-14"}})
    assert not err and again["status"] == "duplicate_open_deal"
    assert again["deal"]["id"] == olivia["deal"]["id"]
    assert {d["card_title"] for d in list_deals(partner_id=partner)} == {"Olivia", "Leo"}


def test_without_a_title_field_one_open_deal_per_partner_service(db):
    service = create_service("Consulting", "consulting")
    partner = create_partner("Jane", email="jane@example.com")
    first = create_deal(partner, service)
    assert get_open_deal_for_partner_service(partner, service)["id"] == first


def test_lead_title_column_is_not_the_card_title(db):
    deal_fields.define_field(label="First name", type="text", is_title=True)
    service = create_service("Consulting", "consulting")
    partner = create_partner("Jane", email="jane@example.com")
    deal_id = create_deal(partner, service)
    deal = get_deal(deal_id)
    assert deal["card_title"] is None
    assert "title" in deal  # the existing contact-title column is untouched


# ---------- MCP ----------

def test_mcp_catalog_lists_fields_and_label(client, db, monkeypatch):
    _enable(monkeypatch)
    _family_setup()
    set_org_label("Family")
    out, err = call(client, "list_catalog")
    assert not err
    assert out["organisation_label"] == {"singular": "Family", "plural": "Families"}
    room = next(f for f in out["deal_fields"] if f["key"] == "room")
    assert room["choices"] == ["Neville", "Rosa"] and room["type"] == "choice"


def test_mcp_define_deal_field(client, db, monkeypatch):
    _enable(monkeypatch)
    out, err = call(client, "define_deal_field", {"label": "Site", "type": "choice",
                                                  "choices": ["Oatley", "Epping"]})
    assert not err and out["field"]["key"] == "site"
    out, err = call(client, "define_deal_field", {"key": "site", "type": "text"})
    assert err and out["error"] == "type_locked"


def test_mcp_update_merges_and_filters(client, db, monkeypatch):
    _enable(monkeypatch)
    partner, _preschool, _oshc = _family_setup()
    created, _ = call(client, "create_deal", {
        "partner_id": partner, "service_slug": "preschool",
        "custom_fields": {"first_name": "Olivia", "date_of_birth": "2022-08-14", "days": ["Mon"]},
    })
    deal_id = created["deal"]["id"]
    out, err = call(client, "update_deal", {"deal_id": deal_id, "custom_fields": {
        "room": "Neville", "days": None}})
    assert not err
    assert out["deal"]["custom_fields"] == {
        "first_name": "Olivia", "date_of_birth": "2022-08-14", "room": "Neville"}
    out, err = call(client, "update_deal", {"deal_id": deal_id, "custom_fields": {"room": "Attic"}})
    assert err and out["error"] == "invalid_choice"
    found, _ = call(client, "list_deals", {"custom_fields": {"room": "neville"}})
    assert [d["id"] for d in found["deals"]] == [deal_id]
    none, _ = call(client, "list_deals", {"custom_fields": {"room": "Rosa"}})
    assert none["deals"] == []
    out, err = call(client, "list_deals", {"custom_fields": {"nope": "x"}})
    assert err and out["error"] == "unknown_field"


def test_mcp_create_requires_required_fields(client, db, monkeypatch):
    _enable(monkeypatch)
    partner, _preschool, _oshc = _family_setup()
    out, err = call(client, "create_deal", {"partner_id": partner, "service_slug": "preschool",
                                            "custom_fields": {"first_name": "Olivia"}})
    assert err and out["error"] == "required"


def test_multi_choice_filter_matches_one_selection(db):
    partner, preschool, _oshc = _family_setup()
    values, _ = deal_fields.validate_values(
        {"first_name": "Olivia", "date_of_birth": "2022-08-14", "days": ["Mon", "Wed"]}, preschool)
    deal_id = create_deal(partner, preschool, custom_fields=values)
    assert [d["id"] for d in list_deals(custom_fields={"days": "wed"})] == [deal_id]
    assert list_deals(custom_fields={"days": "Tue"}) == []


# ---------- admin pages ----------

def test_definitions_page_adds_edits_and_archives(logged_in_client, db):
    preschool = create_service("Preschool", "preschool")
    resp = logged_in_client.post("/definitions/deal-fields/save", data={
        "label": "Room", "type": "choice", "choices": "Neville\nRosa",
        "service_ids": str(preschool), "show_on_card": "1",
        "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    field = deal_fields.get_field("room")
    assert field["choices"] == ["Neville", "Rosa"] and field["service_ids"] == [preschool]
    resp = logged_in_client.post("/definitions/deal-fields/save", data={
        "key": "room", "label": "Room", "choices": "Neville\nRosa\nWattle",
        "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    field = deal_fields.get_field("room")
    assert field["choices"] == ["Neville", "Rosa", "Wattle"]
    assert field["service_ids"] is None and not field["show_on_card"]
    logged_in_client.post("/definitions/deal-fields/room/archive", data={
        "archived": "1", "csrf_token": _csrf(logged_in_client)})
    assert deal_fields.get_field("room")["archived"]
    page = logged_in_client.get("/definitions/deal-fields")
    assert page.status_code == 200 and "archived" in page.text


def test_definitions_page_needs_csrf_and_shows_errors(logged_in_client, db):
    resp = logged_in_client.post("/definitions/deal-fields/save", data={
        "label": "Room", "type": "choice", "csrf_token": "bad"}, follow_redirects=False)
    assert resp.status_code == 303 and deal_fields.get_field("room") is None
    resp = logged_in_client.post("/definitions/deal-fields/save", data={
        "label": "Room", "type": "choice", "choices": "", "csrf_token": _csrf(logged_in_client)})
    assert resp.status_code == 400 and "at least one choice" in resp.text


def test_deal_form_saves_fields_and_pipeline_shows_title(logged_in_client, db):
    partner, preschool, _oshc = _family_setup()
    resp = logged_in_client.post("/crm/deals/new", data={
        "partner_id": str(partner), "service_id": str(preschool),
        "cf_first_name": "Olivia", "cf_date_of_birth": "2022-08-14", "cf_room": "Rosa",
        "cf_days": ["", "Mon", "Tue"], "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    deal = list_deals(partner_id=partner)[0]
    assert deal["custom_fields"] == {
        "first_name": "Olivia", "date_of_birth": "2022-08-14", "room": "Rosa", "days": ["Mon", "Tue"]}
    board = logged_in_client.get("/crm/pipeline").text
    assert "Olivia" in board and "Room: Rosa" in board
    # Unticking every day clears the field; a bad choice is a 400.
    resp = logged_in_client.post(f"/crm/deals/{deal['id']}/edit", data={
        "cf_days": "", "cf_room": "Rosa", "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "days" not in get_deal(deal["id"])["custom_fields"]
    resp = logged_in_client.post(f"/crm/deals/{deal['id']}/edit", data={
        "cf_room": "Attic", "csrf_token": _csrf(logged_in_client)})
    assert resp.status_code == 400 and "pick one of Neville, Rosa" in resp.text


def test_new_deal_form_requires_required_fields(logged_in_client, db):
    partner, preschool, _oshc = _family_setup()
    resp = logged_in_client.post("/crm/deals/new", data={
        "partner_id": str(partner), "service_id": str(preschool),
        "cf_first_name": "Olivia", "csrf_token": _csrf(logged_in_client)})
    assert resp.status_code == 400 and "Date of birth is required" in resp.text
    assert list_deals(partner_id=partner) == []


# ---------- organisation label ----------

def test_org_label_defaults_and_derives_plural(db):
    assert org_label() == {"singular": "Company", "plural": "Companies"}
    assert set_org_label("Household")[0] == {"singular": "Household", "plural": "Households"}
    assert set_org_label("Family")[0]["plural"] == "Families"
    assert set_org_label("")[1] == "label_required"


def test_workspace_setting_changes_contact_wording(logged_in_client, db):
    resp = logged_in_client.post("/settings/workspace", data={
        "org_label_singular": "Family", "org_label_plural": "Companies",
        "csrf_token": _csrf(logged_in_client),
    }, follow_redirects=False)
    assert resp.status_code == 303
    # The plural was left as it was, so it follows the new label.
    assert org_label() == {"singular": "Family", "plural": "Families"}
    html = logged_in_client.get("/contacts/partners/new").text
    assert "This is a family" in html and "Family (if this is a person)" in html
    resp = logged_in_client.post("/settings/workspace", data={
        "org_label_singular": "Clan", "csrf_token": "bad"}, follow_redirects=False)
    assert resp.status_code == 303 and org_label()["singular"] == "Family"


def test_get_partner_returns_org_label(client, db, monkeypatch):
    _enable(monkeypatch)
    partner = create_partner("Whitfield family", is_company=True)
    set_org_label("Family")
    out, err = call(client, "get_partner", {"partner_id": partner})
    assert not err and out["organisation_label"]["singular"] == "Family"
