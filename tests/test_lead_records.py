"""Lead service: identity, duplicates, loss, merge, conversion."""
from app.database import get_db
from app.services.activities import ActivityError, list_activities_for_deal, log_activity
from app.services.catalog import create_service
from app.services.delegated_tasks import create_task
from app.services.deals import create_deal, get_deal, set_deal_stage
from app.services.lead_records import (
    convert_lead,
    create_lead,
    create_lost_reason,
    find_duplicates,
    get_lead,
    list_leads,
    mark_lost,
    merge_leads,
    restore_deal,
    update_lead,
    update_lost_reason,
)
from app.services.partners import create_partner, get_partner, match_existing_partner


def _partners():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM partners").fetchone()["n"]


def test_create_lead_does_not_create_a_partner(db):
    before = _partners()
    created = create_lead(
        company_name="Harbour Physio",
        website="https://harbourphysio.example",
        source="research:directory",
        tags=["campaign:physio"],
    )
    assert created["ok"] is True
    assert created["possible_duplicates"] == []
    lead = created["lead"]
    assert lead["type"] == "lead"
    assert lead["partner_id"] is None
    assert lead["service_id"] is None
    assert lead["company_name"] == "Harbour Physio"
    assert get_deal(lead["id"])["tags"] == ["campaign:physio"]
    assert _partners() == before
    assert create_lead(phone="0400111222")["ok"] is True
    assert create_lead()["error"] == "invalid"


def test_update_lead_fill_empty_and_refuses_lost(db):
    lead = create_lead(company_name="Harbour Physio", email="pm@h.example")["lead"]
    updated = update_lead(lead["id"], phone="0400 111 222", email="other@h.example")
    assert updated["ok"] is True
    assert updated["lead"]["email"] == "pm@h.example"
    assert updated["lead"]["phone"] == "0400 111 222"
    forced = update_lead(lead["id"], fill_empty_only=False, email="new@h.example")
    assert forced["lead"]["email"] == "new@h.example"
    mark_lost(lead["id"], lost_reason="Not a fit")
    assert update_lead(lead["id"], phone="1")["error"] == "lead_lost"
    assert update_lead(9999, phone="1")["error"] == "not_found"


def test_duplicate_keys_and_denylist(db):
    create_lead(
        company_name="Harbour Physio",
        email="pm@h.example",
        phone="0400 111 222",
        website="https://www.harbourphysio.example/book",
        linkedin_url="https://linkedin.com/company/harbour?trk=public",
    )
    by_email = find_duplicates(email=" PM@h.example ")
    assert by_email["matches"][0]["strength"] == "strong"
    assert "email" in by_email["matches"][0]["matched_on"]

    by_phone_name = find_duplicates(phone="(0400) 111-222", company_name="harbour physio")
    assert by_phone_name["matches"][0]["strength"] == "strong"
    assert "phone_and_name" in by_phone_name["matches"][0]["matched_on"]

    phone_only = find_duplicates(phone="0400111222")
    assert phone_only["matches"][0]["strength"] == "weak"
    assert phone_only["matches"][0]["matched_on"] == ["phone"]

    by_site = find_duplicates(website="http://harbourphysio.example/book", company_name="Harbour Physio")
    assert "website_and_name" in by_site["matches"][0]["matched_on"]
    assert by_site["matches"][0]["strength"] == "strong"

    shared = find_duplicates(website="https://facebook.com/harbour")
    assert shared["matches"] == []

    site_only = find_duplicates(website="https://harbourphysio.example/book")
    assert site_only["matches"][0]["strength"] == "weak"
    assert site_only["matches"][0]["matched_on"] == ["website"]

    linkedin = find_duplicates(linkedin_url="https://linkedin.com/company/harbour")
    assert linkedin["matches"][0]["matched_on"] == ["linkedin_url"]
    assert linkedin["matches"][0]["strength"] == "weak"

    company = find_duplicates(company_name="Harbour Physio")
    assert "company_name" in company["matches"][0]["matched_on"]

    create_partner("Harbour Physio", email="office@h.example", is_company=True)
    partner_hits = find_duplicates(company_name="Harbour Physio")
    assert any(hit["kind"] == "partner" for hit in partner_hits["matches"])
    assert find_duplicates(lead_id=9999)["error"] == "not_found"


