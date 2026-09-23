from app.database import get_db
from app.services.auth import check_rate_limit, clear_rate_limits


def _hits(bucket):
    with get_db() as conn:
        return [
            row["hit_at"]
            for row in conn.execute(
                "SELECT hit_at FROM rate_limit_hits WHERE bucket = ? ORDER BY hit_at",
                (bucket,),
            ).fetchall()
        ]


def _insert_hits(bucket, times):
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO rate_limit_hits (bucket, hit_at) VALUES (?, ?)",
            [(bucket, t) for t in times],
        )
        conn.commit()


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
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM rate_limit_hits").fetchone()["c"]
    assert n == 0
    assert check_rate_limit("10.0.0.1", action="leads_api") is True


def test_leads_api_429_includes_retry_after(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    ip = "testclient"
    for _ in range(30):
        assert check_rate_limit(ip, action="leads_api") is True

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
    from app.services.auth import RATE_LIMIT_MAX_BY_ACTION

    key = "10.0.0.8"
    default_cap = RATE_LIMIT_MAX_BY_ACTION["default"]
    for i in range(default_cap):
        assert check_rate_limit(key, action="not-a-real-action") is True, i
    assert check_rate_limit(key, action="not-a-real-action") is False
