from app.services.auth import create_user


def _csrf_from(page_text):
    return page_text.split('name="csrf_token" value="')[1].split('"')[0]


def test_regional_settings_defaults_to_usd(logged_in_client):
    page = logged_in_client.get("/settings")
    assert '<option value="USD" selected>' in page.text


def test_regional_settings_saves_valid_currency(logged_in_client):
    page = logged_in_client.get("/settings")
    csrf = _csrf_from(page.text)

    resp = logged_in_client.post(
        "/settings/regional", data={"currency": "AUD", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = logged_in_client.get("/settings")
    assert '<option value="AUD" selected>' in page.text


def test_regional_settings_rejects_unlisted_currency(logged_in_client):
    page = logged_in_client.get("/settings")
    csrf = _csrf_from(page.text)

    logged_in_client.post(
        "/settings/regional", data={"currency": "XXX", "csrf_token": csrf},
        follow_redirects=False,
    )

    page = logged_in_client.get("/settings")
    assert '<option value="USD" selected>' in page.text


def test_regional_settings_requires_valid_csrf(client, db):
    user_id = create_user("test@example.com", "Test User", "password123")
    from app.routers.auth import cookie_signer
    client.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))

    client.post(
        "/settings/regional", data={"currency": "AUD", "csrf_token": "bogus"},
        follow_redirects=False,
    )

    page = client.get("/settings")
    assert '<option value="USD" selected>' in page.text