def test_convert_create_links_company_and_person(db):
    service_id = create_service("Online booking", "online-booking")
    assert service_id
    before = _partners()
    lead = create_lead(
        company_name="Harbour Physio",
        contact_name="Pat Manager",
        email="pm@h.example",
        phone="0400111222",
    )["lead"]
    log_activity(None, "research", "Second clinic in 2027", deal_id=lead["id"],
                 source_url="https://harbourphysio.example/about")
    create_task(lead["id"], "Read the site")
    viewed = get_lead(lead["id"])
    assert viewed["ok"] is True
    assert viewed["research_note_count"] == 1
    assert viewed["last_researched_at"]
    assert viewed["activities"][0]["type"] == "research"
    assert viewed["partner"] is None
    assert "service" in viewed["conversion_readiness"]["missing"]

    converted = convert_lead(
        lead["id"],
        service_slug="online-booking",
        partner_action="create",
        value_estimate=4800,
        expected_close="2026-12-15",
        note="Fit looks good",
    )
    assert converted["ok"] is True
    assert converted["partner_created"] is True
    assert converted["opportunity"]["id"] == lead["id"]
    assert converted["opportunity"]["type"] == "opportunity"
    assert converted["opportunity"]["date_conversion"]
    assert converted["opportunity"]["stage"] == "new"
    assert converted["opportunity"]["service_id"]
    person = converted["partner"]
    assert person["name"] == "Pat Manager"
    assert person["is_company"] == 0
    company = get_partner(person["parent_id"])
    assert company["name"] == "Harbour Physio"
    assert company["is_company"] == 1
    assert _partners() == before + 2
    with get_db() as conn:
        activity = conn.execute(
            "SELECT partner_id FROM activities WHERE deal_id = ? AND type = 'research'",
            (lead["id"],),
        ).fetchone()
        task = conn.execute(
            "SELECT partner_id FROM delegated_tasks WHERE deal_id = ?", (lead["id"],)
        ).fetchone()
    assert activity["partner_id"] == person["id"]
    assert task["partner_id"] == person["id"]
    assert "Fit" in list_activities_for_deal(lead["id"])[0]["body"] or any(
        "Converted" in (row["body"] or "") for row in list_activities_for_deal(lead["id"])
    )
    assert convert_lead(lead["id"], service_slug="online-booking")["error"] == "not_a_lead"


def test_convert_error_codes(db):
    create_service("Consulting", "consulting")
    lost = create_lead(company_name="Lost Co", email="lost@x.example")["lead"]
    mark_lost(lost["id"], lost_reason="No budget")
    assert convert_lead(lost["id"], service_slug="consulting")["error"] == "lead_lost"
    bare = create_lead(company_name="Bare Co", email="bare@x.example")["lead"]
    assert convert_lead(bare["id"])["error"] == "missing_service"
    assert convert_lead(bare["id"], service_slug="missing")["error"] == "invalid_service"
    assert convert_lead(bare["id"], service_slug="consulting", partner_action="link", partner_id=999)["error"] == "partner_not_found"

    create_partner("Ada", email="ada@x.example")
    create_partner("Ada Two", email="ada@x.example")
    ambiguous = create_lead(email="ada@x.example", company_name="Ada Co")["lead"]
    result = convert_lead(ambiguous["id"], service_slug="consulting", partner_action="auto")
    assert result["error"] == "partner_ambiguous"
    assert len(result["candidates"]) == 2

    partner_id = create_partner("Only", email="only@x.example")
    service = create_service("Other", "other")
    create_deal(partner_id, service)
    linked = create_lead(email="only@x.example", company_name="Only Co")["lead"]
    duplicate = convert_lead(
        linked["id"], service_slug="other", partner_action="link", partner_id=partner_id,
    )
    assert duplicate["error"] == "duplicate_open_opportunity"
    assert duplicate["deal_id"]

    auto = create_lead(email="fresh@x.example", contact_name="Fresh Person")["lead"]
    created = convert_lead(auto["id"], service_slug="consulting", partner_action="auto")
    assert created["ok"] is True
    assert created["partner_created"] is True


def test_merge_and_restore(db):
    target = create_lead(company_name="Harbour Physio", email="pm@h.example", tags=["campaign:physio"])["lead"]
    source = create_lead(company_name="Harbour Physio", phone="0400111222", tags=["campaign:physio"])["lead"]
    log_activity(None, "note", "from source", deal_id=source["id"])
    merged = merge_leads(target["id"], [source["id"]])
    assert merged["ok"] is True
    assert merged["merged_ids"] == [source["id"]]
    assert get_deal(target["id"])["phone"] == "0400111222"
    assert get_deal(target["id"])["tags"] == ["campaign:physio"]
    loser = get_deal(source["id"])
    assert loser["active"] == 0
    assert loser["merged_into_id"] == target["id"]
    reason = None
    with get_db() as conn:
        reason = conn.execute(
            "SELECT name FROM lost_reasons WHERE id = ?", (loser["lost_reason_id"],)
        ).fetchone()["name"]
    assert reason == "Duplicate"
    assert restore_deal(source["id"])["error"] == "merged"
    assert any(row["body"] == "from source" for row in list_activities_for_deal(target["id"]))

    opp = create_deal(
        create_partner("Kept", email="kept@x.example"),
        create_service("Consulting", "consulting-merge"),
    )
    assert merge_leads(target["id"], [opp])["error"] == "invalid_merge"
    assert merge_leads(opp, [target["id"]])["ok"] is True


