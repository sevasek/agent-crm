"""JSON-RPC 2.0 MCP dispatcher (Streamable HTTP, JSON responses).

Stateless: we do not require Mcp-Session-Id. Bots (Grok remote MCP, xAI
API, Cursor) POST one message at a time. Notifications return (None, True)
so the HTTP layer can 202.
"""

import logging

from app.mcp import (
    INSTRUCTIONS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from app.mcp.resources import get_prompt, list_prompt_defs, list_resource_defs, read_resource
from app.mcp.tools import call_tool, list_tool_defs
from app.mcp.util import dumps

logger = logging.getLogger(__name__)

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def _rpc_error(rpc_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def _rpc_result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _tool_call_result(payload, is_error=False):
    text = payload if isinstance(payload, str) else dumps(payload)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
        "isError": bool(is_error),
    }


def _initialize(params):
    params = params or {}
    requested = params.get("protocolVersion") or PROTOCOL_VERSION
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def handle_message(message):
    """Return (body_dict_or_None, is_notification).

    body_dict is a JSON-RPC response. Notifications have no id: return
    (None, True). Invalid JSON-RPC still returns an error object.
    """
    if not isinstance(message, dict):
        return _rpc_error(None, _INVALID_REQUEST, "Request must be a JSON object"), False

    if message.get("jsonrpc") != "2.0":
        return _rpc_error(message.get("id"), _INVALID_REQUEST, "jsonrpc must be \"2.0\""), False

    method = message.get("method")
    rpc_id = message.get("id", None)
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    is_notification = "id" not in message

    if not isinstance(method, str) or not method:
        return _rpc_error(rpc_id, _INVALID_REQUEST, "method is required"), False

    try:
        if method.startswith("notifications/"):
            return None, True

        if is_notification:
            return None, True

        if method == "initialize":
            return _rpc_result(rpc_id, _initialize(params)), False

        if method == "ping":
            return _rpc_result(rpc_id, {}), False

        if method == "tools/list":
            return _rpc_result(rpc_id, {"tools": list_tool_defs()}), False

        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return _rpc_error(rpc_id, _INVALID_PARAMS, "name is required"), False
            result, unknown = call_tool(name, params.get("arguments"))
            if unknown:
                return _rpc_result(rpc_id, _tool_call_result(
                    {"ok": False, "error": "unknown_tool", "message": unknown},
                    is_error=True,
                )), False
            is_error = isinstance(result, dict) and result.get("ok") is False
            return _rpc_result(rpc_id, _tool_call_result(result, is_error=is_error)), False

        if method == "resources/list":
            return _rpc_result(rpc_id, {"resources": list_resource_defs()}), False

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                return _rpc_error(rpc_id, _INVALID_PARAMS, "uri is required"), False
            contents = read_resource(uri)
            if not contents:
                return _rpc_error(rpc_id, _INVALID_PARAMS, f"Unknown resource {uri}"), False
            return _rpc_result(rpc_id, contents), False

        if method == "prompts/list":
            return _rpc_result(rpc_id, {"prompts": list_prompt_defs()}), False

        if method == "prompts/get":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return _rpc_error(rpc_id, _INVALID_PARAMS, "name is required"), False
            prompt = get_prompt(name, params.get("arguments"))
            if not prompt:
                return _rpc_error(rpc_id, _INVALID_PARAMS, f"Unknown prompt {name}"), False
            return _rpc_result(rpc_id, prompt), False

        return _rpc_error(rpc_id, _METHOD_NOT_FOUND, f"Method not found: {method}"), False
    except Exception:
        logger.exception("MCP method %s failed", method)
        return _rpc_error(rpc_id, _INTERNAL_ERROR, "Internal error"), False
