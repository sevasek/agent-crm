from app.services.auth import generate_csrf_token
from app.services.partners import (
    create_partner, get_partner, get_children, list_partners, update_partner,
    apply_social_fields, listed_social_links, platform_field_for_url,
)


def test_edit_form_does_not_render_blank_fields_as_literal_none(logged_in_client):
    """Jinja renders {{ x }} as the literal text "None" when x is Python None.
    A partner created with no email/phone/etc. (e.g. via lead ingest) must not
    pre-fill those inputs with that literal string — it silently gets saved
    back as data, and blocks submission on the email field's type=email
    validation."""
    pid = create_partner("Acme Removal Co")
    resp = logged_in_client.get(f"/partners/{pid}/edit")
    assert resp.status_code == 200
    assert 'value="None"' not in resp.text


def test_new_partner_form_hides_company_only_fields(logged_in_client):
    resp = logged_in_client.get("/partners/new")
    assert resp.status_code == 200
    html = resp.text
    assert "This is a company" in html
    assert 'id="is_company"' in html
    assert "js-person-only" in html
    assert "js-company-only" in html
    assert "Company (if this is a person)" in html
    assert "Title (role at company)" in html
    assert "Team size" in html
    assert 'js-company-only" hidden' in html
    assert html.count('class="form-row js-person-only"') == 2
    assert 'js-person-only" hidden' not in html
    assert 'id="linkedin_url"' in html
    assert 'id="x_url"' in html
    assert 'id="instagram_url"' in html
    assert 'id="facebook_url"' in html
    assert 'id="youtube_url"' in html
    assert "Social / professional profile link" not in html
    assert "syncCompanyFields" in html


