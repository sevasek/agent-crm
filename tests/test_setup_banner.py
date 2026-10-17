"""Insecure-defaults setup banner: visible until SECRET_KEY/SECURE_COOKIES are fixed."""
from app.routers.auth import cookie_signer, session_cookie_name
from app.services.auth import create_user

BANNER = "This instance is running with insecure defaults"


def _log_in(client):
    """Mint the session cookie after env changes.

    The session signer reads SECRET_KEY on every call, so the cookie has to be
    created with whatever env the test has set.
    """
    user_id = create_user("ops@example.com", "Ops User", "password123")
    client.cookies.set(
        session_cookie_name(), cookie_signer.dumps({"user_id": user_id, "sv": 0})
    )


def test_banner_shows_with_default_env(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    _log_in(client)

    resp = client.get("/contacts/partners")
    assert resp.status_code == 200
    assert 'href="/contacts/partners" aria-current="page">Partners</a>' in resp.text
    assert BANNER in resp.text
    assert "SECRET_KEY is still the placeholder value" in resp.text
    assert "SECURE_COOKIES is not enabled" in resp.text
    assert "See docs/DEPLOY.md for how to fix both." in resp.text
    assert 'New here? Visit <a href="/settings">Settings</a>' in resp.text


def test_banner_hidden_when_env_is_fixed(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-real-secret-key-for-tests")
    monkeypatch.setenv("SECURE_COOKIES", "true")
    _log_in(client)

    resp = client.get("/contacts/partners")
    assert resp.status_code == 200
    assert 'href="/contacts/partners" aria-current="page">Partners</a>' in resp.text
    assert BANNER not in resp.text
    assert "New here?" not in resp.text


def test_banner_never_shows_on_login_page(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")

    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert BANNER not in resp.text


def test_banner_names_only_the_setting_that_is_wrong(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.setenv("SECURE_COOKIES", "true")
    _log_in(client)

    resp = client.get("/contacts/partners")
    assert resp.status_code == 200
    assert 'href="/contacts/partners" aria-current="page">Partners</a>' in resp.text
    assert BANNER in resp.text
    assert "SECRET_KEY is still the placeholder value" in resp.text
    assert "SECURE_COOKIES is not enabled" not in resp.text
