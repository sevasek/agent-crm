import json
import math

from fastapi import APIRouter, Request, Header
from fastapi.responses import JSONResponse, Response

from app.services.api_keys import verify_api_key
from app.services.auth import check_env_api_key, check_rate_limit_retry, get_rate_limit_key
from app.services.client_ip import get_client_ip
from app.services.leads import ingest_lead
from app.services import pipeline_stages

router = APIRouter(prefix="/api/v1", tags=["api"])

MAX_LEADS = 100
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


def _json_error(error: str, status_code: int, *, close: bool = False):
    # Connection: close on paths that have not drained request.stream(), so a
    # keep-alive / pooled proxy connection is not left with unread request bytes.
    headers = {"Connection": "close"} if close else None
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def _check_api_key(x_api_key: str) -> bool:
    # Per-user keys created in Account Settings are checked first (the
    # intended path going forward); CRM_API_KEY stays as a legacy shared
    # fallback for bots already configured with it. Settings keys do not
    # authorize /mcp or /api/v1/stages.
    if x_api_key and verify_api_key(x_api_key):
        return True
    return check_env_api_key(x_api_key, "CRM_API_KEY")


def _check_stages_api_key(x_api_key: str) -> bool:
    # Deliberately a separate secret from CRM_API_KEY. That key is the
    # lead-ingest credential — proxy-allowlisted for "post a lead",
    # nothing more. Pipeline config (stage roles, reorder, delete) is a much
    # bigger blast radius than ingest, so it gets its own key rather than
    # inheriting whatever trust CRM_API_KEY already carries elsewhere.
    return check_env_api_key(x_api_key, "CRM_STAGES_API_KEY")


def _content_length_exceeds_cap(request: Request) -> bool:
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        return int(raw) > MAX_BODY_BYTES
    except ValueError:
        return False


def _is_json_content_type(content_type: str) -> bool:
    media_type = content_type.split(";")[0].strip().lower()
    return media_type == "application/json"


def _body_is_present(request: Request) -> bool:
    raw = request.headers.get("content-length")
    if raw is not None:
        try:
            return int(raw) > 0
        except ValueError:
            return True
    transfer_encoding = request.headers.get("transfer-encoding", "")
    return bool(transfer_encoding.strip())


async def _read_body_capped(request: Request):
    """Read the request body. Returns None if it exceeds MAX_BODY_BYTES."""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/leads")
async def create_leads(request: Request, x_api_key: str = Header(default="")):
    ip = get_client_ip(request)
    allowed, retry_after = check_rate_limit_retry(get_rate_limit_key(ip), action="leads_api")
    if not allowed:
        # Body is still unread here, so close the keep-alive connection.
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(retry_after), "Connection": "close"},
        )

    if _content_length_exceeds_cap(request):
        return _json_error("payload_too_large", 413, close=True)

    # Only the X-API-Key header authenticates. Query-string values (api_key,
    # apikey, x-api-key, …) are ignored and never compared.
    if not _check_api_key(x_api_key):
        return _json_error("invalid_api_key", 401, close=True)

    if _body_is_present(request) and not _is_json_content_type(
        request.headers.get("content-type") or ""
    ):
        return _json_error("unsupported_media_type", 415, close=True)

    raw = await _read_body_capped(request)
    if raw is None:
        return _json_error("payload_too_large", 413, close=True)

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_error("invalid_json", 422)

    if not isinstance(body, dict):
        return _json_error("invalid_body_shape", 422)

    leads = body.get("leads")
    if not isinstance(leads, list) or not leads:
        return _json_error("empty_leads_list", 422)

    if len(leads) > MAX_LEADS:
        return _json_error("too_many_leads", 422)

    results = []
    for lead in leads:
        if not isinstance(lead, dict):
            results.append({"email": "", "status": "invalid"})
            continue
        results.append(ingest_lead(lead))
    return {"results": results}


# ==================== Pipeline stages ====================
# The deal pipeline (new -> contacted -> qualified -> ... -> won/lost) is
# configurable, not hardcoded — see app.services.pipeline_stages. These
# endpoints let a script or agent read/reshape the workflow as the business
# learns what it takes to close and upsell. Gated by CRM_STAGES_API_KEY, a
# secret deliberately separate from CRM_API_KEY (leads/ingest) — pipeline
# config (roles, reorder, delete) is a much bigger blast radius than "post a
# lead", so it doesn't inherit whatever trust the ingest key carries.

_STAGE_BOOL_FIELDS = ("is_default", "is_qualified_pool", "triggers_nurture", "is_won", "is_lost")


def _serialize_stage(stage: dict) -> dict:
    out = dict(stage)
    for field in _STAGE_BOOL_FIELDS:
        out[field] = bool(out.get(field))
    return out


def _stage_bool_kwargs(body: dict):
    """Returns (kwargs, None) or (None, bad_field_name). Only real booleans
    (or 0/1) are accepted — in Python `bool("false")` is True, so a JSON
    string would otherwise silently turn a flag on instead of off."""
    kwargs = {}
    for field in _STAGE_BOOL_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if isinstance(value, bool):
            kwargs[field] = value
        elif value in (0, 1):
            kwargs[field] = bool(value)
        else:
            return None, field
    return kwargs, None


