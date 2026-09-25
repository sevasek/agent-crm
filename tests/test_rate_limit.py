import time
from collections import deque

from app.services.auth import (
    RATE_LIMIT_MAX_AUTH_BY_ACTION,
    RATE_LIMIT_MAX_BY_ACTION,
    check_rate_limit,
    clear_rate_limits,
    env_key_rate_limit_identity,
)


def _hits(bucket):
    from app.services import auth as auth_service

    with auth_service._rate_limit_lock:
        return list(auth_service._rate_limit_hits.get(bucket, ()))


def _insert_hits(bucket, times):
    from app.services import auth as auth_service

    with auth_service._rate_limit_lock:
        auth_service._rate_limit_hits[bucket] = deque(times)


def test_leads_api_allows_30_then_rejects_31st(client):
    key = "10.0.0.1"
    for i in range(30):
        assert check_rate_limit(key, action="leads_api") is True, f"call {i + 1} should pass"
    assert check_rate_limit(key, action="leads_api") is False


def test_login_allows_5_then_rejects_6th(client):
    key = "10.0.0.1:brute@example.com"
    for i in range(5):
        assert check_rate_limit(key, action="login") is True, f"call {i + 1} should pass"
    assert check_rate_limit(key, action="login") is False


def test_default_action_allows_5_then_rejects_6th(client):
    key = "10.0.0.1"
    for i in range(5):
        assert check_rate_limit(key) is True, f"call {i + 1} should pass"
    assert check_rate_limit(key) is False


def test_mcp_api_allows_120_then_rejects_121st(client):
    key = "10.0.0.21"
    for i in range(120):
        assert check_rate_limit(key, action="mcp_api") is True, f"call {i + 1} should pass"
    assert check_rate_limit(key, action="mcp_api") is False


def test_mcp_and_leads_api_buckets_are_independent(client):
    key = "10.0.0.22"
    for _ in range(30):
        assert check_rate_limit(key, action="leads_api") is True
    assert check_rate_limit(key, action="leads_api") is False
    assert check_rate_limit(key, action="mcp_api") is True


def test_stages_api_allows_30_then_rejects_31st(client):
    key = "10.0.0.2"
    for i in range(30):
        assert check_rate_limit(key, action="stages_api") is True, f"call {i + 1} should pass"
    assert check_rate_limit(key, action="stages_api") is False


def test_leads_and_stages_api_buckets_are_independent(client):
    key = "10.0.0.3"
    for _ in range(30):
        assert check_rate_limit(key, action="leads_api") is True
    assert check_rate_limit(key, action="leads_api") is False
    for _ in range(30):
        assert check_rate_limit(key, action="stages_api") is True
    assert check_rate_limit(key, action="stages_api") is False


def test_login_and_leads_api_buckets_are_independent(client):
    key = "10.0.0.1"
    for _ in range(30):
        assert check_rate_limit(key, action="leads_api") is True
    assert check_rate_limit(key, action="leads_api") is False

    for _ in range(5):
        assert check_rate_limit(key, action="login") is True
    assert check_rate_limit(key, action="login") is False


def test_client_fixture_clears_rate_limit_state(client):
    """conftest's client fixture calls clear_rate_limits() before each test."""
    from app.services import auth as auth_service

    with auth_service._rate_limit_lock:
        assert len(auth_service._rate_limit_hits) == 0
    assert check_rate_limit("10.0.0.1", action="leads_api") is True


def test_idle_rate_limit_buckets_are_dropped(client):
    from app.services import auth as auth_service

    old = time.time() - auth_service.RATE_LIMIT_WINDOW - 1
    _insert_hits("leads_api:10.0.0.99", [old])
    assert check_rate_limit("10.0.0.1", action="leads_api") is True
    with auth_service._rate_limit_lock:
        assert "leads_api:10.0.0.99" not in auth_service._rate_limit_hits


