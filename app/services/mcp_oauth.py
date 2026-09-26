"""OAuth 2.1 (PKCE, public clients) so hosted MCP connectors (e.g. Grok) can connect.

Grok.com's connector dialog speaks OAuth, not a static API-key field.
Dynamic client registration (RFC 7591) lets Grok self-register. Codes and
tokens are signed (itsdangerous) so we don't persist grants; registered
clients live in sqlite so a restart doesn't drop Grok's client_id.

The operator secret is still CRM_MCP_API_KEY: authorize requires either
that key or an existing admin session, and MCP is fail-closed if the key
is unset.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.database import get_db
from app.services.auth import SECRET_KEY, check_env_api_key

logger = logging.getLogger(__name__)

ACCESS_MAX_AGE = 60 * 60  # 1 hour
REFRESH_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
AUTH_CODE_MAX_AGE = 60 * 10  # 10 minutes
MAX_REDIRECT_URIS = 8
MAX_REDIRECT_LEN = 500

code_signer = URLSafeTimedSerializer(SECRET_KEY, salt="mcp-oauth-code")
access_signer = URLSafeTimedSerializer(SECRET_KEY, salt="mcp-oauth-access")
refresh_signer = URLSafeTimedSerializer(SECRET_KEY, salt="mcp-oauth-refresh")


def mcp_enabled() -> bool:
    return bool(os.getenv("CRM_MCP_API_KEY", "").strip())


def public_base(request) -> str:
    """Issuer / resource base. Prefer BASE_URL so Host cannot rewrite metadata."""
    configured = (os.getenv("BASE_URL") or "").rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def resource_url(request) -> str:
    return f"{public_base(request)}/mcp"


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def get_client(client_id: str):
    if not client_id:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM mcp_oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return dict(row) if row else None


def _parse_redirects(raw):
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def client_redirects(client: dict):
    return [u for u in _parse_redirects(client.get("redirect_uris")) if isinstance(u, str)]


def redirect_allowed(uri: str) -> bool:
    if not uri or not isinstance(uri, str) or len(uri) > MAX_REDIRECT_LEN:
        return False
    if "\\" in uri or uri.strip() != uri:
        return False
    parsed = urlparse(uri)
    if parsed.scheme not in ("https", "http"):
        return False
    if parsed.scheme == "http":
        return parsed.hostname in ("127.0.0.1", "localhost")
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return True


def register_client(body: dict):
    """RFC 7591. Returns (payload, status_code)."""
    if not isinstance(body, dict):
        return {"error": "invalid_client_metadata"}, 400
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"}, 400
    if len(uris) > MAX_REDIRECT_URIS:
        return {"error": "invalid_redirect_uri", "error_description": "too many redirect_uris"}, 400
    cleaned = []
    for uri in uris:
        if not isinstance(uri, str) or not redirect_allowed(uri):
            return {"error": "invalid_redirect_uri"}, 400
        cleaned.append(uri)

    client_name = body.get("client_name")
    if client_name is not None and not isinstance(client_name, str):
        client_name = None
    if isinstance(client_name, str):
        client_name = client_name.strip()[:120] or None

    client_id = secrets.token_urlsafe(24)
    issued_at = _now_ts()
    with get_db() as db:
        db.execute(
            """INSERT INTO mcp_oauth_clients
               (client_id, client_name, redirect_uris, token_endpoint_auth_method, created_at)
               VALUES (?, ?, ?, 'none', ?)""",
            (client_id, client_name, json.dumps(cleaned), datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    return {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "client_name": client_name,
        "redirect_uris": cleaned,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }, 201


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    if len(verifier) < 43 or len(verifier) > 128:
        return False
    digest = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return secrets.compare_digest(digest, challenge)


def issue_auth_code(client_id: str, redirect_uri: str, code_challenge: str, state: str = ""):
    return code_signer.dumps({
        "typ": "mcp_code",
        "cid": client_id,
        "uri": redirect_uri,
        "ch": code_challenge,
        "state": state or "",
    })


def load_auth_code(code: str):
    try:
        data = code_signer.loads(code, max_age=AUTH_CODE_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("typ") != "mcp_code":
        return None
    return data


def issue_tokens(client_id: str):
    access = access_signer.dumps({"typ": "mcp_access", "cid": client_id})
    refresh = refresh_signer.dumps({"typ": "mcp_refresh", "cid": client_id})
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_MAX_AGE,
        "refresh_token": refresh,
        "scope": "crm",
    }


def load_access_token(token: str):
    try:
        data = access_signer.loads(token, max_age=ACCESS_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("typ") != "mcp_access":
        return None
    return data


def load_refresh_token(token: str):
    try:
        data = refresh_signer.loads(token, max_age=REFRESH_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("typ") != "mcp_refresh":
        return None
    return data


def exchange_code(client_id: str, code: str, redirect_uri: str, code_verifier: str):
    client = get_client(client_id)
    if not client:
        return None, "invalid_client"
    if redirect_uri not in client_redirects(client):
        return None, "invalid_grant"
    data = load_auth_code(code)
    if not data:
        return None, "invalid_grant"
    if data.get("cid") != client_id or data.get("uri") != redirect_uri:
        return None, "invalid_grant"
    if not verify_pkce(code_verifier, data.get("ch") or ""):
        return None, "invalid_grant"
    return issue_tokens(client_id), None


def refresh_access(client_id: str, refresh_token: str):
    client = get_client(client_id)
    if not client:
        return None, "invalid_client"
    data = load_refresh_token(refresh_token)
    if not data or data.get("cid") != client_id:
        return None, "invalid_grant"
    return issue_tokens(client_id), None


def presented_secret(request, x_api_key: str = "") -> str:
    """Bearer or X-API-Key. Query-string values are never read."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    return (x_api_key or request.headers.get("x-api-key") or "").strip()


def mcp_request_authorized(secret: str) -> bool:
    """True if MCP is enabled and the secret is the API key or a live access token."""
    if not mcp_enabled():
        return False
    if check_env_api_key(secret, "CRM_MCP_API_KEY"):
        return True
    return load_access_token(secret) is not None


def operator_may_authorize(secret: str, session_user) -> bool:
    if not mcp_enabled():
        return False
    if session_user:
        return True
    return check_env_api_key(secret, "CRM_MCP_API_KEY")
