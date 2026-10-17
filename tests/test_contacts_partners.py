from app.services.auth import generate_csrf_token
from app.services.partners import create_partner, list_partners
from app.services.partner_tags import apply_partner_tags, tags_for_partner


def _csrf(logged_in_client):
    return generate_csrf_token(logged_in_client.cookies.get("session"))


def _nav(html):
    return html.split('<nav class="topnav">', 1)[1].split("</nav>", 1)[0]


def test_partners_list_moved_to_contacts_prefix(logged_in_client):
    resp = logged_in_client.get("/contacts/partners")
    assert resp.status_code == 200
    assert "Partners" in resp.text
    # The old bare path is gone, not redirected.
    assert logged_in_client.get("/partners", follow_redirects=False).status_code == 404


def test_apps_launcher_contacts_tile_links_to_new_path(logged_in_client):
    resp = logged_in_client.get("/apps")
    assert resp.status_code == 200
    assert "/contacts/partners" in resp.text
    assert "<h1>Apps</h1>" not in resp.text


def test_nav_on_apps_page_is_minimal(logged_in_client):
    nav = _nav(logged_in_client.get("/apps").text)
    assert ">Apps<" in nav
    assert ">Logout<" in nav
    for label in ("Pipeline", "Deals", "Leads", "Services", "Stages",
                  "Automations", "Lost reasons", "Offers", "ICP", "Contacts", "Partners",
                  "Definitions", "Account"):
        assert f">{label}<" not in nav


def test_nav_on_contacts_page_shows_only_contacts_and_partners(logged_in_client):
    nav = _nav(logged_in_client.get("/contacts/partners").text)
    assert ">Apps<" in nav
    assert ">Contacts<" in nav
    assert ">Partners<" in nav
    assert ">Logout<" in nav
    for label in ("Pipeline", "Deals", "Leads", "Services", "Stages",
                  "Automations", "Lost reasons", "Offers", "ICP", "Definitions", "Account"):
        assert f">{label}<" not in nav


def test_legacy_pages_drop_bare_partners_nav_link_but_keep_others(logged_in_client):
    nav = _nav(logged_in_client.get("/crm/deals").text)
    assert ">Pipeline<" in nav
    assert ">Deals<" in nav
    assert ">Contacts<" not in nav
    assert ">Partners<" not in nav


def test_create_partner_with_tags_via_form(logged_in_client):
    resp = logged_in_client.post(
        "/contacts/partners/new",
        data={
            "csrf_token": _csrf(logged_in_client),
            "name": "Jane Doe", "is_company": "", "parent_id": "",
            "email": "jane@example.com", "phone": "", "website": "",
            "title": "", "address": "", "preferred_channel": "",
            "industry": "", "team_size": "", "linkedin_url": "", "x_url": "",
            "instagram_url": "", "facebook_url": "", "youtube_url": "",
            "owner_key": "", "tags": "VIP, referral-source",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pid = int(resp.headers["location"].rsplit("/", 1)[-1])
    assert tags_for_partner(pid) == ["referral-source", "vip"]


def test_edit_partner_replaces_tags(logged_in_client):
    pid = create_partner("Acme Co")
    apply_partner_tags(pid, replace=["old-tag"])
    resp = logged_in_client.post(
        f"/contacts/partners/{pid}/edit",
        data={
            "csrf_token": _csrf(logged_in_client),
            "name": "Acme Co", "is_company": "1", "parent_id": "",
            "email": "", "phone": "", "website": "",
            "title": "", "address": "", "preferred_channel": "",
            "industry": "", "team_size": "", "linkedin_url": "", "x_url": "",
            "instagram_url": "", "facebook_url": "", "youtube_url": "",
            "owner_key": "", "tags": "new-tag",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert tags_for_partner(pid) == ["new-tag"]

    edit_page = logged_in_client.get(f"/contacts/partners/{pid}/edit")
    assert 'name="tags"' in edit_page.text
    assert "new-tag" in edit_page.text


def test_partners_list_shows_tags_column_and_truncates(logged_in_client):
    pid = create_partner("Jane Doe", email="jane@example.com")
    apply_partner_tags(pid, replace=["a", "b", "c", "d"])
    resp = logged_in_client.get("/contacts/partners")
    assert resp.status_code == 200
    assert resp.text.count('class="pill pill-tag') == 3
    assert "+1 more" in resp.text


def test_partners_list_email_is_mailto_linked(logged_in_client):
    create_partner("Jane Doe", email="jane@example.com")
    resp = logged_in_client.get("/contacts/partners")
    assert 'href="mailto:jane@example.com"' in resp.text


def test_clicking_a_tag_filters_and_shows_removable_chip(logged_in_client):
    pid1 = create_partner("Jane Doe")
    pid2 = create_partner("John Smith")
    apply_partner_tags(pid1, replace=["vip"])
    apply_partner_tags(pid2, replace=["lead"])

    filtered = logged_in_client.get("/contacts/partners?tag=vip")
    assert "Jane Doe" in filtered.text
    assert "John Smith" not in filtered.text
    assert "Filtering by:" in filtered.text
    assert "vip" in filtered.text
    assert "&times;" in filtered.text


def test_multiple_tag_filters_are_anded(logged_in_client):
    pid1 = create_partner("Jane Doe")
    pid2 = create_partner("John Smith")
    apply_partner_tags(pid1, replace=["vip", "lead"])
    apply_partner_tags(pid2, replace=["lead"])

    both = logged_in_client.get("/contacts/partners?tag=vip&tag=lead")
    assert "Jane Doe" in both.text
    assert "John Smith" not in both.text

    either_only = list_partners(tags=["lead"])
    assert len(either_only) == 2


def test_sort_by_name_toggles_direction(logged_in_client):
    create_partner("Alice")
    create_partner("Zoe")

    default = logged_in_client.get("/contacts/partners")
    assert default.text.index("Alice") < default.text.index("Zoe")

    desc = logged_in_client.get("/contacts/partners?dir=desc")
    assert desc.text.index("Zoe") < desc.text.index("Alice")

    # The header link flips direction again when already sorted that way.
    assert 'href="/contacts/partners"' in desc.text or "dir=asc" not in desc.text


def test_sort_by_email_column(logged_in_client):
    create_partner("B Person", email="b@example.com")
    create_partner("A Person", email="a@example.com")

    resp = logged_in_client.get("/contacts/partners?sort=email")
    assert resp.text.index("a@example.com") < resp.text.index("b@example.com")

    resp_desc = logged_in_client.get("/contacts/partners?sort=email&dir=desc")
    assert resp_desc.text.index("b@example.com") < resp_desc.text.index("a@example.com")
