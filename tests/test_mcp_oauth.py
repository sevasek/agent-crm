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
