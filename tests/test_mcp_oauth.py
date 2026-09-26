import base64
import hashlib
import secrets

from app.services.auth import generate_csrf_token
from app.services import mcp_oauth


def _enable(monkeypatch, key="test-mcp-key"):
    monkeypatch.setenv("CRM_MCP_API_KEY", key)


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register(client, monkeypatch, redirect="http://127.0.0.1:9/cb"):
    _enable(monkeypatch)
    resp = client.post("/oauth/register", json={
        "client_name": "Grok",
        "redirect_uris": [redirect],
        "token_endpoint_auth_method": "none",
    })
    assert resp.status_code == 201, resp.text
    return resp.json(), redirect


def test_register_requires_mcp_enabled(client, db, monkeypatch):
    monkeypatch.delenv("CRM_MCP_API_KEY", raising=False)
    resp = client.post("/oauth/register", json={"redirect_uris": ["https://example.com/cb"]})
    assert resp.status_code == 503


def test_register_rejects_http_non_loopback(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post("/oauth/register", json={
        "redirect_uris": ["http://evil.example/cb"],
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_register_rejects_javascript_uri(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post("/oauth/register", json={
        "redirect_uris": ["javascript:alert(1)"],
    })
    assert resp.status_code == 400


def test_pkce_round_trip_can_call_mcp(client, db, monkeypatch):
    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    client_id = registered["client_id"]

    page = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    })
    assert page.status_code == 200
    assert "Authorize a bot" in page.text
    assert redirect in page.text
    assert client_id in page.text
    assert "the authorization code is sent to" in page.text

    submitted = client.post("/oauth/authorize", data={
        "csrf_token": generate_csrf_token(),
        "api_key": "test-mcp-key",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "crm",
    }, follow_redirects=False)
    assert submitted.status_code == 302
    location = submitted.headers["location"]
    assert location.startswith(redirect)
    assert "state=xyz" in location
    assert "code=" in location
    code = dict(part.split("=", 1) for part in location.split("?", 1)[1].split("&"))["code"]

    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    assert token.status_code == 200, token.text
    access = token.json()["access_token"]
    refresh = token.json()["refresh_token"]

    ping = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    assert ping.status_code == 200
    assert ping.json()["result"] == {}

    refreshed = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh,
    })
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


def test_token_rejects_wrong_verifier(client, db, monkeypatch):
    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    code = mcp_oauth.issue_auth_code(registered["client_id"], redirect, challenge)
    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": registered["client_id"],
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": "not-the-verifier-and-long-enough-to-look-real-xxxxx",
    })
    assert token.status_code == 400
    assert token.json()["error"] == "invalid_grant"


def test_authorize_unknown_client(client, db, monkeypatch):
    _enable(monkeypatch)
    page = client.get("/oauth/authorize", params={
        "client_id": "nope",
        "redirect_uri": "http://127.0.0.1:9/cb",
        "code_challenge": "abc",
        "code_challenge_method": "S256",
    })
    assert page.status_code == 400
    assert "Unknown client" in page.text


def test_logged_in_operator_can_authorize_without_pasting_key(client, db, monkeypatch):
    _enable(monkeypatch)
    from app.services.auth import create_user
    from app.routers.auth import cookie_signer

    user_id = create_user("test@example.com", "Test User", "password123")
    client.cookies.set("session", cookie_signer.dumps({"user_id": user_id}))
    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    submitted = client.post("/oauth/authorize", data={
        "csrf_token": generate_csrf_token(),
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "",
    }, follow_redirects=False)
    assert submitted.status_code == 302


def test_oauth_clients_table_exists(db):
    from app.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_oauth_clients'"
        ).fetchone()
    assert row is not None


def _authorize_form(registered, redirect, challenge, api_key="test-mcp-key", csrf=None):
    return {
        "csrf_token": csrf if csrf is not None else generate_csrf_token(),
        "api_key": api_key,
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "crm",
    }


def _handshake(client, monkeypatch):
    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    submitted = client.post(
        "/oauth/authorize",
        data=_authorize_form(registered, redirect, challenge),
        follow_redirects=False,
    )
    assert submitted.status_code == 302, submitted.text
    location = submitted.headers["location"]
    code = dict(part.split("=", 1) for part in location.split("?", 1)[1].split("&"))["code"]
    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": registered["client_id"],
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    assert token.status_code == 200, token.text
    return registered, redirect, token.json()


