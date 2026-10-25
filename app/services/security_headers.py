"""Content-Security-Policy shared by the app middleware and routes that widen it."""

import re
from urllib.parse import urlsplit

# Same-origin scripts and styles only. Vendored Pico, htmx, and Tom Select
# live at /static/vendor/; call-tap.js / app.js replace inline <script> so
# this does not need script-src 'unsafe-inline'. style-src still allows
# 'unsafe-inline' for the handful of style="" attributes in templates.
_BASE_DIRECTIVES = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
)
CONTENT_SECURITY_POLICY = _BASE_DIRECTIVES + "form-action 'self'"

# scheme://host[:port] only, so nothing from a URL can add a directive.
_ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")


def csp_allowing_form_redirect(uri: str) -> str:
    """CONTENT_SECURITY_POLICY with uri's origin added to form-action.

    Browsers apply form-action to the redirects a form POST follows, so a
    form whose handler 302s off-site (OAuth authorize -> client redirect_uri)
    is blocked under form-action 'self'. Returns the base policy unchanged if
    uri has no plain http(s) origin.
    """
    parts = urlsplit(uri)
    origin = f"{parts.scheme}://{parts.netloc}"
    if parts.username or parts.password or not _ORIGIN_RE.fullmatch(origin):
        return CONTENT_SECURITY_POLICY
    return _BASE_DIRECTIVES + f"form-action 'self' {origin}"