def test_leads_api_429_includes_retry_after(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    identity = env_key_rate_limit_identity("CRM_API_KEY")
    auth_cap = RATE_LIMIT_MAX_AUTH_BY_ACTION["leads_api"]
    for _ in range(auth_cap):
        assert check_rate_limit(identity, action="leads_api", authenticated=True) is True

    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key"},
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited"}
    retry_after = int(resp.headers.get("Retry-After"))
    assert 1 <= retry_after <= 60


def test_retry_after_matches_sliding_window_remaining(client, monkeypatch):
    from app.services import auth as auth_service

    now = 1_700_000_000.0
    monkeypatch.setattr(auth_service.time, "time", lambda: now)
    ip = "10.0.0.9"
    _insert_hits("leads_api:" + ip, [now - 50] * 30)
    allowed, retry_after = auth_service.check_rate_limit_retry(ip, action="leads_api")
    assert allowed is False
    assert retry_after == 10


def test_empty_rate_limit_keys_are_evicted(client, monkeypatch):
    from app.services import auth as auth_service

    now = 1_800_000_000.0
    monkeypatch.setattr(auth_service.time, "time", lambda: now)
    _insert_hits("leads_api:stale", [now - 120])
    assert check_rate_limit("stale", action="leads_api") is True
    assert _hits("leads_api:stale") == [now]


def test_clear_rate_limits_deletes_rows(client):
    assert check_rate_limit("10.0.0.4", action="leads_api") is True
    assert _hits("leads_api:10.0.0.4")
    clear_rate_limits()
    assert _hits("leads_api:10.0.0.4") == []


def test_unknown_action_uses_default_cap(client):
    key = "10.0.0.8"
    default_cap = RATE_LIMIT_MAX_BY_ACTION["default"]
    for i in range(default_cap):
        assert check_rate_limit(key, action="not-a-real-action") is True, i
    assert check_rate_limit(key, action="not-a-real-action") is False


def test_failed_mcp_auth_does_not_lock_out_valid_key(client, monkeypatch):
    monkeypatch.setenv("CRM_MCP_API_KEY", "real-mcp-key")
    unauth_cap = RATE_LIMIT_MAX_BY_ACTION["mcp_api"]
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for _ in range(unauth_cap):
        resp = client.post(
            "/mcp",
            json=ping,
            headers={"Authorization": "Bearer wrong", "Accept": "application/json"},
        )
        assert resp.status_code == 401, resp.text
    blocked = client.post(
        "/mcp",
        json=ping,
        headers={"Authorization": "Bearer wrong", "Accept": "application/json"},
    )
    assert blocked.status_code == 429
    allowed = client.post(
        "/mcp",
        json=ping,
        headers={"X-API-Key": "real-mcp-key", "Accept": "application/json"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["result"] == {}


def test_failed_leads_auth_does_not_lock_out_valid_key(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "real-leads-key")
    from app.services.catalog import create_service

    create_service("Consulting", "consulting")
    payload = {"leads": [{"name": "X", "email": "x@example.com", "service_slug": "consulting"}]}
    unauth_cap = RATE_LIMIT_MAX_BY_ACTION["leads_api"]
    for _ in range(unauth_cap):
        resp = client.post("/api/v1/leads", json=payload, headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401, resp.text
    blocked = client.post("/api/v1/leads", json=payload, headers={"X-API-Key": "wrong"})
    assert blocked.status_code == 429
    allowed = client.post(
        "/api/v1/leads", json=payload, headers={"X-API-Key": "real-leads-key"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["results"][0]["status"] == "created"


def test_authenticated_mcp_bucket_is_larger_than_unauth(client):
    identity = env_key_rate_limit_identity("CRM_MCP_API_KEY")
    unauth_cap = RATE_LIMIT_MAX_BY_ACTION["mcp_api"]
    auth_cap = RATE_LIMIT_MAX_AUTH_BY_ACTION["mcp_api"]
    assert auth_cap > unauth_cap
    for i in range(auth_cap):
        assert check_rate_limit(identity, action="mcp_api", authenticated=True) is True, i
    assert check_rate_limit(identity, action="mcp_api", authenticated=True) is False
    # Filling the auth bucket must not fill the IP guess bucket.
    assert check_rate_limit("10.0.0.9", action="mcp_api") is True


def test_login_succeeds_after_failed_attempts(client):
    from app.services.auth import create_user, generate_csrf_token

    create_user("op@example.com", "Op", "correct-horse")
    for _ in range(5):
        resp = client.post(
            "/auth/login",
            data={
                "email": "op@example.com",
                "password": "wrong-password",
                "csrf_token": generate_csrf_token(),
            },
        )
        assert resp.status_code == 400
        assert "Invalid email or password" in resp.text
    ok = client.post(
        "/auth/login",
        data={
            "email": "op@example.com",
            "password": "correct-horse",
            "csrf_token": generate_csrf_token(),
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/partners"


def test_sixth_failed_login_is_429(client):
    from app.services.auth import create_user, generate_csrf_token

    create_user("op@example.com", "Op", "correct-horse")
    for _ in range(5):
        resp = client.post(
            "/auth/login",
            data={
                "email": "op@example.com",
                "password": "wrong-password",
                "csrf_token": generate_csrf_token(),
            },
        )
        assert resp.status_code == 400
    blocked = client.post(
        "/auth/login",
        data={
            "email": "op@example.com",
            "password": "wrong-password",
            "csrf_token": generate_csrf_token(),
        },
    )
    assert blocked.status_code == 429


def test_health_is_exempt_from_rate_limiter(client):
    for action, cap in RATE_LIMIT_MAX_BY_ACTION.items():
        for _ in range(cap):
            check_rate_limit("testclient", action=action)
        assert check_rate_limit("testclient", action=action) is False
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_rate_limiter_does_not_write_sqlite(client):
    from app.database import get_db

    assert check_rate_limit("10.0.0.5", action="leads_api") is True
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM rate_limit_hits").fetchone()["c"]
    assert n == 0
