from app.services.won_webhook import notify_deal_won


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PARTNER = {"id": 7, "name": "Jane Doe", "email": "jane@acme.example"}
SERVICE = {"id": 3, "name": "Consulting", "slug": "consulting"}
DEAL = {"id": 42, "value_estimate": 1500.0, "offer_id": 9}
OFFER = {"id": 9, "name": "Discovery Session", "price": 350.0, "currency": "AUD"}


def _configure(monkeypatch, token=None):
    monkeypatch.setenv("DEAL_WON_WEBHOOK_URL", "http://hooks.test/won")
    if token:
        monkeypatch.setenv("DEAL_WON_WEBHOOK_TOKEN", token)
    else:
        monkeypatch.delenv("DEAL_WON_WEBHOOK_TOKEN", raising=False)


def test_noop_when_webhook_not_configured(monkeypatch):
    monkeypatch.delenv("DEAL_WON_WEBHOOK_URL", raising=False)
    success, message = notify_deal_won(PARTNER, SERVICE, DEAL, "won", offer=OFFER)
    assert success is False
    assert "not configured" in message


def test_successful_hand_off_posts_expected_payload(monkeypatch):
    _configure(monkeypatch, token="test-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse()

    monkeypatch.setattr("app.services.won_webhook.httpx.post", fake_post)

    success, message = notify_deal_won(PARTNER, SERVICE, DEAL, "won", offer=OFFER)

    assert success is True
    assert "deal 42" in message
    assert captured["url"] == "http://hooks.test/won"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["json"] == {
        "deal_id": 42,
        "stage": "won",
        "partner_id": 7,
        "partner_name": "Jane Doe",
        "partner_email": "jane@acme.example",
        "service_id": 3,
        "service_name": "Consulting",
        "service_slug": "consulting",
        "value_estimate": 1500.0,
        "offer": {
            "id": 9,
            "name": "Discovery Session",
            "price": 350.0,
            "currency": "AUD",
        },
    }


def test_payload_omits_offer_when_absent_and_allows_missing_email(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "app.services.won_webhook.httpx.post",
        lambda url, json=None, headers=None, **kw: captured.update(json=json) or FakeResponse(),
    )
    partner = {"id": 7, "name": "Jane Doe", "email": None}
    deal = {"id": 42, "value_estimate": None, "offer_id": None}
    assert notify_deal_won(partner, SERVICE, deal, "won")[0] is True
    assert captured["json"]["partner_email"] is None
    assert captured["json"]["offer"] is None
    assert captured["json"]["value_estimate"] is None


def test_no_auth_header_without_token(monkeypatch):
    _configure(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "app.services.won_webhook.httpx.post",
        lambda url, json=None, headers=None, **kw: captured.update(headers=headers) or FakeResponse(),
    )
    assert notify_deal_won(PARTNER, SERVICE, DEAL, "won")[0] is True
    assert "Authorization" not in captured["headers"]


def test_http_failure_is_caught_not_raised(monkeypatch):
    _configure(monkeypatch)

    def fake_post(*args, **kwargs):
        raise ConnectionError("receiver is down")

    monkeypatch.setattr("app.services.won_webhook.httpx.post", fake_post)

    success, message = notify_deal_won(PARTNER, SERVICE, DEAL, "won")
    assert success is False
    assert "receiver is down" in message
