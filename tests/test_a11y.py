from app.services.auth import generate_csrf_token
from app.services.catalog import create_service
from app.services.deals import create_deal
from app.services.partners import create_partner


def _assert_skip_and_main(html):
    assert 'href="#main"' in html
    assert 'id="main"' in html
    assert "Skip to main content" in html


def test_login_has_skip_link_and_main(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    _assert_skip_and_main(resp.text)


def test_partners_has_skip_link_and_main(logged_in_client):
    resp = logged_in_client.get("/partners")
    assert resp.status_code == 200
    _assert_skip_and_main(resp.text)
    assert 'href="/partners" aria-current="page">Partners</a>' in resp.text
    assert 'href="/calls" aria-current="page"' not in resp.text


def test_pipeline_nav_marks_current_page(logged_in_client):
    resp = logged_in_client.get("/pipeline")
    assert resp.status_code == 200
    _assert_skip_and_main(resp.text)
    assert 'href="/pipeline" aria-current="page">Pipeline</a>' in resp.text
    assert 'href="/partners" aria-current="page"' not in resp.text


def test_login_error_is_announced(client):
    resp = client.post(
        "/auth/login",
        data={
            "email": "nobody@example.com",
            "password": "wrong-password",
            "csrf_token": generate_csrf_token(client.cookies.get("session")),
        },
    )
    assert resp.status_code == 400
    assert "Invalid email or password" in resp.text
    assert 'class="error" role="alert"' in resp.text


def test_mcp_authorize_error_is_announced(client):
    resp = client.get("/oauth/authorize")
    assert resp.status_code in (200, 400)
    assert 'class="error" role="alert"' in resp.text


def test_call_view_marks_calls_nav_current(logged_in_client, db):
    pid = create_partner("Jane Doe", email="jane@acme.example", phone="0400 111 222")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    resp = logged_in_client.get(f"/deals/{deal_id}/call")
    assert resp.status_code == 200
    _assert_skip_and_main(resp.text)
    assert 'href="/calls" aria-current="page">Calls</a>' in resp.text
    assert 'href="/deals" aria-current="page"' not in resp.text
