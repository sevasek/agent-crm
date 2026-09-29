"""Issue #56: a stolen session cookie survives a password change today, and
there's no self-service way to change a password or revoke other sessions."""

from fastapi.testclient import TestClient

from app.services.auth import bump_session_version, change_password, create_user
from app.routers.auth import cookie_signer


def _csrf_from(page_text):
    return page_text.split('name="csrf_token" value="')[1].split('"')[0]


def test_bumping_session_version_invalidates_an_outstanding_cookie(client, db):
    user_id = create_user("test@example.com", "Test", "password123")
    token = cookie_signer.dumps({"user_id": user_id, "sv": 0})
    client.cookies.set("session", token)
    assert client.get("/partners").status_code == 200

    bump_session_version(user_id)
    resp = client.get("/partners", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_change_password_requires_correct_current_password(db):
    user_id = create_user("test@example.com", "Test", "password123")
    ok, error = change_password(user_id, "wrong-current", "newpassword1")
    assert ok is False
    assert "incorrect" in error


def test_change_password_rejects_short_new_password(db):
    user_id = create_user("test@example.com", "Test", "password123")
    ok, error = change_password(user_id, "password123", "short")
    assert ok is False
    assert "8 characters" in error


def test_change_password_bumps_session_version(db):
    user_id = create_user("test@example.com", "Test", "password123")
    ok, error = change_password(user_id, "password123", "newpassword1")
    assert ok is True
    assert error == ""
    from app.services.auth import get_user_by_email
    assert get_user_by_email("test@example.com")["session_version"] == 1


def test_password_change_via_settings_keeps_current_session_but_logs_out_others(client, db):
    user_id = create_user("test@example.com", "Test", "password123")
    client.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))

    other = TestClient(client.app)
    other.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))
    assert other.get("/partners").status_code == 200

    page = client.get("/settings")
    csrf = _csrf_from(page.text)
    resp = client.post("/settings/password", data={
        "current_password": "password123", "new_password": "newpassword1",
        "csrf_token": csrf,
    })
    assert resp.status_code == 200
    assert "Password changed" in resp.text

    assert client.get("/partners").status_code == 200
    other_resp = other.get("/partners", follow_redirects=False)
    assert other_resp.status_code == 303
    assert other_resp.headers["location"] == "/auth/login"


def test_password_change_wrong_current_password_shows_error(logged_in_client):
    page = logged_in_client.get("/settings")
    csrf = _csrf_from(page.text)
    resp = logged_in_client.post("/settings/password", data={
        "current_password": "not-the-password", "new_password": "newpassword1",
        "csrf_token": csrf,
    })
    assert resp.status_code == 400
    assert "incorrect" in resp.text
    assert logged_in_client.get("/partners").status_code == 200  # still logged in


def test_logout_everywhere_logs_out_other_sessions_but_not_current(client, db):
    user_id = create_user("test@example.com", "Test", "password123")
    client.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))

    other = TestClient(client.app)
    other.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))
    assert other.get("/partners").status_code == 200

    page = client.get("/settings")
    csrf = _csrf_from(page.text)
    resp = client.post(
        "/settings/logout-everywhere", data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert resp.status_code == 303

    assert client.get("/partners").status_code == 200
    other_resp = other.get("/partners", follow_redirects=False)
    assert other_resp.status_code == 303
    assert other_resp.headers["location"] == "/auth/login"
