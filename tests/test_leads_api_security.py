import socket
import threading
import time

import pytest
import uvicorn

from app.routers.api import MAX_BODY_BYTES, MAX_LEADS
from app.services.catalog import create_service
from app.services.partners import get_partner_by_email


def _headers(key="test-crm-api-key"):
    return {"X-API-Key": key}


def test_wrong_length_api_key_is_401_not_500(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    payload = {"leads": [{"name": "X", "service_slug": "consulting"}]}

    short = client.post("/api/v1/leads", json=payload, headers=_headers("x"))
    assert short.status_code == 401
    assert short.json() == {"error": "invalid_api_key"}

    long_key = client.post(
        "/api/v1/leads",
        json=payload,
        headers=_headers("test-crm-api-key-plus-extra"),
    )
    assert long_key.status_code == 401
    assert long_key.json() == {"error": "invalid_api_key"}


def test_missing_api_key_header_rejected(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid_api_key"}


def test_missing_api_key_env_fails_closed(client, monkeypatch):
    monkeypatch.delenv("CRM_API_KEY", raising=False)
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers=_headers("anything"),
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid_api_key"}


def test_query_string_api_key_does_not_authenticate(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    payload = {"leads": [{"name": "X", "service_slug": "consulting"}]}

    for params in (
        {"api_key": "test-crm-api-key"},
        {"apikey": "test-crm-api-key"},
        {"api-key": "test-crm-api-key"},
        {"X-API-Key": "test-crm-api-key"},
    ):
        resp = client.post("/api/v1/leads", json=payload, params=params)
        assert resp.status_code == 401, params
        assert resp.json() == {"error": "invalid_api_key"}


def test_query_string_api_key_ignored_when_header_present(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post(
        "/api/v1/leads",
        json={"leads": [{"name": "X", "service_slug": "consulting"}]},
        headers=_headers(),
        params={"api_key": "wrong-query-key"},
    )
    # Query string must not override / break a valid header.
    assert resp.status_code != 401


def test_invalid_json_is_422(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post(
        "/api/v1/leads",
        content=b"{not json",
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json() == {"error": "invalid_json"}


def test_non_json_content_type_rejected(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post(
        "/api/v1/leads",
        content=b'{"leads":[{"name":"X","service_slug":"consulting"}]}',
        headers={**_headers(), "Content-Type": "text/plain"},
    )
    assert resp.status_code == 415


def test_too_many_leads_rejected_before_ingest(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    leads = [
        {"name": f"Lead {i}", "email": f"lead{i}@example.com", "service_slug": "consulting"}
        for i in range(MAX_LEADS + 1)
    ]
    resp = client.post("/api/v1/leads", json={"leads": leads}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json() == {"error": "too_many_leads"}
    assert get_partner_by_email("lead0@example.com") is None


def test_max_batch_size_allowed(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    leads = [{"name": "X", "service_slug": "does-not-exist"}] * MAX_LEADS
    resp = client.post("/api/v1/leads", json={"leads": leads}, headers=_headers())
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == MAX_LEADS


def test_empty_leads_list_still_rejected(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post("/api/v1/leads", json={"leads": []}, headers=_headers())
    assert resp.status_code == 422
    assert resp.json() == {"error": "empty_leads_list"}


def test_oversized_body_is_413(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    resp = client.post(
        "/api/v1/leads",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json() == {"error": "payload_too_large"}


def test_non_ascii_api_key_compare_does_not_raise(db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    from app.routers.api import _check_api_key

    assert _check_api_key("café") is False


def test_non_dict_lead_item_is_invalid_not_500(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    create_service("Consulting", "consulting")
    resp = client.post(
        "/api/v1/leads",
        json={
            "leads": [
                "not-a-dict",
                {
                    "name": "Jane Doe",
                    "email": "jane@acme.example",
                    "service_slug": "consulting",
                },
                42,
                None,
            ]
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "invalid"
    assert results[1]["email"] == "jane@acme.example"
    assert results[1]["status"] == "created"
    assert results[1]["partner_id"]
    assert results[2]["status"] == "invalid"
    assert results[3]["status"] == "invalid"
    assert get_partner_by_email("jane@acme.example") is not None


def test_non_dict_json_body_is_invalid_body_shape(client, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    headers = {**_headers(), "Content-Type": "application/json"}
    for content in (b"[1,2,3]", b'"a string"', b"42", b"null", b"true"):
        resp = client.post("/api/v1/leads", content=content, headers=headers)
        assert resp.status_code == 422, content
        assert resp.json() == {"error": "invalid_body_shape"}, content


def _read_http_response(sock, timeout=2.0):
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    header, sep, body = bytes(buf).partition(b"\r\n\r\n")
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return header + sep + body


def _assert_reuse_does_not_hang(sock):
    sock.settimeout(2.0)
    second = (
        b"POST /api/v1/leads HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 12\r\n"
        b"X-API-Key: test-crm-api-key\r\n"
        b"\r\n"
        b'{"leads":[]}'
    )
    try:
        sock.sendall(second)
    except (BrokenPipeError, ConnectionError, OSError):
        return
    try:
        sock.recv(4096)
    except socket.timeout as exc:
        raise AssertionError("keep-alive reuse hung after early reject") from exc
    except (BrokenPipeError, ConnectionError, OSError):
        # Peer RST after Connection: close is a closed socket, not a hang.
        return


@pytest.fixture
def live_leads_port(db, monkeypatch):
    monkeypatch.setenv("CRM_API_KEY", "test-crm-api-key")
    from app.services import auth as auth_service
    auth_service.clear_rate_limits()
    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not server.started:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_oversized_content_length_does_not_poison_keepalive(live_leads_port):
    body = b"x" * (MAX_BODY_BYTES + 1)
    req = (
        b"POST /api/v1/leads HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"X-API-Key: test-crm-api-key\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        + body
    )
    sock = socket.create_connection(("127.0.0.1", live_leads_port), timeout=2)
    try:
        sock.sendall(req)
        resp = _read_http_response(sock)
        status = resp.split(b"\r\n", 1)[0]
        assert b"413" in status, resp[:200]
        assert b"connection: close" in resp.lower()
        _assert_reuse_does_not_hang(sock)
    finally:
        sock.close()


def test_wrong_content_type_does_not_poison_keepalive(live_leads_port):
    body = b'{"leads":[{"name":"X","service_slug":"consulting"}]}'
    req = (
        b"POST /api/v1/leads HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: text/plain\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"X-API-Key: test-crm-api-key\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        + body
    )
    sock = socket.create_connection(("127.0.0.1", live_leads_port), timeout=2)
    try:
        sock.sendall(req)
        resp = _read_http_response(sock)
        status = resp.split(b"\r\n", 1)[0]
        assert b"415" in status, resp[:200]
        assert b"connection: close" in resp.lower()
        _assert_reuse_does_not_hang(sock)
    finally:
        sock.close()


def test_mid_stream_body_cap_does_not_poison_keepalive(live_leads_port):
    # No Content-Length: uvicorn reads the stream until the cap, then we
    # abort without draining. Connection: close must still drop the socket.
    body = b"x" * (MAX_BODY_BYTES + 1)
    req = (
        b"POST /api/v1/leads HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"X-API-Key: test-crm-api-key\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        + f"{len(body):x}\r\n".encode()
        + body
        + b"\r\n0\r\n\r\n"
    )
    sock = socket.create_connection(("127.0.0.1", live_leads_port), timeout=2)
    try:
        sock.sendall(req)
        resp = _read_http_response(sock)
        status = resp.split(b"\r\n", 1)[0]
        assert b"413" in status, resp[:200]
        assert b"connection: close" in resp.lower()
        _assert_reuse_does_not_hang(sock)
    finally:
        sock.close()


def test_non_ascii_api_key_is_401_not_500(live_leads_port):
    body = b'{"leads":[{"name":"X","service_slug":"consulting"}]}'
    req = (
        b"POST /api/v1/leads HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"X-API-Key: "
        + "café".encode("latin-1")
        + b"\r\n\r\n"
        + body
    )
    sock = socket.create_connection(("127.0.0.1", live_leads_port), timeout=2)
    try:
        sock.sendall(req)
        resp = _read_http_response(sock)
        status = resp.split(b"\r\n", 1)[0]
        assert b" 401 " in status, resp[:300]
        assert b"invalid_api_key" in resp
    finally:
        sock.close()
