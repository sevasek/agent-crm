"""Trusted-proxy client IP used as the rate-limit key.

Empty TRUSTED_PROXIES (the default) must ignore X-Forwarded-For / X-Real-IP
so a client cannot spoof their bucket. When the immediate peer is listed,
Traefik's X-Real-IP (or the right-most untrusted X-Forwarded-For hop) is
what get_rate_limit_key sees.
"""
from starlette.requests import Request

from app.services import auth as auth_service
from app.services.auth import get_rate_limit_key
from app.services.client_ip import get_client_ip, warn_if_non_ip_trusted_proxies


def _leads_api_limit():
    """Use the per-action cap when present (#11) so these tests survive merge."""
    by_action = getattr(auth_service, "RATE_LIMIT_MAX_BY_ACTION", None)
    if isinstance(by_action, dict) and "leads_api" in by_action:
        return by_action["leads_api"]
    return getattr(auth_service, "RATE_LIMIT_MAX", 5)


def _request(peer, headers=None):
    header_list = []
    if headers:
        for name, value in headers.items():
            header_list.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": header_list,
        "client": (peer, 12345) if peer is not None else None,
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_spoofed_xff_ignored_when_trusted_proxies_unset(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    req = _request("192.0.2.1", {
        "x-forwarded-for": "203.0.113.9, 198.51.100.2",
        "x-real-ip": "203.0.113.9",
    })
    assert get_client_ip(req) == "192.0.2.1"
    assert get_rate_limit_key(get_client_ip(req)) == "192.0.2.1"


def test_spoofed_xff_ignored_when_trusted_proxies_empty(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    req = _request("192.0.2.1", {"x-forwarded-for": "203.0.113.9"})
    assert get_client_ip(req) == "192.0.2.1"


def test_spoofed_xff_ignored_when_peer_is_not_trusted(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("192.0.2.1", {"x-forwarded-for": "203.0.113.9", "x-real-ip": "203.0.113.9"})
    assert get_client_ip(req) == "192.0.2.1"
    assert get_rate_limit_key(get_client_ip(req)) == "192.0.2.1"


def test_x_real_ip_used_when_peer_is_trusted(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2", {"x-real-ip": "203.0.113.9"})
    assert get_client_ip(req) == "203.0.113.9"
    assert get_rate_limit_key(get_client_ip(req)) == "203.0.113.9"


def test_rate_limit_key_uses_forwarded_ip_when_peer_trusted(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2", {"x-real-ip": "203.0.113.9"})
    assert get_rate_limit_key(get_client_ip(req), "importer@example.com") == "203.0.113.9:importer@example.com"


def test_xff_rightmost_untrusted_hop_not_leftmost_spoof(monkeypatch):
    """A client-prepended X-Forwarded-For[0] must not become the key."""
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2", {"x-forwarded-for": "1.2.3.4, 203.0.113.50"})
    assert get_client_ip(req) == "203.0.113.50"
    assert get_rate_limit_key(get_client_ip(req)) == "203.0.113.50"


def test_xff_skips_trusted_hops_from_the_right(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2, 10.0.0.3")
    req = _request("10.0.0.2", {"x-forwarded-for": "1.2.3.4, 203.0.113.50, 10.0.0.3"})
    assert get_client_ip(req) == "203.0.113.50"


def test_x_real_ip_preferred_over_spoofed_xff(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2", {
        "x-forwarded-for": "1.2.3.4, 198.51.100.7",
        "x-real-ip": "203.0.113.9",
    })
    assert get_client_ip(req) == "203.0.113.9"


def test_invalid_x_real_ip_falls_through_to_xff(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2", {
        "x-real-ip": "not-an-ip",
        "x-forwarded-for": "1.2.3.4, 203.0.113.50",
    })
    assert get_client_ip(req) == "203.0.113.50"


def test_trusted_peer_without_forwarding_headers_stays_peer(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request("10.0.0.2")
    assert get_client_ip(req) == "10.0.0.2"


def test_missing_client_is_unknown(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    req = _request(None, {"x-real-ip": "203.0.113.9"})
    assert get_client_ip(req) == "unknown"


def test_trusted_proxies_whitespace_and_commas(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", " 10.0.0.2 , 10.0.0.3 ")
    req = _request("10.0.0.3", {"x-real-ip": "203.0.113.9"})
    assert get_client_ip(req) == "203.0.113.9"


def test_leads_api_spoofed_xff_shares_one_bucket_when_untrusted(client, monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    payload = {"leads": [{"name": "X", "email": "x@example.com", "service_slug": "none"}]}
    headers_base = {"X-API-Key": "test-crm-api-key"}

    for i in range(_leads_api_limit()):
        resp = client.post(
            "/api/v1/leads", json=payload,
            headers={**headers_base, "X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert resp.status_code != 429, resp.text

    resp = client.post(
        "/api/v1/leads", json=payload,
        headers={**headers_base, "X-Forwarded-For": "198.51.100.1"},
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited"}


def test_leads_api_forwarded_ip_is_rate_limit_key_when_trusted(client, monkeypatch):
    # Starlette 0.38 TestClient hardcodes scope["client"] as ("testclient", 50000).
    monkeypatch.setenv("TRUSTED_PROXIES", "testclient")
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    payload = {"leads": [{"name": "X", "email": "x@example.com", "service_slug": "none"}]}

    for _ in range(_leads_api_limit()):
        resp = client.post(
            "/api/v1/leads", json=payload,
            headers={"X-API-Key": "test-crm-api-key", "X-Real-IP": "203.0.113.9"},
        )
        assert resp.status_code != 429, resp.text

    blocked = client.post(
        "/api/v1/leads", json=payload,
        headers={"X-API-Key": "test-crm-api-key", "X-Real-IP": "203.0.113.9"},
    )
    assert blocked.status_code == 429

    other = client.post(
        "/api/v1/leads", json=payload,
        headers={"X-API-Key": "test-crm-api-key", "X-Real-IP": "198.51.100.7"},
    )
    assert other.status_code != 429


def test_xff_all_trusted_hops_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2, 10.0.0.3")
    req = _request("10.0.0.2", {"x-forwarded-for": "10.0.0.3"})
    assert get_client_ip(req) == "10.0.0.2"
    req = _request("10.0.0.2", {"x-forwarded-for": "10.0.0.2, 10.0.0.3"})
    assert get_client_ip(req) == "10.0.0.2"
    req = _request("10.0.0.2", {"x-forwarded-for": "not-an-ip, also-bad"})
    assert get_client_ip(req) == "10.0.0.2"


def test_x_real_ip_that_is_a_trusted_hop_is_ignored(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2, 10.0.0.3")
    req = _request("10.0.0.2", {"x-real-ip": "10.0.0.3"})
    assert get_client_ip(req) == "10.0.0.2"
    req = _request("10.0.0.2", {
        "x-real-ip": "10.0.0.3",
        "x-forwarded-for": "203.0.113.9, 10.0.0.3",
    })
    assert get_client_ip(req) == "203.0.113.9"


def test_hostname_trusted_proxy_entry_is_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("TRUSTED_PROXIES", "traefik")
    with caplog.at_level(logging.WARNING, logger="app.services.client_ip"):
        warn_if_non_ip_trusted_proxies()
    assert "not IP addresses" in caplog.text
    assert "traefik" in caplog.text
    req = _request("10.0.0.2", {"x-real-ip": "203.0.113.9"})
    assert get_client_ip(req) == "10.0.0.2"


def test_ip_trusted_proxy_entry_does_not_warn(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.2")
    with caplog.at_level(logging.WARNING, logger="app.services.client_ip"):
        warn_if_non_ip_trusted_proxies()
    assert caplog.text == ""
