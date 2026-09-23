from app.services.nurture import enroll_partner_in_nurture


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PARTNER = {"id": 7, "name": "Jane Doe", "email": "jane@acme.example"}
SERVICE = {"name": "Consulting", "nurture_list_slug": "automation-interest"}


def _configure(monkeypatch, token=None):
    monkeypatch.setenv("NURTURE_WEBHOOK_URL", "http://hooks.test/nurture")
    if token:
        monkeypatch.setenv("NURTURE_WEBHOOK_TOKEN", token)
    else:
        monkeypatch.delenv("NURTURE_WEBHOOK_TOKEN", raising=False)


def test_noop_when_webhook_not_configured(monkeypatch):
    monkeypatch.delenv("NURTURE_WEBHOOK_URL", raising=False)
    success, message = enroll_partner_in_nurture(PARTNER, SERVICE)
    assert success is False
    assert "not configured" in message


def test_noop_when_service_has_no_list_slug(monkeypatch):
    _configure(monkeypatch)
    success, message = enroll_partner_in_nurture(PARTNER, {"name": "Consulting", "nurture_list_slug": None})
    assert success is False
    assert "nurture_list_slug" in message


def test_noop_when_partner_has_no_email(monkeypatch):
    _configure(monkeypatch)
    success, message = enroll_partner_in_nurture({"name": "Jane Doe", "email": None}, SERVICE)
    assert success is False
    assert "no email" in message


def test_successful_hand_off_posts_expected_payload(monkeypatch):
    _configure(monkeypatch, token="test-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse()

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)

    success, message = enroll_partner_in_nurture(PARTNER, SERVICE, deal_id=42)

    assert success is True
    assert "automation-interest" in message
    assert captured["url"] == "http://hooks.test/nurture"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["json"] == {
        "email": "jane@acme.example",
        "name": "Jane Doe",
        "list_slug": "automation-interest",
        "partner_id": 7,
        "deal_id": 42,
    }


def test_no_auth_header_without_token(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "app.services.nurture.httpx.post",
        lambda url, json=None, headers=None, **kw: captured.update(headers=headers) or FakeResponse(),
    )
    assert enroll_partner_in_nurture(PARTNER, SERVICE)[0] is True
    assert "Authorization" not in captured["headers"]


def test_http_failure_is_caught_not_raised(monkeypatch):
    _configure(monkeypatch)

    def fake_post(*args, **kwargs):
        raise ConnectionError("receiver is down")

    monkeypatch.setattr("app.services.nurture.httpx.post", fake_post)

    success, message = enroll_partner_in_nurture(PARTNER, SERVICE)
    assert success is False
    assert "receiver is down" in message


def test_invalid_nurture_list_slug_does_not_call_webhook(monkeypatch):
    _configure(monkeypatch)
    called = []
    monkeypatch.setattr("app.services.nurture.httpx.post", lambda *a, **k: called.append(1))
    success, message = enroll_partner_in_nurture(PARTNER, {"name": "Consulting", "nurture_list_slug": "../admin"})
    assert success is False
    assert "invalid nurture_list_slug" in message
    assert called == []
