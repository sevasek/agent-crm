from pathlib import Path

from app.services.catalog import create_service, get_service_by_slug
from app.services.partners import (
    create_partner, get_company_by_name, get_partner, get_partner_by_email,
    get_children, get_person_by_name,
)
from app.services.deals import list_deals
from app.services.activities import list_activities_for_partner
from app.services.client_import import parse_client_markdown, parse_client_file, import_client, import_paths

FIXTURES = Path(__file__).parent / "fixtures" / "clients"
ACME = FIXTURES / "acme-childcare-jane-doe.md"


def test_parse_frontmatter():
    record = parse_client_file(ACME)
    assert record["company"] == "Acme Childcare"
    assert record["name"] == "Jane Doe"
    assert record["title"] == "Director"
    assert record["industry"] == "Childcare"
    assert record["team_size"] == "12"
    assert record["service_slug"] == "consulting"
    assert "KidPlatform" in record["notes"]


def test_parse_block_scalars():
    record = parse_client_file(FIXTURES / "multiline-pain.md")
    assert record["company"] == "Folded Frontmatter Co"
    assert record["pain_points"] == (
        "No visibility on business processes.\n"
        "Systems described as scattergun."
    )
    assert record["goals"] == "A clear view of what to automate first, then a staged rollout."
    assert record["next_action"] == "Call Dana\nSend consulting outline"
    assert "Body note" in record["notes"]


def test_parse_indented_plain_and_list():
    record = parse_client_markdown("""---
company: List Co
pain_points:
  - No CRM
  - Manual invoicing
goals:
  Get out of the spreadsheet
---
""")
    assert record["pain_points"] == "No CRM\nManual invoicing"
    assert record["goals"] == "Get out of the spreadsheet"


def test_parse_keeps_prose_that_looks_like_a_yaml_key():
    record = parse_client_markdown("""---
company: Prose Co
pain_points:
  Note: they mentioned budget concerns.
  Follow-up needed monthly.
---
""")
    assert record["pain_points"] == (
        "Note: they mentioned budget concerns.\n"
        "Follow-up needed monthly."
    )


def test_nested_frontmatter_map_is_skipped():
    nested = parse_client_markdown("""---
company: Nested Co
contact:
  name: Should Skip
---
""")
    assert nested["company"] == "Nested Co"
    assert "name" not in nested
    assert "contact" not in nested


def test_h1_is_company_when_no_frontmatter():
    record = parse_client_file(FIXTURES / "solo-lead.md")
    assert record["company"] == "Solo Lead Pty"
    assert "name" not in record
    assert "company-only note" in record["notes"]


def test_empty_markdown_is_invalid():
    assert parse_client_markdown("") == {}
    assert parse_client_markdown("# ") == {}


def test_import_acme_seed_creates_company_person_deal(db):
    create_service("Consulting", "consulting")
    result = import_client(parse_client_file(ACME))

    assert result["company"] == "created"
    assert result["person"] == "created"
    assert result["deal"] == "created"

    company = get_company_by_name("Acme Childcare")
    assert company["is_company"] == 1
    assert company["industry"] == "Childcare"
    assert company["team_size"] == 12
    assert company["website"] == "https://acme.example"

    jane = get_partner_by_email("jane@acme.example")
    assert jane["name"] == "Jane Doe"
    assert jane["parent_id"] == company["id"]
    assert jane["title"] == "Director"
    assert jane["phone"] == "0400 111 222"
    assert get_children(company["id"])[0]["id"] == jane["id"]

    deals = list_deals(partner_id=jane["id"])
    assert len(deals) == 1
    assert deals[0]["source"] == "referral"
    assert "scattergun" in deals[0]["pain_points"]

    notes = list_activities_for_partner(jane["id"])
    assert any("KidPlatform" in (a["body"] or "") for a in notes)


def test_reimport_does_not_duplicate(db):
    create_service("Consulting", "consulting")
    first = import_client(parse_client_file(ACME))
    second = import_client(parse_client_file(ACME))

    assert second["company"] == "existing"
    assert second["person"] == "existing"
    assert second["deal"] == "duplicate_open_deal"
    assert second["company_id"] == first["company_id"]
    assert second["partner_id"] == first["partner_id"]
    assert second["deal_id"] == first["deal_id"]
    assert len(list_deals()) == 1


def test_reimport_does_not_overwrite_team_size_zero(db):
    create_service("Consulting", "consulting")
    company_id = create_partner("Zero Team Co", is_company=True, team_size=0)
    import_client({
        "company": "Zero Team Co",
        "team_size": "12",
        "industry": "Childcare",
    })
    company = get_partner(company_id)
    assert company["team_size"] == 0
    assert company["industry"] == "Childcare"


