"""Lead and stages IP allowlists. Unset means no restriction. /mcp is not gated."""
from app.services.catalog import create_service
from app.services.client_ip import reset_trusted_proxies_cache


def _reset():
    reset_trusted_proxies_cache()


def test_unset_allowlist_does_not_block_leads(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.delenv("LEADS_IP_ALLOWLIST", raising=False)
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key"},
    )
    assert resp.status_code != 403


def test_leads_allowlist_rejects_other_ips_before_the_key(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.setenv("LEADS_IP_ALLOWLIST", "203.0.113.10")
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "ip_not_allowlisted"}


def test_leads_allowlist_accepts_a_forwarded_client(client, db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.setenv("TRUSTED_PROXIES", "testclient")
    monkeypatch.setenv("LEADS_IP_ALLOWLIST", "203.0.113.0/24")
    _reset()
    create_service("Consulting", "consulting")
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "Allowed", "email": "a@example.com", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key", "X-Real-IP": "203.0.113.10"},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "created"


def test_garbage_allowlist_fails_closed(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    monkeypatch.setenv("LEADS_IP_ALLOWLIST", "not-an-ip")
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers={"X-API-Key": "test-crm-api-key"},
    )
    assert resp.status_code == 403


def test_stages_allowlist_is_independent_of_leads(client, monkeypatch):
    monkeypatch.setenv("CRM_STAGES_API_KEY", "stages-key")
    monkeypatch.setenv("CRM_API_KEY", "leads-key")
    monkeypatch.setenv("LEADS_IP_ALLOWLIST", "203.0.113.10")
    monkeypatch.delenv("STAGES_IP_ALLOWLIST", raising=False)
    stages = client.get("/api/v1/stages", headers={"X-API-Key": "stages-key"})
    assert stages.status_code == 200

    monkeypatch.setenv("STAGES_IP_ALLOWLIST", "198.51.100.4")
    blocked = client.get("/api/v1/stages", headers={"X-API-Key": "stages-key"})
    assert blocked.status_code == 403
    assert blocked.json() == {"error": "ip_not_allowlisted"}

    # Leads list does not apply to /mcp.
    monkeypatch.setenv("CRM_MCP_API_KEY", "mcp-key")
    mcp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"X-API-Key": "mcp-key", "Accept": "application/json", "Content-Type": "application/json"},
    )
    assert mcp.status_code == 200
    assert mcp.json()["result"] == {}


def test_stages_write_is_allowlisted(client, monkeypatch):
    monkeypatch.setenv("CRM_STAGES_API_KEY", "stages-key")
    monkeypatch.setenv("STAGES_IP_ALLOWLIST", "203.0.113.10")
    resp = client.post(
        "/api/v1/stages",
        json={"key": "review", "label": "Review"},
        headers={"X-API-Key": "stages-key"},
    )
    assert resp.status_code == 403
