"""One-time passwords (bootstrap / generated) expire and must be changed first."""
import sys

from app.database import get_db
from app.services import api_keys as api_keys_service
from app.services.auth import (
    ONE_TIME_PASSWORD_EXPIRED,
    create_user,
    generate_csrf_token,
    get_user_by_email,
    reset_password,
)


def _login(client, email="otp@example.com", password="one-time-pass"):
    return client.post("/auth/login", data={
        "email": email, "password": password,
        "csrf_token": generate_csrf_token(),
    }, follow_redirects=False)


def _expire(email="otp@example.com"):
    with get_db() as db:
        db.execute(
            "UPDATE users SET password_expires_at = '2000-01-01 00:00:00' WHERE email = ?",
            (email,),
        )
        db.commit()


def test_create_user_is_permanent_unless_one_time(db):
    create_user("own@example.com", "Own", "own-password")
    create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    assert get_user_by_email("own@example.com")["password_expires_at"] is None
    assert get_user_by_email("otp@example.com")["password_expires_at"]


def test_one_time_login_only_reaches_password_change(client, db):
    user_id = create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings#password"

    assert client.get("/contacts/partners", follow_redirects=False).headers["location"] == "/settings#password"
    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "one-time password" in settings.text

    session_csrf = generate_csrf_token(client.cookies.get("session"))
    blocked = client.post("/settings/api-keys/new", data={
        "csrf_token": session_csrf, "name": "sneaky",
    }, follow_redirects=False)
    assert blocked.headers["location"] == "/settings#password"
    assert api_keys_service.list_api_keys(user_id) == []

    changed = client.post("/settings/password", data={
        "csrf_token": session_csrf,
        "current_password": "one-time-pass", "new_password": "my-own-password",
    })
    assert changed.status_code == 200
    assert "one-time password" not in changed.text
    assert get_user_by_email("otp@example.com")["password_expires_at"] is None
    assert client.get("/contacts/partners", follow_redirects=False).status_code == 200


def test_expired_one_time_password_cannot_log_in(client, db):
    create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    _expire()
    resp = _login(client)
    assert resp.status_code == 403
    assert ONE_TIME_PASSWORD_EXPIRED in resp.text.replace("&#39;", "'")
    assert "session" not in resp.cookies
    # A wrong password still gets the generic error, not the expiry notice.
    wrong = _login(client, password="wrong-password")
    assert wrong.status_code == 400
    assert "expired" not in wrong.text


def test_one_time_session_cannot_authorize_oauth(client, db, monkeypatch):
    from tests.test_mcp_oauth import _enable, _pkce, _register

    _enable(monkeypatch)
    create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    _login(client)
    _, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    form = {
        "client_id": registered["client_id"], "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    }
    page = client.get("/oauth/authorize", params=form, follow_redirects=False)
    assert page.headers["location"] == "/settings#password"
    submitted = client.post("/oauth/authorize", data={
        **form, "csrf_token": generate_csrf_token(client.cookies.get("session")),
    }, follow_redirects=False)
    assert submitted.status_code == 401
    assert "code=" not in submitted.headers.get("location", "")


def test_reset_password_issues_new_one_time_and_logs_out(db):
    user_id = create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    _expire()
    before = get_user_by_email("otp@example.com")["session_version"]
    reset_password(user_id, "fresh-one-time")
    user = get_user_by_email("otp@example.com")
    assert user["password_expires_at"] > "2000-01-01 00:00:00"
    assert user["session_version"] == before + 1


def test_create_admin_reset_cli(db, monkeypatch, capsys):
    monkeypatch.syspath_prepend("scripts")
    import create_admin

    create_user("otp@example.com", "OTP", "one-time-pass", one_time=True)
    _expire()
    monkeypatch.setenv("CREATE_ADMIN_PASSWORD", "fresh-one-time")
    monkeypatch.setattr(sys, "argv", ["create_admin.py", "--reset", "OTP@example.com"])
    create_admin.main()
    out = capsys.readouterr().out
    assert "Reset the password" in out
    assert "One-time password" in out
    user = get_user_by_email("otp@example.com")
    assert user["password_expires_at"] > "2000-01-01 00:00:00"
