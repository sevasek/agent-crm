"""Streamable HTTP MCP endpoint at POST /mcp.

Auth: CRM_MCP_API_KEY via Authorization: Bearer or X-API-Key, or a live
OAuth access token from /oauth/authorize. Query-string keys never count.
Lives on crm-web (not /api) so hosted Grok bots are not IP-allowlisted.
"""

import json
import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from app.mcp.protocol import handle_message
from app.mcp.util import MAX_BODY_BYTES
from app.services.auth import (
    check_env_api_key,
    check_rate_limit_retry,
    env_key_rate_limit_identity,
    get_rate_limit_key,
    oauth_rate_limit_identity,
)
from app.services.client_ip import get_client_ip
from app.services import mcp_oauth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": (
            "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version"
        ),
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id",
    }


def _www_authenticate(request: Request) -> str:
    metadata = f"{mcp_oauth.public_base(request)}/.well-known/oauth-protected-resource"
    return f'Bearer realm="crm", resource_metadata="{metadata}"'


def _unauthenticated(request: Request, *, close: bool = True):
    headers = _cors_headers()
    headers["WWW-Authenticate"] = _www_authenticate(request)
    if close:
        headers["Connection"] = "close"
    return JSONResponse({"error": "invalid_api_key"}, status_code=401, headers=headers)


def _error(error: str, status: int, *, close: bool = False, extra_headers=None):
    headers = _cors_headers()
    if close:
        headers["Connection"] = "close"
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def _accepts_json(accept: str) -> bool:
    lowered = (accept or "").lower()
    return "application/json" in lowered or "*/*" in lowered or not lowered.strip()


def _accepts_sse(accept: str) -> bool:
    return "text/event-stream" in (accept or "").lower()


def _mcp_rate_limit_identity(secret: str) -> str | None:
    """Key/token id for the authenticated MCP bucket, or None if unauthorized."""
    if not mcp_oauth.mcp_enabled():
        return None
    if check_env_api_key(secret, "CRM_MCP_API_KEY"):
        return env_key_rate_limit_identity("CRM_MCP_API_KEY")
    data = mcp_oauth.load_access_token(secret)
    if data:
        return oauth_rate_limit_identity(data.get("cid") or "")
    return None


def _mcp_rate_limited(ip_or_identity: str, *, authenticated: bool):
    allowed, retry_after = check_rate_limit_retry(
        ip_or_identity, action="mcp_api", authenticated=authenticated,
    )
    if allowed:
        return None
    return _error(
        "rate_limited", 429, close=True,
        extra_headers={"Retry-After": str(retry_after)},
    )


def _mcp_response(payload: dict, accept: str):
    headers = _cors_headers()
    if _accepts_json(accept) or not _accepts_sse(accept):
        return JSONResponse(payload, headers=headers)
    body = f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    headers["Content-Type"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"
    return Response(content=body, media_type="text/event-stream", headers=headers)


@router.options("/mcp")
async def mcp_preflight():
    return Response(status_code=204, headers=_cors_headers())


@router.get("/mcp")
async def mcp_get():
    # Optional GET SSE stream is not implemented (stateless JSON POST).
    headers = _cors_headers()
    headers["Allow"] = "POST, OPTIONS"
    return Response(status_code=405, headers=headers)


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers=_cors_headers())


@router.post("/mcp")
async def mcp_post(request: Request, x_api_key: str = Header(default="")):
    secret = mcp_oauth.presented_secret(request, x_api_key)
    identity = _mcp_rate_limit_identity(secret)
    if identity is None:
        limited = _mcp_rate_limited(get_rate_limit_key(get_client_ip(request)), authenticated=False)
        if limited:
            return limited
        return _unauthenticated(request)

    limited = _mcp_rate_limited(identity, authenticated=True)
    if limited:
        return limited

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return _error("payload_too_large", 413, close=True)
        except ValueError:
            return _error("payload_too_large", 413, close=True)

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return _error("payload_too_large", 413, close=True)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return _error("invalid_json", 422)

    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("invalid_json", 422)

    accept = request.headers.get("accept") or ""
    body, is_notification = handle_message(message)
    if is_notification and body is None:
        return Response(status_code=202, headers=_cors_headers())
    return _mcp_response(body, accept)
