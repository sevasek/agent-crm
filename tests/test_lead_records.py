"""ADR 0001 P2: lead service layer, duplicates, convert, merge, lost/restore."""
from app.database import get_db
from app.services.activities import log_activity
from app.services.catalog import create_service
from app.services.deals import (
    create_deal,
    get_deal,
    mark_lost,
    restore_deal,
    set_deal_stage,
)
from app.services.delegated_tasks import create_task
from app.services.lead_records import (
    convert_lead,
    create_lead,
    find_duplicates,
    get_lead,
    lead_has_identity,
    list_leads,
    merge_leads,
    update_lead,
)
from app.services.lost_reasons import (
    create_lost_reason,
    list_lost_reasons,
    update_lost_reason,
)
from app.services.partners import create_partner
from tests.lead_model_fixtures import apply_through_v7, populate_schema_7_sample


def _partners_count():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM partners").fetchone()[0]


def test_create_lead_does_not_create_a_partner(db):
    before = _partners_count()
    lead, error = create_lead(company_name="Harbour Physio", website="https://harbourphysio.example")
    assert error is None
    assert lead["type"] == "lead"
    assert lead["partner_id"] is None
    assert lead["service_id"] is None
    assert lead["id"]
    assert _partners_count() == before


def test_create_lead_i4_requires_identity(db):
    lead, error = create_lead(title="Manager")
    assert lead is None
    assert error == "invalid"
    assert not lead_has_identity({"title": "Manager"})


def test_create_lead_i5_rejects_parent(db):
    lead, error = create_lead(company_name="Acme", parent_deal_id=1)
    assert lead is None
    assert error == "invalid"


def test_create_lead_invalid_service_and_partner(db):
    _, error = create_lead(company_name="Acme", service_slug="nope")
    assert error == "invalid_service"
    _, error = create_lead(company_name="Acme", partner_id=999)
    assert error == "partner_not_found"


def test_update_lead_fill_empty_and_errors(db):
    lead, _error = create_lead(company_name="Harbour Physio", phone="0400111222")
    updated, error = update_lead(lead["id"], phone="0499000000", website="https://harbourphysio.example")
    assert error is None
    assert updated["phone"] == "0400111222"
    assert updated["website"] == "https://harbourphysio.example"
    updated, error = update_lead(lead["id"], phone="0499000000", fill_empty_only=False)
    assert updated["phone"] == "0499000000"
    _, error = update_lead(lead["id"] + 99, company_name="x")
    assert error == "not_found"
    pid = create_partner("Jane", email="j@x.example")
    sid = create_service("Consulting", "consulting-p2")
    opp = create_deal(pid, sid)
    _, error = update_lead(opp, company_name="x")
    assert error == "not_a_lead"
    mark_lost(lead["id"], lost_reason="Not a fit")
    _, error = update_lead(lead["id"], website="https://other.example")
    assert error == "lead_lost"


def test_update_lead_invalid_tags_write_no_fields(db):
    lead, _error = create_lead(company_name="Harbour Physio", phone="0400111222")
    updated, error = update_lead(lead["id"], next_action="Call back", tags=[123])
    assert error == "invalid"
    assert updated is None
    fresh = get_deal(lead["id"])
    assert fresh["next_action"] is None
    from app.services.deal_tags import tags_for_deal
    assert not tags_for_deal(lead["id"])
    updated, error = update_lead(lead["id"], next_action="Call back", add_tags="followup")
    assert error is None
    assert updated["next_action"] == "Call back"


def test_mark_lost_and_restore_and_i2(db):
    lead, _error = create_lead(company_name="Harbour Physio")
    _, error = mark_lost(lead["id"], lost_reason="not-a-real-reason")
    assert error == "invalid_lost_reason"
    assert get_deal(lead["id"])["active"] == 1
    lost, error = mark_lost(lead["id"], lost_reason="Not a fit", note="Out of area")
    assert error is None
    assert lost["active"] == 0
    assert lost["lost_note"] == "Out of area"
    assert lost["closed_at"]
    _, error = mark_lost(lead["id"], lost_reason="No budget")
    assert error == "already_lost"
    restored, error = restore_deal(lead["id"])
    assert error is None
    assert restored["active"] == 1
    assert restored["lost_reason_id"] is None
    assert restored["closed_at"] is None
    _, error = restore_deal(lead["id"])
    assert error == "not_lost"


