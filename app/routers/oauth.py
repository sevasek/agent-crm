"""OAuth 2.1 endpoints for the MCP resource (Grok custom connectors).

Well-known metadata is unlimited (Grok must discover it). Register and
token count only failures toward the unauthenticated IP bucket; a
successful exchange or refresh uses the larger oauth:{client_id} bucket.
Authorize GET is a form view and is not counted. Authorize POST counts
only failed auth (wrong key / CSRF), same rule as login.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.routers.auth import get_current_user
from app.services.auth import (
    check_rate_limit_retry,
    generate_csrf_token,
    get_rate_limit_key,
    oauth_rate_limit_identity,
    validate_csrf_token,
)
from app.services.client_ip import get_client_ip
from app.services import mcp_oauth

router = APIRouter(tags=["mcp-oauth"])
templates = Jinja2Templates(directory="app/templates")


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
    return response


def _json(payload, status=200):
    return _cors(JSONResponse(payload, status_code=status))


def _oauth_error(error, status=400, description=None):
    body = {"error": error}
    if description:
        body["error_description"] = description
    return _json(body, status)


def _rate_limited(request, action, *, identity=None, authenticated=False):
    """Count one hit. Unauthenticated uses the client IP; success uses identity."""
    key = identity if authenticated else get_rate_limit_key(get_client_ip(request))
    allowed, retry_after = check_rate_limit_retry(
        key, action=action, authenticated=authenticated,
    )
    if allowed:
        return None
    response = JSONResponse(
        {"error": "rate_limited"},
        status_code=429,
        headers={"Retry-After": str(retry_after), "Connection": "close"},
    )
    return _cors(response)


def _unauth_limited(request, action):
    return _rate_limited(request, action)


def _client_limited(request, action, client_id):
    return _rate_limited(
        request, action,
        identity=oauth_rate_limit_identity(client_id),
        authenticated=True,
    )


def _metadata(request: Request):
    base = mcp_oauth.public_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["crm"],
        "authorization_response_iss_parameter_supported": True,
    }


def _resource_metadata(request: Request):
    base = mcp_oauth.public_base(request)
    return {
        "resource": mcp_oauth.resource_url(request),
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["crm"],
    }


@router.options("/.well-known/oauth-authorization-server")
@router.options("/.well-known/oauth-protected-resource")
@router.options("/.well-known/oauth-protected-resource/mcp")
@router.options("/oauth/register")
@router.options("/oauth/token")
@router.options("/oauth/authorize")
async def oauth_preflight():
    return _cors(JSONResponse({}, status_code=204))


@router.get("/.well-known/oauth-authorization-server")
async def oauth_as_metadata(request: Request):
    return _json(_metadata(request))


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_resource_metadata(request: Request):
    return _json(_resource_metadata(request))


@router.post("/oauth/register")
async def oauth_register(request: Request):
    if not mcp_oauth.mcp_enabled():
        limited = _unauth_limited(request, "oauth_register")
        if limited:
            return limited
        return _oauth_error("temporarily_unavailable", 503, "MCP is disabled (CRM_MCP_API_KEY unset)")
    try:
        body = await request.json()
    except Exception:
        limited = _unauth_limited(request, "oauth_register")
        if limited:
            return limited
        return _oauth_error("invalid_client_metadata", 400, "JSON body required")
    payload, status = mcp_oauth.register_client(body if isinstance(body, dict) else {})
    if status >= 400:
        limited = _unauth_limited(request, "oauth_register")
        if limited:
            return limited
        return _json(payload, status)
    client_id = (payload.get("client_id") or "").strip()
    if client_id:
        limited = _client_limited(request, "oauth_register", client_id)
        if limited:
            return limited
    return _json(payload, status)


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_page(request: Request):
    # Form views do not burn the failure bucket (same as GET /auth/login).
    params = request.query_params
    form = {
        "client_id": params.get("client_id") or "",
        "redirect_uri": params.get("redirect_uri") or "",
        "state": params.get("state") or "",
        "code_challenge": params.get("code_challenge") or "",
        "code_challenge_method": params.get("code_challenge_method") or "",
        "scope": params.get("scope") or "crm",
        "resource": params.get("resource") or "",
    }
    error, client_name = _validate_authorize_query(form)
    return templates.TemplateResponse("auth/mcp_authorize.html", {
        "request": request,
        "error": error,
        "csrf_token": generate_csrf_token(),
        "form": form,
        "client_name": client_name,
        "mcp_enabled": mcp_oauth.mcp_enabled(),
        "logged_in": bool(get_current_user(request)),
    }, status_code=400 if error else 200)


@router.post("/oauth/authorize")
async def oauth_authorize_submit(
    request: Request,
    csrf_token: str = Form(""),
    api_key: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form(""),
    scope: str = Form("crm"),
    resource: str = Form(""),
):
    form = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "resource": resource,
    }

    if not validate_csrf_token(csrf_token):
        # Failed auth (CSRF) counts toward the IP bucket, like a wrong key.
        limited = _unauth_limited(request, "oauth_authorize")
        if limited:
            return templates.TemplateResponse("auth/mcp_authorize.html", {
                "request": request,
                "error": "Too many attempts. Wait a minute and try again.",
                "csrf_token": generate_csrf_token(),
                "form": form,
                "client_name": "",
                "mcp_enabled": mcp_oauth.mcp_enabled(),
                "logged_in": bool(get_current_user(request)),
            }, status_code=429)
        return templates.TemplateResponse("auth/mcp_authorize.html", {
            "request": request,
            "error": "Invalid form submission. Please try again.",
            "csrf_token": generate_csrf_token(),
            "form": form,
            "client_name": "",
            "mcp_enabled": mcp_oauth.mcp_enabled(),
            "logged_in": bool(get_current_user(request)),
        }, status_code=400)

    error, client_name = _validate_authorize_query(form)
    if error:
        return templates.TemplateResponse("auth/mcp_authorize.html", {
            "request": request,
            "error": error,
            "csrf_token": generate_csrf_token(),
            "form": form,
            "client_name": client_name,
            "mcp_enabled": mcp_oauth.mcp_enabled(),
            "logged_in": bool(get_current_user(request)),
        }, status_code=400)

    user = get_current_user(request)
    if not mcp_oauth.operator_may_authorize(api_key, user):
        # Count only failed authorize attempts so a correct key after
        # guesses still works (same rule as login).
        limited = _unauth_limited(request, "oauth_authorize")
        if limited:
            return templates.TemplateResponse("auth/mcp_authorize.html", {
                "request": request,
                "error": "Too many attempts. Wait a minute and try again.",
                "csrf_token": generate_csrf_token(),
                "form": form,
                "client_name": client_name,
                "mcp_enabled": mcp_oauth.mcp_enabled(),
                "logged_in": bool(user),
            }, status_code=429)
        return templates.TemplateResponse("auth/mcp_authorize.html", {
            "request": request,
            "error": (
                "MCP is disabled." if not mcp_oauth.mcp_enabled()
                else "Wrong API key. Use CRM_MCP_API_KEY, or log in to the CRM first."
            ),
            "csrf_token": generate_csrf_token(),
            "form": form,
            "client_name": client_name,
            "mcp_enabled": mcp_oauth.mcp_enabled(),
            "logged_in": bool(user),
        }, status_code=401)

    code = mcp_oauth.issue_auth_code(client_id, redirect_uri, code_challenge, state)
    query = {"code": code, "iss": mcp_oauth.public_base(request)}
    if state:
        query["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=302)


@router.post("/oauth/token")
async def oauth_token(request: Request):
    if not mcp_oauth.mcp_enabled():
        limited = _unauth_limited(request, "oauth_token")
        if limited:
            return limited
        return _oauth_error("temporarily_unavailable", 503, "MCP is disabled")

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            body = await request.json()
        except Exception:
            limited = _unauth_limited(request, "oauth_token")
            if limited:
                return limited
            return _oauth_error("invalid_request", 400, "invalid JSON")
        if not isinstance(body, dict):
            limited = _unauth_limited(request, "oauth_token")
            if limited:
                return limited
            return _oauth_error("invalid_request")
    else:
        form = await request.form()
        body = {k: form.get(k) for k in form.keys()}

    grant = (body.get("grant_type") or "").strip()
    client_id = (body.get("client_id") or "").strip()
    if grant == "authorization_code":
        tokens, error = mcp_oauth.exchange_code(
            client_id,
            body.get("code") or "",
            body.get("redirect_uri") or "",
            body.get("code_verifier") or "",
        )
    elif grant == "refresh_token":
        tokens, error = mcp_oauth.refresh_access(client_id, body.get("refresh_token") or "")
    else:
        limited = _unauth_limited(request, "oauth_token")
        if limited:
            return limited
        return _oauth_error("unsupported_grant_type")
    if error:
        limited = _unauth_limited(request, "oauth_token")
        if limited:
            return limited
        return _oauth_error(error)
    if client_id:
        limited = _client_limited(request, "oauth_token", client_id)
        if limited:
            return limited
    return _json(tokens)


def _validate_authorize_query(form: dict):
    if not mcp_oauth.mcp_enabled():
        return "MCP is disabled (CRM_MCP_API_KEY is unset).", ""
    client_id = form.get("client_id") or ""
    redirect_uri = form.get("redirect_uri") or ""
    challenge = form.get("code_challenge") or ""
    method = (form.get("code_challenge_method") or "").upper()
    if method != "S256":
        return "code_challenge_method must be S256.", ""
    if not challenge:
        return "PKCE code_challenge is required.", ""
    client = mcp_oauth.get_client(client_id)
    if not client:
        return "Unknown client. Grok must register first.", ""
    if redirect_uri not in mcp_oauth.client_redirects(client):
        return "redirect_uri is not registered for this client.", ""
    return None, client.get("client_name") or "Grok"
