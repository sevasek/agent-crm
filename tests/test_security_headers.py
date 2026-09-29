import pytest
from fastapi.testclient import TestClient

from app.services.catalog import create_service


def _assert_docs_are_404(c):
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
    assert c.get("/redoc").status_code == 404


def test_docs_available_in_local_dev(client):
    resp = client.get("/docs")
    assert resp.status_code == 200


def test_docs_and_openapi_disabled_when_secure_cookies(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from app.main import create_app
    with TestClient(create_app()) as c:
        _assert_docs_are_404(c)


def test_docs_disabled_when_base_url_is_https(db, monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    from app.main import create_app
    with TestClient(create_app()) as c:
        _assert_docs_are_404(c)


def test_docs_disabled_when_secure_cookies_false_but_https_base_url(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    from app.main import create_app
    with TestClient(create_app()) as c:
        _assert_docs_are_404(c)


def test_docs_disabled_when_crm_api_key_set(db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "not-empty")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    from app.main import create_app
    with TestClient(create_app()) as c:
        _assert_docs_are_404(c)


def test_docs_disabled_when_disable_docs_true(db, monkeypatch):
    monkeypatch.setenv("DISABLE_DOCS", "true")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    from app.main import create_app
    with TestClient(create_app()) as c:
        _assert_docs_are_404(c)


def test_docs_available_when_disable_docs_false_even_with_https(db, monkeypatch):
    monkeypatch.setenv("DISABLE_DOCS", "false")
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    from app.main import create_app
    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 200


def test_refuses_insecure_secret_when_secure_cookies(db, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from app.main import create_app
    with pytest.raises(RuntimeError, match="insecure default"):
        with TestClient(create_app()):
            pass


def test_refuses_insecure_secret_when_https_base_url(db, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    from app.main import create_app
    with pytest.raises(RuntimeError, match="insecure default"):
        with TestClient(create_app()):
            pass


def test_insecure_secret_warns_in_local_dev(db, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-prod")
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    from app.main import create_app
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(create_app()) as c:
            assert c.get("/health").status_code == 200
    assert "insecure default" in caplog.text


def _csp_directive(csp: str, name: str) -> str | None:
    for part in csp.split(";"):
        part = part.strip()
        if part == name or part.startswith(name + " "):
            return part
    return None


def _assert_baseline_security_headers(resp, *, hsts: bool = False):
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    assert _csp_directive(csp, "default-src") == "default-src 'self'"
    assert _csp_directive(csp, "script-src") == "script-src 'self'"
    assert "unsafe-inline" not in _csp_directive(csp, "script-src")
    assert _csp_directive(csp, "style-src").startswith("style-src 'self'")
    assert "cdn" not in csp.lower()
    assert "http:" not in csp
    assert "https:" not in csp
    if hsts:
        assert resp.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    else:
        assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}


def test_health_includes_nosniff_and_does_not_require_login(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    _assert_baseline_security_headers(resp, hsts=False)


def test_health_returns_503_when_db_check_fails(db, monkeypatch, caplog):
    import logging
    from contextlib import contextmanager

    @contextmanager
    def failing_db(*args, **kwargs):
        raise RuntimeError("attempt to write a readonly database")
        yield None

    monkeypatch.setattr("app.main.get_db", failing_db)
    monkeypatch.setattr("app.main._health_db_warned", False)
    from app.main import create_app
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(create_app()) as c:
            resp = c.get("/health")
            again = c.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}
    assert again.status_code == 503
    assert "database check failed" in caplog.text
    assert "Traceback" not in caplog.text


def test_health_returns_503_when_db_file_is_readonly(client, tmp_path, monkeypatch):
    import os
    import stat

    from app import database

    monkeypatch.setattr("app.main._health_db_warned", False)
    paths = [database.DB_PATH]
    for suffix in ("-wal", "-shm"):
        extra = database.DB_PATH + suffix
        if os.path.exists(extra):
            paths.append(extra)
    for path in paths:
        os.chmod(path, stat.S_IREAD)
    os.chmod(tmp_path, stat.S_IREAD | stat.S_IEXEC)
    try:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json() == {"status": "unavailable"}
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)
        for path in paths:
            if os.path.exists(path):
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_login_includes_nosniff(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    _assert_baseline_security_headers(resp, hsts=False)


def test_login_loads_call_tap_from_static_not_inline(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'src="/static/call-tap.js"' in resp.text
    assert 'src="/static/app.js"' in resp.text
    assert "function logCallTap" not in resp.text
    js = client.get("/static/call-tap.js")
    assert js.status_code == 200
    assert "function logCallTap" in js.text
    assert "<!DOCTYPE" not in js.text[:80]


def test_hsts_not_sent_on_plain_http_localhost(client):
    resp = client.get("/health")
    assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}


def test_hsts_sent_when_secure_cookies(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    from app.main import create_app
    with TestClient(create_app()) as c:
        resp = c.get("/health")
        _assert_baseline_security_headers(resp, hsts=True)


def test_hsts_sent_when_base_url_is_https(db, monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    from app.main import create_app
    with TestClient(create_app()) as c:
        resp = c.get("/health")
        _assert_baseline_security_headers(resp, hsts=True)


def test_hsts_not_sent_when_secure_cookies_false_and_http_base_url(db, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "false")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    from app.main import create_app
    with TestClient(create_app()) as c:
        resp = c.get("/health")
        _assert_baseline_security_headers(resp, hsts=False)


def test_non_api_unhandled_exception_still_has_security_headers(db, monkeypatch):
    from app.main import create_app
    application = create_app()

    @application.get("/__boom")
    async def boom():
        raise RuntimeError("html crash must not skip headers")

    with TestClient(application) as c:
        resp = c.get("/__boom")
    assert resp.status_code == 500
    _assert_baseline_security_headers(resp, hsts=False)
    assert "html crash" not in resp.text.lower()
    assert "traceback" not in resp.text.lower()


def test_api_unhandled_exception_returns_json_server_error(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")

    def boom(*args, **kwargs):
        raise RuntimeError("secret traceback should not leak")

    monkeypatch.setattr("app.routers.api.ingest_lead", boom)
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "email": "x@example.com", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key"},
    )
    assert resp.status_code == 500
    assert resp.json() == {"error": "server_error"}
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    body = resp.text.lower()
    assert "traceback" not in body
    assert "runtimeerror" not in body
    assert "secret traceback" not in body


def test_mcp_still_works_when_docs_disabled(client, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    monkeypatch.setenv("CRM_MCP_API_KEY", "test-mcp-key")
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 404
        resp = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"X-API-Key": "test-mcp-key", "Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {}


def test_leads_route_still_works_when_docs_disabled(client, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as c:
        create_service("Consulting", "consulting")
        resp = c.post(
            "/api/v1/leads",
            json={"leads": [{
                "name": "Jane Doe",
                "email": "jane@acme.example",
                "service_slug": "consulting",
            }]},
            headers={"X-API-Key": "test-crm-api-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["status"] == "created"


def test_admin_bookmark_redirects_to_flat_path(client):
    resp = client.get("/admin/partners", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/partners"


def test_admin_root_redirects_to_partners(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/partners"


def test_admin_redirect_keeps_query_string(client):
    resp = client.get("/admin/deals?due=1", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/deals?due=1"


def test_admin_redirect_rejects_protocol_relative_target(client):
    from app.main import compat_admin_redirect_target

    assert compat_admin_redirect_target("/evil.example") == "/partners"
    assert compat_admin_redirect_target("//evil.example") == "/partners"
    assert compat_admin_redirect_target("https://evil.example") == "/partners"
    assert compat_admin_redirect_target("deals") == "/deals"
    assert compat_admin_redirect_target("deals", "due=1") == "/deals?due=1"

    resp = client.get("/admin//evil.example", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/partners"
    assert not resp.headers["location"].startswith("//")
