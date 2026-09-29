"""Issue #57: CSRF token must be session-bound, and cookie-authenticated
state-changing requests must be same-origin (defense-in-depth)."""

from app.services.auth import create_user, generate_csrf_token, validate_csrf_token
from app.routers.auth import cookie_signer


def test_token_minted_logged_out_is_rejected_against_a_logged_in_session(client):
    """The confirmed #57 exploit: mint a token from a logged-out GET
    /auth/login, then replay it against an authenticated POST."""
    logged_out_token = generate_csrf_token(client.cookies.get("session"))

    user_id = create_user("victim@example.com", "Victim", "password123")
    client.cookies.set("session", cookie_signer.dumps({"user_id": user_id, "sv": 0}))

    resp = client.post("/partners/new", data={
        "name": "Forged Partner", "csrf_token": logged_out_token,
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/partners/new"  # re-shown, not saved


def test_token_minted_for_one_session_is_rejected_for_another(client):
    user_a = create_user("a@example.com", "A", "password123")
    user_b = create_user("b@example.com", "B", "password123")

    token_a = generate_csrf_token(cookie_signer.dumps({"user_id": user_a}))
    assert not validate_csrf_token(token_a, cookie_signer.dumps({"user_id": user_b}))


def test_token_bound_to_current_session_is_accepted(logged_in_client):
    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post("/partners/new", data={
        "name": "Real Partner", "csrf_token": token,
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] != "/partners/new"


def test_cross_origin_post_with_session_cookie_is_blocked(logged_in_client):
    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post(
        "/partners/new",
        data={"name": "Evil Partner", "csrf_token": token},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_same_origin_post_with_session_cookie_is_allowed(logged_in_client):
    token = generate_csrf_token(logged_in_client.cookies.get("session"))
    resp = logged_in_client.post(
        "/partners/new",
        data={"name": "Fine Partner", "csrf_token": token},
        headers={"Origin": f"http://{logged_in_client.base_url.host}"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_cross_origin_post_without_session_cookie_is_not_blocked(client):
    """Bearer/key-authenticated routes (no session cookie) are untouched."""
    resp = client.post(
        "/api/v1/leads", json={"leads": []},
        headers={"Origin": "https://evil.example", "X-API-Key": "wrong"},
    )
    assert resp.status_code == 401  # rejected for a bad key, not by the origin guard


def _logout_csrf(html: str) -> str:
    marker = 'action="/auth/logout"'
    start = html.index(marker)
    chunk = html[start:start + 500]
    return chunk.split('name="csrf_token" value="')[1].split('"')[0]


def test_logout_is_post_only(logged_in_client):
    assert logged_in_client.get("/auth/logout", follow_redirects=False).status_code == 405
    missing = logged_in_client.post("/auth/logout", follow_redirects=False)
    assert missing.status_code == 303
    assert missing.headers["location"] == "/partners"
    assert logged_in_client.get("/partners").status_code == 200

    forged = logged_in_client.post(
        "/auth/logout", data={"csrf_token": "not-a-token"}, follow_redirects=False,
    )
    assert forged.status_code == 303
    assert forged.headers["location"] == "/partners"
    assert logged_in_client.get("/partners").status_code == 200

    page = logged_in_client.get("/partners")
    assert 'action="/auth/logout"' in page.text
    token = _logout_csrf(page.text)
    assert token
    resp = logged_in_client.post(
        "/auth/logout", data={"csrf_token": token}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    cleared = [
        h for h in resp.headers.get_list("set-cookie")
        if h.startswith("session=")
    ]
    assert cleared
    assert any("Max-Age=0" in h or "max-age=0" in h for h in cleared)


def test_login_then_logout_clears_client_cookie(client):
    """A cookie the TestClient stored from Set-Cookie is dropped on logout."""
    from app.services.auth import create_user, generate_csrf_token

    create_user("roundtrip@example.com", "Round", "password123")
    csrf = generate_csrf_token()
    login = client.post("/auth/login", data={
        "email": "roundtrip@example.com", "password": "password123", "csrf_token": csrf,
    }, follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/partners").status_code == 200
    token = _logout_csrf(client.get("/partners").text)
    resp = client.post("/auth/logout", data={"csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert client.get("/partners", follow_redirects=False).status_code == 303


def test_logged_out_binding_is_consistent_with_explicit_none():
    """Default arg (no session) and an explicit None must bind the same way."""
    assert validate_csrf_token(generate_csrf_token(), None)
    assert validate_csrf_token(generate_csrf_token(None))
