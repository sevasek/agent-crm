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


def test_health_includes_nosniff_and_does_not_require_login(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_login_includes_nosniff(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_non_api_unhandled_exception_still_has_security_headers(db, monkeypatch):
    from app.main import create_app
    application = create_app()

    @application.get("/__boom")
    async def boom():
        raise RuntimeError("html crash must not skip headers")

    with TestClient(application) as c:
        resp = c.get("/__boom")
    assert resp.status_code == 500
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
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