def test_restore_migrated_lost_stage_opportunity(tmp_path, monkeypatch):
    path = tmp_path / "data" / "crm.db"
    path.parent.mkdir()
    monkeypatch.setattr("app.database.DB_PATH", str(path))
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    import app.database as database
    apply_through_v7(database)
    sample = populate_schema_7_sample(database, null_closed_lost=False)
    database.init_db()
    deal = get_deal(sample["lost_id"])
    assert deal["active"] == 0
    assert deal["stage"] == "lost"
    restored, error = restore_deal(sample["lost_id"])
    assert error is None
    assert restored["active"] == 1
    assert restored["stage"] == "new"
    assert restored["lost_reason_id"] is None


def test_set_deal_stage_lost_shim(db):
    pid = create_partner("Jane", email="j@x.example")
    sid = create_service("Consulting", "consulting-shim")
    deal_id = create_deal(pid, sid)
    assert set_deal_stage(deal_id, "lost") is True
    deal = get_deal(deal_id)
    assert deal["active"] == 0
    assert deal["stage"] == "lost"
    from app.services.lost_reasons import get_lost_reason
    reason = get_lost_reason(deal["lost_reason_id"])
    assert reason["name"] == "Lost stage (migrated)"


def test_convert_errors_and_partner_actions(db):
    sid = create_service("Online booking", "online-booking")
    lead, _error = create_lead(company_name="Harbour Physio", website="https://harbourphysio.example")
    result, error = convert_lead(lead["id"])
    assert error == "missing_service"
    result, error = convert_lead(999, service_slug="online-booking")
    assert error == "not_a_lead"
    mark_lost(lead["id"], lost_reason="Not a fit")
    result, error = convert_lead(lead["id"], service_slug="online-booking")
    assert error == "lead_lost"
    restore_deal(lead["id"])

    before = _partners_count()
    result, error = convert_lead(
        lead["id"], service_slug="online-booking", partner_action="create",
        value_estimate=4800, note="Fit enough",
    )
    assert error is None
    assert result["opportunity"]["id"] == lead["id"]
    assert result["opportunity"]["type"] == "opportunity"
    assert result["opportunity"]["date_conversion"]
    assert result["opportunity"]["stage"] == "new"
    assert result["opportunity"]["service_id"] == sid
    assert result["partner_created"] is True
    assert _partners_count() == before + 1
    assert result["partner"]["name"] == "Harbour Physio"
    assert result["partner"]["is_company"] == 1

    other, _error = create_lead(
        company_name="Harbour Physio 2", contact_name="Pat Manager",
        website="https://other.example",
    )
    result, error = convert_lead(
        other["id"], service_slug="online-booking", partner_action="link",
        partner_id=result["partner"]["id"],
    )
    assert error == "duplicate_open_opportunity"
    assert result["deal_id"] == lead["id"]

    _, error = convert_lead(other["id"], service_slug="online-booking", partner_action="link", partner_id=999)
    assert error == "partner_not_found"

    a = create_partner("Pat", email="pat@dup.example")
    b = create_partner("Pat", phone="0400111000")
    amb, _error = create_lead(
        contact_name="Pat", email="pat@dup.example", phone="0400111000",
    )
    payload, error = convert_lead(amb["id"], service_slug="online-booking", partner_action="auto")
    assert error == "partner_ambiguous"
    assert {c["id"] for c in payload["candidates"]} == {a, b}


def test_convert_auto_create_and_link_and_activity_partner(db):
    create_service("Online booking", "online-booking")
    lead, _error = create_lead(
        company_name="Solo Clinic", contact_name="Ada North",
        email="ada@solo.example", website="https://solo.example",
    )
    log_activity(None, "research", "Website is phone-only", deal_id=lead["id"],
                 source_url="https://solo.example/about")
    create_task(lead["id"], "Check hours", owner="agent")
    before = _partners_count()
    result, error = convert_lead(lead["id"], service_slug="online-booking", partner_action="auto")
    assert error is None
    assert result["partner_created"] is True
    assert _partners_count() == before + 2  # company + person
    person = result["partner"]
    assert person["name"] == "Ada North"
    assert person["parent_id"]
    with get_db() as db:
        acts = list(db.execute(
            "SELECT partner_id, type FROM activities WHERE deal_id = ?", (lead["id"],)
        ))
        tasks = list(db.execute(
            "SELECT partner_id FROM delegated_tasks WHERE deal_id = ?", (lead["id"],)
        ))
    assert all(row[0] == person["id"] for row in acts)
    assert all(row[0] == person["id"] for row in tasks)
    assert any(row[1] == "research" for row in acts)

    existing = create_partner("Known", email="known@x.example")
    lead2, _error = create_lead(contact_name="Known", email="known@x.example")
    result, error = convert_lead(lead2["id"], service_slug="online-booking", partner_action="auto")
    assert error is None
    assert result["partner_created"] is False
    assert result["partner"]["id"] == existing


