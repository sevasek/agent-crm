import pytest
from itsdangerous import SignatureExpired, URLSafeSerializer

from app.routers.auth import SESSION_MAX_AGE, SECRET_KEY, cookie_signer
from app.services.auth import create_user


def test_timed_session_cookie_is_accepted(logged_in_client):
    resp = logged_in_client.get("/pipeline", follow_redirects=False)
    assert resp.status_code == 200


def test_untimed_session_cookie_is_rejected(client):
    """Cookies minted with URLSafeSerializer (no timestamp) must not load."""
    user_id = create_user("oldcookie@example.com", "Old", "password123")
    token = URLSafeSerializer(SECRET_KEY, salt="session").dumps({"user_id": user_id})
    client.cookies.set("session", token)
    resp = client.get("/pipeline", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_session_token_rejected_when_past_max_age():
    token = cookie_signer.dumps({"user_id": 1})
    with pytest.raises(SignatureExpired):
        cookie_signer.loads(token, max_age=-1)


def test_get_current_user_honors_session_max_age(client, monkeypatch):
    from app.routers import auth as auth_mod

    monkeypatch.setattr(auth_mod, "SESSION_MAX_AGE", -1)
    user_id = create_user("expired@example.com", "Expired", "password123")
    token = cookie_signer.dumps({"user_id": user_id})
    client.cookies.set("session", token)
    resp = client.get("/pipeline", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_session_cookie_max_age_matches_loads_constant():
    assert SESSION_MAX_AGE == 60 * 60 * 24 * 30
