import pytest
from itsdangerous import SignatureExpired, URLSafeSerializer

from app.routers.auth import SESSION_MAX_AGE, SECRET_KEY, cookie_signer
from app.services.auth import create_user


def test_timed_session_cookie_is_accepted(logged_in_client):
    resp = logged_in_client.get("/crm/pipeline", follow_redirects=False)
    assert resp.status_code == 200


def test_untimed_session_cookie_is_rejected(client):
    """Cookies minted with URLSafeSerializer (no timestamp) must not load."""
    user_id = create_user("oldcookie@example.com", "Old", "password123")
    token = URLSafeSerializer(SECRET_KEY, salt="session").dumps({"user_id": user_id})
    client.cookies.set("session", token)
    resp = client.get("/crm/pipeline", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fcrm%2Fpipeline"


def test_session_token_rejected_when_past_max_age(db):
    token = cookie_signer.dumps({"user_id": 1})
    with pytest.raises(SignatureExpired):
        cookie_signer.loads(token, max_age=-1)


def test_get_current_user_honors_session_max_age(client, monkeypatch):
    from app.routers import auth as auth_mod

    monkeypatch.setattr(auth_mod, "SESSION_MAX_AGE", -1)
    user_id = create_user("expired@example.com", "Expired", "password123")
    token = cookie_signer.dumps({"user_id": user_id})
    client.cookies.set("session", token)
    resp = client.get("/crm/pipeline", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fcrm%2Fpipeline"


def test_session_cookie_max_age_matches_loads_constant():
    assert SESSION_MAX_AGE == 60 * 60 * 24 * 30


def _cookie_flags(header: str) -> dict:
    flags = {}
    parts = [p.strip() for p in header.split(";")[1:]]
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            flags[key.lower()] = value
        else:
            flags[part.lower()] = True
    return flags


def _set_cookie_headers(resp):
    return resp.headers.get_list("set-cookie")


def test_local_login_sets_unprefixed_session_cookie(client):
    from app.services.auth import create_user, generate_csrf_token

    create_user("cookie@example.com", "Cookie", "password123")
    csrf = generate_csrf_token()
    resp = client.post("/auth/login", data={
        "email": "cookie@example.com", "password": "password123", "csrf_token": csrf,
    }, follow_redirects=False)
    cookies = _set_cookie_headers(resp)
    assert any(h.startswith("session=") for h in cookies)
    assert not any(h.startswith("__Host-session=") for h in cookies)
    session = [h for h in cookies if h.startswith("session=") and "Max-Age=0" not in h][-1]
    flags = _cookie_flags(session)
    assert flags.get("httponly") is True
    assert flags.get("samesite", "").lower() == "lax"
    assert "secure" not in flags
    assert "domain" not in flags


def test_secure_login_sets_host_prefixed_session_cookie(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.auth import create_user, generate_csrf_token

    create_user("hostcookie@example.com", "Host", "password123")
    with TestClient(create_app(), base_url="https://crm.example.com") as c:
        csrf = generate_csrf_token()
        resp = c.post("/auth/login", data={
            "email": "hostcookie@example.com", "password": "password123",
            "csrf_token": csrf,
        }, follow_redirects=False)
    cookies = _set_cookie_headers(resp)
    host = [h for h in cookies if h.startswith("__Host-session=")]
    assert host
    header = host[-1]
    assert "Max-Age=0" not in header
    flags = _cookie_flags(header)
    assert flags.get("secure") is True
    assert flags.get("httponly") is True
    assert flags.get("path") == "/"
    assert "domain" not in flags
    assert not any(h.startswith("session=") for h in cookies)


def test_secure_logout_clears_host_prefixed_cookie(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.routers.auth import cookie_signer, HOST_SESSION_COOKIE_NAME
    from app.services.auth import create_user, generate_csrf_token

    user_id = create_user("hostlogout@example.com", "Host", "password123")
    token = cookie_signer.dumps({"user_id": user_id, "sv": 0})
    with TestClient(create_app(), base_url="https://crm.example.com") as c:
        c.cookies.set(HOST_SESSION_COOKIE_NAME, token)
        csrf = generate_csrf_token(token)
        resp = c.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
    cookies = _set_cookie_headers(resp)
    cleared = [h for h in cookies if h.startswith("__Host-session=")]
    assert cleared
    header = cleared[-1]
    flags = _cookie_flags(header)
    assert flags.get("max-age") == "0"
    assert flags.get("path") == "/"
    assert flags.get("secure") is True
    assert "domain" not in flags


def test_login_empty_email_or_password_is_html_error_not_422(client):
    from app.services.auth import generate_csrf_token

    empty_email = client.post("/auth/login", data={
        "email": "", "password": "password123", "csrf_token": generate_csrf_token(),
    })
    assert empty_email.status_code == 400
    assert "text/html" in empty_email.headers.get("content-type", "")
    assert "Invalid email or password" in empty_email.text

    empty_password = client.post("/auth/login", data={
        "email": "test@example.com", "password": "", "csrf_token": generate_csrf_token(),
    })
    assert empty_password.status_code == 400
    assert "text/html" in empty_password.headers.get("content-type", "")
    assert "Invalid email or password" in empty_password.text


def test_login_missing_csrf_still_422(client):
    resp = client.post("/auth/login", data={
        "email": "test@example.com", "password": "password123",
    })
    assert resp.status_code == 422
    assert "application/json" in resp.headers.get("content-type", "")


def test_session_cookie_from_another_database_is_rejected(tmp_path, monkeypatch):
    """Same SECRET_KEY + same numeric user_id must not authenticate on another DB."""
    from fastapi.testclient import TestClient

    from app import database
    from app.main import create_app
    from app.routers import auth as auth_mod
    from app.services.auth import create_user

    monkeypatch.setenv("SECRET_KEY", "shared-copied-secret")
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    monkeypatch.setattr(database, "DB_PATH", str(db_a))
    database.init_db()
    user_a = create_user("a@example.com", "A", "password123")
    token = auth_mod.cookie_signer.dumps({"user_id": user_a})

    monkeypatch.setattr(database, "DB_PATH", str(db_b))
    database.init_db()
    create_user("b@example.com", "B", "password123")
    with TestClient(create_app()) as c:
        c.cookies.set("session", token)
        resp = c.get("/crm/pipeline", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login?next=%2Fcrm%2Fpipeline"


def test_fresh_databases_get_distinct_install_ids(tmp_path, monkeypatch):
    from app.database import get_install_id, init_db
    from app import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "one.db"))
    init_db()
    first = get_install_id()
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "two.db"))
    init_db()
    second = get_install_id()
    assert len(first) == 64
    assert len(second) == 64
    assert first != second


@pytest.mark.parametrize("next_url", [
    "//evil.example",
    "https://evil.example/",
    "javascript:alert(1)",
    "/\\evil.example",
    "\\\\evil.example",
    "/%2F%2Fevil.example",
    "/%2f/evil.example",
    "/%5Cevil.example",
    "/%252F%252Fevil.example",
    "/%09/evil.example",
    "/ok\r\nSet-Cookie: x=1",
    "/ok path",
    "evil.example",
])
def test_login_rejects_unsafe_return_to(client, next_url):
    from app.services.auth import generate_csrf_token

    create_user("next@example.com", "Next", "password123")
    page = client.get("/auth/login", params={"next": next_url})
    assert 'name="next"' not in page.text
    resp = client.post("/auth/login", data={
        "email": "next@example.com", "password": "password123",
        "csrf_token": generate_csrf_token(), "next": next_url,
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/contacts/partners"


def test_login_keeps_encoded_query_in_return_to(client):
    from app.services.auth import generate_csrf_token

    create_user("next2@example.com", "Next", "password123")
    next_url = "/oauth/authorize?redirect_uri=https%3A%2F%2Fgrok.com%2Fcb&state=a%5Cb"
    resp = client.post("/auth/login", data={
        "email": "next2@example.com", "password": "password123",
        "csrf_token": generate_csrf_token(), "next": next_url,
    }, follow_redirects=False)
    assert resp.headers["location"] == next_url


def test_deep_link_returns_to_same_url_after_login(client):
    from app.services.auth import generate_csrf_token

    create_user("deep@example.com", "Deep", "password123")
    deep_link = "/crm/pipeline?view=list&q=a%20b"
    resp = client.get(deep_link, follow_redirects=False)
    assert resp.status_code == 303
    login_url = resp.headers["location"]
    assert login_url == "/auth/login?next=%2Fcrm%2Fpipeline%3Fview%3Dlist%26q%3Da%2520b"
    assert 'name="next"' in client.get(login_url).text
    resp = client.post("/auth/login", data={
        "email": "deep@example.com", "password": "password123",
        "csrf_token": generate_csrf_token(), "next": deep_link,
    }, follow_redirects=False)
    assert resp.headers["location"] == deep_link


def test_post_to_protected_route_redirects_to_bare_login(client):
    resp = client.post("/contacts/partners/1/edit", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_htmx_get_redirects_to_bare_login(client):
    resp = client.get("/crm/pipeline", headers={"HX-Request": "true"}, follow_redirects=False)
    assert resp.headers["location"] == "/auth/login"


@pytest.mark.parametrize("path", ["//evil.example/crm", "/%2F%2Fevil.example"])
def test_require_login_drops_unsafe_return_to(db, path):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.routers.auth import require_login

    request = Request({"type": "http", "method": "GET", "path": path,
                       "query_string": b"", "headers": []})
    with pytest.raises(HTTPException) as exc:
        require_login(request)
    assert exc.value.headers["Location"] == "/auth/login"