def test_convert_create_company_and_person(db):
    create_service("Online booking", "online-booking")
    lead, _error = create_lead(
        company_name="Harbour Physio", contact_name="Pat Lee",
        website="https://harbourphysio.example",
    )
    before = _partners_count()
    result, error = convert_lead(lead["id"], service_slug="online-booking", partner_action="create")
    assert error is None
    assert _partners_count() == before + 2
    assert result["partner"]["name"] == "Pat Lee"
    assert result["partner"]["is_company"] == 0
    from app.services.partners import get_partner
    company = get_partner(result["partner"]["parent_id"])
    assert company["name"] == "Harbour Physio"
    assert company["is_company"] == 1


def test_convert_create_onto_preexisting_company_not_partner_created(db):
    create_service("Online booking", "online-booking")
    existing = create_partner(
        "Harbour Physio", is_company=True, website="https://harbourphysio.example",
    )
    lead, _error = create_lead(
        company_name="Harbour Physio", website="https://harbourphysio.example",
    )
    before = _partners_count()
    result, error = convert_lead(
        lead["id"], service_slug="online-booking", partner_action="create",
    )
    assert error is None
    assert result["partner_created"] is False
    assert result["partner"]["id"] == existing
    assert _partners_count() == before


def test_find_duplicates_strong_weak_and_denylist(db):
    create_lead(company_name="Alpha", email="shared@alpha.example", phone="0400111222",
                website="https://alpha.example", linkedin_url="https://linkedin.com/in/alpha?foo=1")
    create_lead(company_name="Reception", phone="0400111222")
    create_lead(company_name="Hosted", website="https://facebook.com/alpha")
    hits, error = find_duplicates(email="shared@alpha.example")
    assert error is None
    assert any(h["strength"] == "strong" and "email" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(phone="0400 111 222", company_name="Alpha")
    assert any(h["strength"] == "strong" and "phone_and_name" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(phone="0400111222")
    assert any(h["strength"] == "weak" and "phone" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(website="https://www.alpha.example/", company_name="Alpha")
    assert any(h["strength"] == "strong" and "website_and_name" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(website="https://alpha.example")
    assert any(h["strength"] == "weak" and "website" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(website="https://facebook.com/alpha")
    assert not any("website" in h["matched_on"] and h["strength"] == "weak" for h in hits)
    hits, _error = find_duplicates(linkedin_url="https://linkedin.com/in/alpha")
    assert any("linkedin_url" in h["matched_on"] for h in hits)
    hits, _error = find_duplicates(company_name="Alpha")
    assert any("company_name" in h["matched_on"] for h in hits)
    pid = create_partner("Alpha", email="partner@alpha.example", website="https://alpha.example")
    hits, _error = find_duplicates(email="partner@alpha.example")
    assert any(h["kind"] == "partner" and h["id"] == pid and h["strength"] == "strong" for h in hits)


def test_merge_leads_and_cannot_restore(db):
    target, _error = create_lead(company_name="Harbour Physio", email="a@h.example")
    source, _error = create_lead(company_name="Harbour Physio", phone="0400111222", website="https://h.example")
    apply_tags_both = __import__("app.services.deal_tags", fromlist=["apply_deal_tags"]).apply_deal_tags
    apply_tags_both(target["id"], replace=["shared", "keep"])
    apply_tags_both(source["id"], replace=["shared", "extra"])
    log_activity(None, "research", "From source", deal_id=source["id"], source_url="https://h.example")
    result, error = merge_leads(target["id"], [source["id"]])
    assert error is None
    assert source["id"] in result["merged_ids"]
    merged = get_deal(source["id"])
    assert merged["active"] == 0
    assert merged["merged_into_id"] == target["id"]
    from app.services.lost_reasons import get_lost_reason
    assert get_lost_reason(merged["lost_reason_id"])["name"] == "Duplicate"
    kept = get_deal(target["id"])
    assert kept["phone"] == "0400111222"
    assert kept["website"] == "https://h.example"
    from app.services.deal_tags import tags_for_deal
    assert set(tags_for_deal(target["id"])) == {"shared", "keep", "extra"}
    from app.services.activities import list_activities_for_deal
    bodies = [a["body"] for a in list_activities_for_deal(target["id"])]
    assert any("From source" in (b or "") for b in bodies)
    _, error = restore_deal(source["id"])
    assert error == "merged"
    _, error = merge_leads(target["id"], [target["id"]])
    assert error == "invalid_merge"
    opp_id = create_deal(
        create_partner("X", email="x@x.example"),
        create_service("S", "s-merge"),
    )
    _, error = merge_leads(target["id"], [opp_id])
    assert error == "invalid_merge"


def test_merge_leads_dedupes_duplicate_source_ids(db):
    target, _error = create_lead(company_name="Harbour Physio", email="a@h.example")
    source, _error = create_lead(
        company_name="Harbour Physio", phone="0400111222", email="b@h.example",
    )
    result, error = merge_leads(target["id"], [source["id"], source["id"]])
    assert error is None
    assert result["merged_ids"] == [source["id"]]
    merged = get_deal(source["id"])
    assert merged["active"] == 0
    assert merged["merged_into_id"] == target["id"]


def test_merge_leads_missing_duplicate_reason_fails_before_writes(db):
    target, _error = create_lead(company_name="Harbour Physio", email="a@h.example")
    source, _error = create_lead(
        company_name="Harbour Physio", phone="0400111222", email="b@h.example",
    )
    from app.services.lost_reasons import get_lost_reason_by_name
    update_lost_reason(get_lost_reason_by_name("Duplicate")["id"], name="Duplicate renamed")
    result, error = merge_leads(target["id"], [source["id"]])
    assert error == "invalid_lost_reason"
    assert result is None
    untouched_source = get_deal(source["id"])
    assert untouched_source["active"] == 1
    assert untouched_source["merged_into_id"] is None
    assert get_deal(target["id"])["phone"] is None


def test_list_leads_and_get_lead(db):
    create_lead(company_name="Harbour Physio", email="pm@harbour.example", source="research:directory")
    create_lead(company_name="Other", email="o@x.example")
    rows = list_leads(query="harbour")
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Harbour Physio"
    assert "fit_score" in rows[0]
    full = get_lead(rows[0]["id"])
    assert full["possible_duplicates"] is not None
    assert "ready" in full["conversion_readiness"]
    assert "service" in full["conversion_readiness"]["missing"]
    assert full["research_note_count"] == 0


def test_lost_reasons_service(db):
    reasons = list_lost_reasons()
    assert any(r["name"] == "Not now" and r["triggers_nurture"] == 1 for r in reasons)
    created, error = create_lost_reason("Not now")
    assert created is None
    assert error == "duplicate_name"
    created, error = create_lost_reason("Wrong suburb")
    assert error is None
    updated, error = update_lost_reason(created["id"], name="Not a fit")
    assert error == "duplicate_name"
    updated, error = update_lost_reason(created["id"], active=False)
    assert error is None
    assert updated["active"] == 0


def test_i1_opportunity_has_partner_and_service(db):
    lead, _error = create_lead(company_name="Need Service")
    _, error = convert_lead(lead["id"], partner_action="create")
    assert error == "missing_service"
    assert get_deal(lead["id"])["type"] == "lead"


def test_activities_research_and_null_partner(db):
    lead, _error = create_lead(company_name="Harbour Physio")
    activity_id = log_activity(
        None, "research", "Found a second clinic plan",
        deal_id=lead["id"], source_url="https://harbourphysio.example/news",
    )
    from app.services.activities import list_activities_for_deal, clean_source_url
    assert clean_source_url("ftp://nope") is None
    assert clean_source_url("https://ok.example/" + ("a" * 2000)) is None
    rows = list_activities_for_deal(lead["id"], research_first=True)
    assert rows[0]["id"] == activity_id
    assert rows[0]["type"] == "research"
    assert rows[0]["partner_id"] is None
    assert rows[0]["source_url"] == "https://harbourphysio.example/news"