def test_email_match_does_not_cross_companies(db):
    create_service("Consulting", "consulting")
    acme = import_client({
        "company": "Acme",
        "name": "Pat Shared",
        "email": "pat@x.example",
        "service_slug": "consulting",
    })
    globex = import_client({
        "company": "Globex",
        "name": "Pat Shared",
        "email": "pat@x.example",
        "service_slug": "consulting",
    })
    assert globex["company"] == "created"
    assert globex["person"] == "created"
    assert globex["partner_id"] != acme["partner_id"]
    assert globex["company_id"] != acme["company_id"]

    acme_person = get_partner(acme["partner_id"])
    globex_person = get_partner(globex["partner_id"])
    assert acme_person["parent_id"] == acme["company_id"]
    assert globex_person["parent_id"] == globex["company_id"]

    acme_deals = list_deals(partner_id=acme["partner_id"])
    globex_deals = list_deals(partner_id=globex["partner_id"])
    assert len(acme_deals) == 1
    assert len(globex_deals) == 1
    assert acme_deals[0]["id"] != globex_deals[0]["id"]


def test_person_only_email_does_not_attach_to_company_person(db):
    acme = import_client({
        "company": "Acme",
        "name": "Pat Shared",
        "email": "pat@x.example",
    })
    solo = import_client({
        "name": "Pat Shared",
        "email": "pat@x.example",
    })
    assert solo["person"] == "created"
    assert solo["partner_id"] != acme["partner_id"]
    assert get_partner(solo["partner_id"])["parent_id"] is None
    assert get_partner(acme["partner_id"])["parent_id"] == acme["company_id"]


def test_reimport_fills_empty_fields_only(db):
    create_service("Consulting", "consulting")
    import_client(parse_client_file(ACME))
    company = get_company_by_name("Acme Childcare")
    from app.services.partners import update_partner
    update_partner(company["id"], industry="Edited")

    again = parse_client_file(ACME)
    import_client(again)
    assert get_partner(company["id"])["industry"] == "Edited"


def test_unknown_service_still_imports_partners(db):
    result = import_client(parse_client_file(ACME))
    assert result["company"] == "created"
    assert result["person"] == "created"
    assert result["deal"] == "unknown_service"
    assert get_service_by_slug("consulting") is None
    assert list_deals() == []


def test_import_paths_directory(db):
    create_service("Consulting", "consulting")
    results = import_paths([FIXTURES])
    files = {Path(r["file"]).name for r in results}
    assert "acme-childcare-jane-doe.md" in files
    assert "solo-lead.md" in files
    assert get_company_by_name("Solo Lead Pty") is not None
    solo = get_company_by_name("Solo Lead Pty")
    assert any("company-only note" in (a["body"] or "") for a in list_activities_for_partner(solo["id"]))
    assert get_company_by_name("Acme Childcare") is not None


def test_email_on_company_does_not_create_person(db):
    company_id = create_partner("Acme", is_company=True, email="info@acme.example")
    result = import_client({
        "company": "Acme",
        "name": "Pat Acme",
        "email": "info@acme.example",
    })
    assert result["person"] == "skipped_company_email"
    assert result["partner_id"] == company_id
    owner = get_partner_by_email("info@acme.example")
    assert owner["id"] == company_id
    assert owner["is_company"] == 1
    assert get_person_by_name("Pat Acme", parent_id=company_id) is None
    assert get_children(company_id) == []


def test_person_without_email_dedups_by_name(db):
    record = {
        "company": "Example Co",
        "name": "Alex Example",
        "title": "Owner",
    }
    first = import_client(record)
    second = import_client(record)
    assert first["person"] == "created"
    assert second["person"] == "existing"
    assert second["partner_id"] == first["partner_id"]


def test_name_only_without_company_does_not_merge(db):
    first = import_client({"name": "Alex Smith"})
    second = import_client({"name": "Alex Smith"})
    assert first["person"] == "created"
    assert second["person"] == "created"
    assert first["partner_id"] != second["partner_id"]


def test_canonical_key_wins_over_alias():
    record = parse_client_markdown("""---
name: Canonical Name
contact: Alias Name
company: Canonical Co
company_name: Alias Co
---
""")
    assert record["name"] == "Canonical Name"
    assert record["company"] == "Canonical Co"


def test_import_paths_continues_after_unreadable_file(db, tmp_path):
    good = tmp_path / "ok.md"
    good.write_text("---\ncompany: Good Co\n---\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    results = import_paths([good, bad])
    by_file = {Path(r["file"]).name: r for r in results}
    assert by_file["ok.md"]["status"] == "ok"
    assert by_file["ok.md"]["company"] == "created"
    assert by_file["bad.md"]["status"] == "error"
    assert by_file["bad.md"].get("error")
