from app.services.security_headers import CONTENT_SECURITY_POLICY, csp_allowing_form_redirect


def test_form_redirect_adds_only_the_origin():
    csp = csp_allowing_form_redirect("https://claude.ai/api/mcp/auth_callback?x=1")
    assert csp.endswith("form-action 'self' https://claude.ai")
    assert csp.replace(" https://claude.ai", "") == CONTENT_SECURITY_POLICY


def test_form_redirect_keeps_loopback_port():
    assert csp_allowing_form_redirect("http://127.0.0.1:33418/cb").endswith(
        "form-action 'self' http://127.0.0.1:33418"
    )


def test_form_redirect_cannot_inject_directives():
    for uri in (
        "https://a.example;script-src 'unsafe-inline'/cb",
        "https://a.example cdn.evil/cb",
        "https://user:pw@a.example/cb",
        "javascript:alert(1)",
        "",
    ):
        assert csp_allowing_form_redirect(uri) == CONTENT_SECURITY_POLICY, uri