# SQLite INTEGER is signed 64-bit; Python's int is unbounded, so a value
# outside this range would raise OverflowError on bind rather than fail
# validation here. Same bounds as leads.py's _in_sqlite_int_range.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _safe_int(value):
    """int for a real int or finite float within SQLite's signed 64-bit
    range; None for anything else (bool, NaN/Infinity, out of range,
    non-numeric) rather than letting int() or a later SQLite bind raise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    if isinstance(value, float) and math.isfinite(value):
        converted = int(value)
        return converted if _SQLITE_INT_MIN <= converted <= _SQLITE_INT_MAX else None
    return None


async def _authenticated_json_body(request: Request, x_api_key: str, action: str):
    """Common preamble for the mutating stage endpoints: rate limit, size cap,
    API key, content-type, then a parsed JSON object. Returns (body, None) on
    success, or (None, error_response) to return immediately."""
    ip = get_client_ip(request)
    allowed, retry_after = check_rate_limit_retry(get_rate_limit_key(ip), action=action)
    if not allowed:
        return None, JSONResponse(
            {"error": "rate_limited"}, status_code=429,
            headers={"Retry-After": str(retry_after), "Connection": "close"},
        )

    if _content_length_exceeds_cap(request):
        return None, _json_error("payload_too_large", 413, close=True)

    if not _check_stages_api_key(x_api_key):
        return None, _json_error("invalid_api_key", 401, close=True)

    if _body_is_present(request) and not _is_json_content_type(
        request.headers.get("content-type") or ""
    ):
        return None, _json_error("unsupported_media_type", 415, close=True)

    raw = await _read_body_capped(request)
    if raw is None:
        return None, _json_error("payload_too_large", 413, close=True)
    if not raw:
        return {}, None

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _json_error("invalid_json", 422)

    if not isinstance(body, dict):
        return None, _json_error("invalid_body_shape", 422)

    return body, None


def _authenticate_get(request: Request, x_api_key: str, action: str = "stages_api"):
    """Rate limit + API key check for read-only (no body) GET endpoints."""
    ip = get_client_ip(request)
    allowed, retry_after = check_rate_limit_retry(get_rate_limit_key(ip), action=action)
    if not allowed:
        return JSONResponse(
            {"error": "rate_limited"}, status_code=429,
            headers={"Retry-After": str(retry_after), "Connection": "close"},
        )
    if not _check_stages_api_key(x_api_key):
        return _json_error("invalid_api_key", 401, close=True)
    return None


@router.get("/stages")
async def list_stages_endpoint(request: Request, x_api_key: str = Header(default="")):
    error = _authenticate_get(request, x_api_key)
    if error:
        return error
    return {"stages": [_serialize_stage(s) for s in pipeline_stages.list_stages()]}


@router.get("/stages/{key}")
async def get_stage_endpoint(key: str, request: Request, x_api_key: str = Header(default="")):
    error = _authenticate_get(request, x_api_key)
    if error:
        return error
    stage = pipeline_stages.get_stage(key)
    if not stage:
        return _json_error("not_found", 404)
    return {"stage": _serialize_stage(stage)}


@router.post("/stages")
async def create_stage_endpoint(request: Request, x_api_key: str = Header(default="")):
    body, error = await _authenticated_json_body(request, x_api_key, "stages_api")
    if error:
        return error

    key = body.get("key")
    label = body.get("label")
    if not isinstance(key, str) or not isinstance(label, str):
        return _json_error("key_and_label_required", 422)

    position = None
    if "position" in body:
        position = _safe_int(body["position"])
        if position is None:
            return _json_error("invalid_position", 422)

    bool_kwargs, bad_field = _stage_bool_kwargs(body)
    if bad_field:
        return _json_error(f"invalid_{bad_field}", 422)

    stage, error_code = pipeline_stages.create_stage(key, label, position=position, **bool_kwargs)
    if error_code:
        return _json_error(error_code, 422)
    return JSONResponse({"stage": _serialize_stage(stage)}, status_code=201)


@router.patch("/stages/{key}")
async def update_stage_endpoint(key: str, request: Request, x_api_key: str = Header(default="")):
    body, error = await _authenticated_json_body(request, x_api_key, "stages_api")
    if error:
        return error

    fields, bad_field = _stage_bool_kwargs(body)
    if bad_field:
        return _json_error(f"invalid_{bad_field}", 422)
    if "label" in body:
        if not isinstance(body["label"], str):
            return _json_error("invalid_label", 422)
        fields["label"] = body["label"]
    if "position" in body:
        position = _safe_int(body["position"])
        if position is None:
            return _json_error("invalid_position", 422)
        fields["position"] = position

    stage, error_code = pipeline_stages.update_stage(key, **fields)
    if error_code:
        return _json_error(error_code, 404 if error_code == "not_found" else 422)
    return {"stage": _serialize_stage(stage)}


@router.delete("/stages/{key}")
async def delete_stage_endpoint(key: str, request: Request, x_api_key: str = Header(default="")):
    error = _authenticate_get(request, x_api_key)
    if error:
        return error
    ok, error_code = pipeline_stages.delete_stage(key)
    if not ok:
        return _json_error(error_code, 404 if error_code == "not_found" else 409)
    return Response(status_code=204)


@router.post("/stages/reorder")
async def reorder_stages_endpoint(request: Request, x_api_key: str = Header(default="")):
    body, error = await _authenticated_json_body(request, x_api_key, "stages_api")
    if error:
        return error

    order = body.get("order")
    if not isinstance(order, list) or not order or not all(isinstance(k, str) for k in order):
        return _json_error("invalid_order", 422)

    ok, error_code = pipeline_stages.reorder_stages(order)
    if not ok:
        return _json_error(error_code, 422)
    return {"stages": [_serialize_stage(s) for s in pipeline_stages.list_stages()]}