def test_company_edit_form_hides_person_only_fields(logged_in_client):
    pid = create_partner("Acme Childcare", is_company=True, team_size=12)
    resp = logged_in_client.get(f"/partners/{pid}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert 'js-person-only" hidden' in html
    assert html.count('js-person-only" hidden') == 2
    assert 'js-company-only" hidden' not in html
    assert 'value="12"' in html


def test_person_edit_form_hides_team_size(logged_in_client):
    pid = create_partner("Jane Doe", title="Director")
    resp = logged_in_client.get(f"/partners/{pid}/edit")
    assert resp.status_code == 200
    html = resp.text
    assert 'js-company-only" hidden' in html
    assert 'value="Director"' in html


def test_edit_form_preserves_team_size_zero(logged_in_client):
    pid = create_partner("Zero Headcount", is_company=True, team_size=0)
    resp = logged_in_client.get(f"/partners/{pid}/edit")
    assert resp.status_code == 200
    assert 'id="team_size"' in resp.text
    assert 'value="0"' in resp.text
    assert 'js-company-only" hidden' not in resp.text


def test_partner_detail_shows_team_size_zero(logged_in_client):
    pid = create_partner("Zero Headcount", is_company=True, team_size=0)
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "0 people" in resp.text


def test_partner_detail_omits_team_size_for_person(logged_in_client):
    pid = create_partner("Jane Doe", team_size=12)
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "12 people" not in resp.text
    assert "people ·" not in resp.text


def test_partner_detail_omits_missing_team_size(logged_in_client):
    pid = create_partner("Unknown Size", is_company=True)
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "people ·" not in resp.text
    assert "None people" not in resp.text


def test_create_company_via_form_drops_person_fields(logged_in_client, db):
    company_id = create_partner("Parent Co", is_company=True)
    resp = logged_in_client.post(
        "/partners/new",
        data={
            "csrf_token": generate_csrf_token(),
            "name": "Acme Childcare",
            "is_company": "1",
            "parent_id": str(company_id),
            "title": "Director",
            "team_size": "12",
            "linkedin_url": "https://linkedin.com/company/acme",
            "x_url": "https://x.com/acme",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    partner = get_partner(int(resp.headers["location"].rsplit("/", 1)[-1]))
    assert partner["is_company"] == 1
    assert partner["parent_id"] is None
    assert partner["title"] is None
    assert partner["team_size"] == 12
    assert partner["linkedin_url"] == "https://linkedin.com/company/acme"
    assert partner["x_url"] == "https://x.com/acme"
    assert partner["social_url"] == "https://linkedin.com/company/acme"


def test_create_person_via_form_drops_team_size(logged_in_client, db):
    company_id = create_partner("Acme Childcare", is_company=True)
    resp = logged_in_client.post(
        "/partners/new",
        data={
            "csrf_token": generate_csrf_token(),
            "name": "Jane Doe",
            "parent_id": str(company_id),
            "title": "Director",
            "team_size": "99",
            "instagram_url": "https://instagram.com/jane",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    partner = get_partner(int(resp.headers["location"].rsplit("/", 1)[-1]))
    assert partner["is_company"] == 0
    assert partner["parent_id"] == company_id
    assert partner["title"] == "Director"
    assert partner["team_size"] is None
    assert partner["instagram_url"] == "https://instagram.com/jane"


def test_partner_detail_lists_social_platforms(logged_in_client):
    pid = create_partner(
        "Jane Doe",
        linkedin_url="https://linkedin.com/in/jane",
        youtube_url="https://youtube.com/@jane",
    )
    resp = logged_in_client.get(f"/partners/{pid}")
    assert resp.status_code == 200
    assert "LinkedIn" in resp.text
    assert "YouTube" in resp.text
    assert "https://linkedin.com/in/jane" in resp.text
    assert "https://youtube.com/@jane" in resp.text


def test_create_and_get_partner(db):
    pid = create_partner("Acme Childcare", is_company=True, email="info@acme.example")
    partner = get_partner(pid)
    assert partner["name"] == "Acme Childcare"
    assert partner["is_company"] == 1
    assert partner["email"] == "info@acme.example"


def test_person_linked_to_company(db):
    company_id = create_partner("Acme Childcare", is_company=True)
    person_id = create_partner("Jane Doe", parent_id=company_id, title="Director")

    children = get_children(company_id)
    assert len(children) == 1
    assert children[0]["id"] == person_id
    assert children[0]["title"] == "Director"


def test_search_by_name_and_email(db):
    create_partner("Jane Doe", email="jane@acme.example")
    create_partner("Someone Else", email="someone@other.example")

    results = list_partners("jane")
    assert len(results) == 1
    assert results[0]["name"] == "Jane Doe"

    results = list_partners("other.example")
    assert len(results) == 1
    assert results[0]["name"] == "Someone Else"


def test_update_partner(db):
    pid = create_partner("Jane Doe")
    assert update_partner(pid, phone="0400 000 000")
    assert get_partner(pid)["phone"] == "0400 000 000"


def test_create_partner_with_profile_extras(db):
    pid = create_partner(
        "Acme Childcare", is_company=True,
        address="123 Example St, Springfield", social_url="https://linkedin.com/company/acme",
        industry="Childcare", team_size=12,
    )
    partner = get_partner(pid)
    assert partner["address"] == "123 Example St, Springfield"
    assert partner["social_url"] == "https://linkedin.com/company/acme"
    assert partner["linkedin_url"] == "https://linkedin.com/company/acme"
    assert partner["industry"] == "Childcare"
    assert partner["team_size"] == 12


def test_create_partner_stores_per_platform_socials(db):
    pid = create_partner(
        "Jane Doe",
        linkedin_url="https://linkedin.com/in/jane",
        x_url="https://x.com/jane",
        instagram_url="https://instagram.com/jane",
        facebook_url="https://facebook.com/jane",
        youtube_url="https://youtube.com/@jane",
    )
    partner = get_partner(pid)
    assert partner["linkedin_url"] == "https://linkedin.com/in/jane"
    assert partner["x_url"] == "https://x.com/jane"
    assert partner["instagram_url"] == "https://instagram.com/jane"
    assert partner["facebook_url"] == "https://facebook.com/jane"
    assert partner["youtube_url"] == "https://youtube.com/@jane"
    assert partner["social_url"] == "https://linkedin.com/in/jane"
    labels = [label for label, _url in listed_social_links(partner)]
    assert labels == ["LinkedIn", "X", "Instagram", "Facebook", "YouTube"]


def test_legacy_social_url_classifies_by_host(db):
    pid = create_partner("Jane Doe", social_url="https://instagram.com/jane")
    partner = get_partner(pid)
    assert partner["instagram_url"] == "https://instagram.com/jane"
    assert partner["linkedin_url"] is None
    assert partner["social_url"] == "https://instagram.com/jane"


def test_unclassified_social_url_lands_on_linkedin(db):
    pid = create_partner("Jane Doe", social_url="https://example.com/jane")
    partner = get_partner(pid)
    assert partner["linkedin_url"] == "https://example.com/jane"
    assert partner["social_url"] == "https://example.com/jane"


def test_platform_field_for_url():
    assert platform_field_for_url("https://www.linkedin.com/in/jane") == "linkedin_url"
    assert platform_field_for_url("https://x.com/jane") == "x_url"
    assert platform_field_for_url("https://twitter.com/jane") == "x_url"
    assert platform_field_for_url("instagram.com/jane") == "instagram_url"
    assert platform_field_for_url("https://example.com") is None


def test_apply_social_fields_does_not_clobber_existing_platform():
    applied = apply_social_fields(
        {"linkedin_url": "https://linkedin.com/in/jane", "x_url": None},
        {"social_url": "https://linkedin.com/in/other"},
    )
    assert applied["linkedin_url"] == "https://linkedin.com/in/jane"
    assert applied["social_url"] == "https://linkedin.com/in/jane"


def test_update_partner_social_platforms(db):
    pid = create_partner("Jane Doe", linkedin_url="https://linkedin.com/in/old")
    assert update_partner(pid, linkedin_url="", x_url="https://x.com/jane")
    partner = get_partner(pid)
    assert partner["linkedin_url"] is None
    assert partner["x_url"] == "https://x.com/jane"
    assert partner["social_url"] == "https://x.com/jane"


def test_init_db_backfills_legacy_social_url(db):
    from app.database import get_db, _backfill_partner_social_urls

    with get_db() as conn:
        conn.execute(
            "INSERT INTO partners (name, social_url) VALUES (?, ?)",
            ("Legacy Lead", "https://x.com/legacy"),
        )
        conn.commit()
        _backfill_partner_social_urls(conn)
    partner = list_partners("Legacy Lead")[0]
    assert partner["x_url"] == "https://x.com/legacy"
    assert partner["social_url"] == "https://x.com/legacy"


def test_preferred_channel_validated(db):
    pid = create_partner("Jane Doe", preferred_channel="phone")
    assert get_partner(pid)["preferred_channel"] == "phone"

    pid2 = create_partner("Someone Else", preferred_channel="carrier-pigeon")
    assert get_partner(pid2)["preferred_channel"] is None


def test_team_size_rejects_garbage(db):
    pid = create_partner("Acme Childcare", is_company=True, team_size="not-a-number")
    assert get_partner(pid)["team_size"] is None

    pid2 = create_partner("Another Co", is_company=True, team_size=-5)
    assert get_partner(pid2)["team_size"] is None


def test_update_partner_profile_extras(db):
    pid = create_partner("Jane Doe")
    assert update_partner(pid, industry="Childcare", team_size="7", preferred_channel="email")
    partner = get_partner(pid)
    assert partner["industry"] == "Childcare"
    assert partner["team_size"] == 7
    assert partner["preferred_channel"] == "email"