def test_authorize_get_does_not_count_toward_rate_limit(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    cap = RATE_LIMIT_MAX_BY_ACTION["oauth_authorize"]
    for _ in range(cap + 5):
        page = client.get("/oauth/authorize", params=params)
        assert page.status_code == 200, page.text
    wrong = client.post(
        "/oauth/authorize",
        data=_authorize_form(registered, redirect, challenge, api_key="wrong"),
    )
    assert wrong.status_code == 401


def test_authorize_post_wrong_key_is_rate_limited(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    cap = RATE_LIMIT_MAX_BY_ACTION["oauth_authorize"]
    for _ in range(cap):
        resp = client.post(
            "/oauth/authorize",
            data=_authorize_form(registered, redirect, challenge, api_key="wrong"),
        )
        assert resp.status_code == 401, resp.text
    blocked = client.post(
        "/oauth/authorize",
        data=_authorize_form(registered, redirect, challenge, api_key="wrong"),
    )
    assert blocked.status_code == 429
    page = client.get("/oauth/authorize", params={
        "client_id": registered["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    assert page.status_code == 200
    ok = client.post(
        "/oauth/authorize",
        data=_authorize_form(registered, redirect, challenge),
        follow_redirects=False,
    )
    assert ok.status_code == 302


def test_authorize_post_bad_csrf_is_rate_limited(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    verifier, challenge = _pkce()
    registered, redirect = _register(client, monkeypatch)
    cap = RATE_LIMIT_MAX_BY_ACTION["oauth_authorize"]
    for _ in range(cap):
        resp = client.post(
            "/oauth/authorize",
            data=_authorize_form(
                registered, redirect, challenge, csrf="not-a-csrf-token",
            ),
        )
        assert resp.status_code == 400, resp.text
    blocked = client.post(
        "/oauth/authorize",
        data=_authorize_form(
            registered, redirect, challenge, csrf="not-a-csrf-token",
        ),
    )
    assert blocked.status_code == 429
    ok = client.post(
        "/oauth/authorize",
        data=_authorize_form(registered, redirect, challenge),
        follow_redirects=False,
    )
    assert ok.status_code == 302


def test_failed_register_is_rate_limited(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    _enable(monkeypatch)
    cap = RATE_LIMIT_MAX_BY_ACTION["oauth_register"]
    payload = {"redirect_uris": ["http://evil.example/cb"]}
    for _ in range(cap):
        resp = client.post("/oauth/register", json=payload)
        assert resp.status_code == 400, resp.text
    blocked = client.post("/oauth/register", json=payload)
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "rate_limited"}
    ok = client.post("/oauth/register", json={
        "client_name": "Grok",
        "redirect_uris": ["http://127.0.0.1:9/cb"],
        "token_endpoint_auth_method": "none",
    })
    assert ok.status_code == 201, ok.text


def test_successful_register_does_not_fill_ip_failure_bucket(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    _enable(monkeypatch)
    unauth_cap = RATE_LIMIT_MAX_BY_ACTION["oauth_register"]
    for i in range(unauth_cap + 1):
        resp = client.post("/oauth/register", json={
            "client_name": f"Grok-{i}",
            "redirect_uris": ["http://127.0.0.1:9/cb"],
            "token_endpoint_auth_method": "none",
        })
        assert resp.status_code == 201, resp.text
    failed = client.post("/oauth/register", json={
        "redirect_uris": ["http://evil.example/cb"],
    })
    assert failed.status_code == 400


def test_failed_token_does_not_lock_out_valid_refresh(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    registered, _redirect, tokens = _handshake(client, monkeypatch)
    cap = RATE_LIMIT_MAX_BY_ACTION["oauth_token"]
    for _ in range(cap):
        resp = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": "not-a-refresh-token",
        })
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_grant"
    blocked = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": registered["client_id"],
        "refresh_token": "not-a-refresh-token",
    })
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "rate_limited"}
    assert "Retry-After" in blocked.headers
    ok = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "client_id": registered["client_id"],
        "refresh_token": tokens["refresh_token"],
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["access_token"]


def test_successful_token_refresh_uses_client_bucket(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    registered, _redirect, tokens = _handshake(client, monkeypatch)
    unauth_cap = RATE_LIMIT_MAX_BY_ACTION["oauth_token"]
    refresh = tokens["refresh_token"]
    for i in range(unauth_cap + 1):
        resp = client.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": refresh,
        })
        assert resp.status_code == 200, f"refresh {i + 1}: {resp.text}"
    failed = client.post("/oauth/token", data={
        "grant_type": "unsupported",
        "client_id": registered["client_id"],
    })
    assert failed.status_code == 400
    assert failed.json()["error"] == "unsupported_grant_type"


def test_well_known_metadata_is_not_rate_limited(client, db, monkeypatch):
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION, check_rate_limit

    _enable(monkeypatch)
    for action in ("oauth_register", "oauth_token", "oauth_authorize"):
        for _ in range(RATE_LIMIT_MAX_BY_ACTION[action]):
            assert check_rate_limit("testclient", action=action) is True
        assert check_rate_limit("testclient", action=action) is False
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        body = resp.json()
        assert body.get("issuer") or body.get("resource")