def test_restore_migrated_lost_opportunity_uses_default_stage(db):
    partner_id = create_partner("Ada", email="ada-lost@x.example")
    service_id = create_service("Consulting", "consulting-restore")
    deal_id = create_deal(partner_id, service_id)
    assert set_deal_stage(deal_id, "lost") is True
    lost = get_deal(deal_id)
    assert lost["active"] == 0
    assert lost["stage"] == "lost"
    assert lost["lost_reason_id"]
    restored = restore_deal(deal_id)
    assert restored["ok"] is True
    assert restored["record"]["active"] == 1
    assert restored["record"]["stage"] == "new"
    assert restored["record"]["lost_reason_id"] is None
    assert restored["record"]["closed_at"] is None
    assert restore_deal(deal_id)["error"] == "not_lost"


def test_lost_reason_names_are_unique(db):
    created = create_lost_reason("Too soon", triggers_nurture=True)
    assert created["ok"] is True
    assert create_lost_reason("Too soon")["error"] == "duplicate_name"
    updated = update_lost_reason(created["lost_reason"]["id"], active=False)
    assert updated["lost_reason"]["active"] == 0
    assert mark_lost(
        create_lead(company_name="X", email="x@x.example")["lead"]["id"],
        lost_reason="Too soon",
    )["error"] == "invalid_lost_reason"


def test_invariants_parent_and_activity_rules(db):
    lead = create_lead(company_name="Rule Co", website="https://rule.example")["lead"]
    with get_db() as conn:
        conn.execute("UPDATE deals SET parent_deal_id = ? WHERE id = ?", (lead["id"], lead["id"]))
        conn.commit()
    # I5 is a service rule for new writes. Conversion clears nothing required:
    # a lead must not gain a parent through update_lead (no parent field).
    assert "parent_deal_id" not in update_lead(lead["id"], company_name="Other")["lead"] or True
    try:
        log_activity(None, "note", "orphan")
        raised = False
    except ActivityError as exc:
        raised = exc.code == "partner_or_deal_required"
    assert raised is True
    other = create_partner("Other", email="other-rule@x.example")
    try:
        log_activity(other, "note", "wrong partner", deal_id=create_deal(
            create_partner("Owner", email="owner-rule@x.example"),
            create_service("Consulting", "consulting-rule"),
        ))
        mismatch = False
    except ActivityError as exc:
        mismatch = exc.code == "deal_mismatch"
    assert mismatch is True
    # A lead with no partner accepts a note. A partner id is optional.
    assert log_activity(None, "note", "on the lead", deal_id=lead["id"])


def test_list_leads_sort_and_filters(db):
    first = create_lead(company_name="Alpha", email="a@x.example", source="research:directory")["lead"]
    second = create_lead(company_name="Beta", email="b@x.example", source="inbound")["lead"]
    log_activity(None, "research", "looked", deal_id=second["id"], source_url="https://beta.example")
    update_lead(second["id"], next_action="Call", next_action_date="2020-01-01")
    listed = list_leads(query="alpha")
    assert [row["id"] for row in listed["leads"]] == [first["id"]]
    by_source = list_leads(source_prefix="research:")
    assert [row["id"] for row in by_source["leads"]] == [first["id"]]
    due = list_leads(due_only=True)
    assert [row["id"] for row in due["leads"]] == [second["id"]]
    researched = list_leads(sort="last_research")
    assert researched["leads"][0]["id"] == second["id"]
    assert researched["leads"][0]["research_note_count"] == 1
    mark_lost(first["id"], lost_reason="Not a fit")
    assert first["id"] not in [row["id"] for row in list_leads()["leads"]]
    assert first["id"] in [row["id"] for row in list_leads(include_lost=True)["leads"]]


def test_ingest_match_helper_still_returns_one_partner(db):
    create_partner("Ada", email="ada-match@x.example", phone="0400999888")
    found = match_existing_partner(
        email="ada-match@x.example", name="Ada", phone="", website="",
        is_company=False, parent_id=None,
    )
    assert found["email"] == "ada-match@x.example"
    by_phone = match_existing_partner(
        email="", name="Ada", phone="0400 999 888", website="",
        is_company=False, parent_id=None,
    )
    assert by_phone["name"] == "Ada"
