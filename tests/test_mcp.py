def _headers(key="test-mcp-key"):
    return {
        "X-API-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _enable(monkeypatch, key="test-mcp-key"):
    monkeypatch.setenv("CRM_MCP_API_KEY", key)


def rpc(client, method, params=None, rpc_id=1, key="test-mcp-key", extra_headers=None):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = _headers(key)
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/mcp", json=body, headers=headers)


def test_mcp_requires_api_key(client, db):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid_api_key"}
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")
    assert "oauth-protected-resource" in resp.headers["WWW-Authenticate"]


def test_mcp_fails_closed_when_key_unset(client, db, monkeypatch):
    monkeypatch.delenv("CRM_MCP_API_KEY", raising=False)
    resp = rpc(client, "ping")
    assert resp.status_code == 401


def test_leads_api_key_does_not_authorize_mcp(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "ingest-only")
    monkeypatch.setenv("CRM_STAGES_API_KEY", "stages-only")
    _enable(monkeypatch, "mcp-only")
    resp = rpc(client, "ping", key="ingest-only")
    assert resp.status_code == 401
    resp = rpc(client, "ping", key="stages-only")
    assert resp.status_code == 401


def test_query_string_key_does_not_authenticate(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        params={"api_key": "test-mcp-key"},
    )
    assert resp.status_code == 401


def test_bearer_token_authenticates(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={
            "Authorization": "Bearer test-mcp-key",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {}


def test_wrong_length_key_is_401_not_500(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = rpc(client, "ping", key="x")
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid_api_key"}


def test_initialize_returns_instructions_and_tools_capability(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "crm"
    assert "Search before creating" in result["instructions"]
    assert "parent_deal_id" in result["instructions"]
    assert "tools" in result["capabilities"]
    assert "resources" in result["capabilities"]
    assert "prompts" in result["capabilities"]


def test_initialized_notification_is_202(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers(),
    )
    assert resp.status_code == 202


def test_tools_list_includes_bot_safety_descriptions(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = rpc(client, "tools/list")
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    assert "search_partners" in tools
    assert "ingest_leads" in tools
    assert "create_offer" in tools
    assert "bulk_update_deals" in tools
    assert "set_deal_owner" in tools
    assert "create_delegated_task" in tools
    assert "create_service" in tools
    assert "update_service" in tools
    assert "list_delegated_tasks" in tools
    assert "get_delegated_task" in tools
    assert "complete_delegated_task" in tools
    assert tools["search_partners"]["annotations"]["readOnlyHint"] is True
    assert tools["ingest_leads"]["annotations"]["idempotentHint"] is True
    assert "delete" not in tools
    assert "reorder" not in " ".join(tools)
    assert "search before" in tools["create_partner"]["description"].lower() or (
        "after search_partners" in tools["create_partner"]["description"]
    )
    assert tools["update_partner"]["annotations"]["readOnlyHint"] is False
    assert "parent_deal_id" in tools["list_deals"]["description"]
    assert "NURTURE_WEBHOOK_URL" in tools["set_deal_stage"]["description"]
    assert "DEAL_WON_WEBHOOK_URL" in tools["set_deal_stage"]["description"]


def test_unknown_method_is_jsonrpc_error_not_http_404(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = rpc(client, "nope/nope")
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601


def test_malformed_json_is_422(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.post("/mcp", content=b"{not-json", headers=_headers())
    assert resp.status_code == 422
    assert resp.json() == {"error": "invalid_json"}


def test_get_mcp_is_405(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = client.get("/mcp", headers=_headers())
    assert resp.status_code == 405


def test_resources_and_prompts_list(client, db, monkeypatch):
    _enable(monkeypatch)
    resources = rpc(client, "resources/list").json()["result"]["resources"]
    uris = {r["uri"] for r in resources}
    assert "crm://instructions" in uris
    assert "crm://catalog/stages" in uris
    prompts = rpc(client, "prompts/list").json()["result"]["prompts"]
    names = {p["name"] for p in prompts}
    assert "work_the_queue" in names


def test_read_instructions_resource(client, db, monkeypatch):
    _enable(monkeypatch)
    resp = rpc(client, "resources/read", {"uri": "crm://instructions"})
    text = resp.json()["result"]["contents"][0]["text"]
    assert "ingest_leads" in text


def test_mcp_429_includes_retry_after(client, monkeypatch):
    monkeypatch.setenv("CRM_MCP_API_KEY", "test-mcp-key")
    from app.services.auth import (
        RATE_LIMIT_MAX_AUTH_BY_ACTION,
        check_rate_limit,
        env_key_rate_limit_identity,
    )

    identity = env_key_rate_limit_identity("CRM_MCP_API_KEY")
    for _ in range(RATE_LIMIT_MAX_AUTH_BY_ACTION["mcp_api"]):
        assert check_rate_limit(identity, action="mcp_api", authenticated=True) is True
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"X-API-Key": "test-mcp-key", "Accept": "application/json"},
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited"}
    assert 1 <= int(resp.headers.get("Retry-After")) <= 60


def test_mcp_401_still_has_security_headers(client, db):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_well_known_metadata_is_public(client, db, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://crm.example.com")
    as_meta = client.get("/.well-known/oauth-authorization-server")
    assert as_meta.status_code == 200
    body = as_meta.json()
    assert body["issuer"] == "https://crm.example.com"
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert "S256" in body["code_challenge_methods_supported"]
    resource = client.get("/.well-known/oauth-protected-resource")
    assert resource.json()["resource"] == "https://crm.example.com/mcp"
    assert resource.headers["Access-Control-Allow-Origin"] == "*"
