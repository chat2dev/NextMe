"""ACP (Agent Control Protocol) — JSON-RPC 2.0 over subprocess stdin/stdout.

Wire format: newline-delimited JSON (ndjson), one message per line.

Protocol flow
-------------
Bot → cc-acp (via stdin):
    initialize          negotiates capabilities
    session/new         creates a new Claude session
    session/load        resumes an existing session
    session/prompt      submits a user prompt (long-running)
    session/cancel      cancels in-flight prompt

cc-acp → Bot (via stdout):
    JSON-RPC responses  matched by id to the originating request
    session/update      streaming notification (no id); carries content
                        deltas, tool-call events, plan updates, etc.
    session/request_permission
                        inbound JSON-RPC *request* asking the bot to
                        choose a permission option; bot must respond

Message direction is indicated by whether a message has both ``id`` and
``method`` (inbound server→client request), only ``id`` (response), or
only ``method`` (notification).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def make_request(method: str, params: dict[str, Any], req_id: int) -> str:
    """Serialize a JSON-RPC 2.0 request to a single ndjson line (no trailing \\n)."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    return json.dumps(msg, ensure_ascii=False)


def make_response(req_id: int, result: Any) -> str:
    """Serialize a JSON-RPC 2.0 success response."""
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)


def make_error_response(req_id: int, code: int, message: str) -> str:
    """Serialize a JSON-RPC 2.0 error response."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )


def parse_message(line: str) -> dict[str, Any]:
    """Parse one ndjson line from cc-acp stdout.

    Raises:
        ValueError: If the line is empty, not valid JSON, or not a dict.
    """
    line = line.strip()
    if not line:
        raise ValueError("empty line")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def classify(msg: dict[str, Any]) -> str:
    """Classify an inbound cc-acp message.

    Returns one of:
        ``"response"``      — has ``id`` + (``result`` or ``error``)
        ``"notification"``  — has ``method``, no ``id`` (or ``id`` is None)
        ``"server_request"``— has ``method`` + ``id`` (server calls client)
    """
    has_id = "id" in msg and msg["id"] is not None
    has_method = "method" in msg
    has_result = "result" in msg or "error" in msg

    if has_id and has_result and not has_method:
        return "response"
    if has_method and not has_id:
        return "notification"
    if has_method and has_id:
        return "server_request"
    return "unknown"


# ---------------------------------------------------------------------------
# Outbound request parameter builders
# ---------------------------------------------------------------------------


def initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": False},
            "terminal": False,
        },
    }


def new_session_params(cwd: str) -> dict[str, Any]:
    return {"cwd": cwd, "mcpServers": []}


def load_session_params(session_id: str, cwd: str) -> dict[str, Any]:
    return {"sessionId": session_id, "cwd": cwd, "mcpServers": []}


def prompt_params(session_id: str, content: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": content}],
    }


def cancel_params(session_id: str) -> dict[str, Any]:
    return {"sessionId": session_id}


# ---------------------------------------------------------------------------
# Inbound permission option kinds
# ---------------------------------------------------------------------------

#: Mapping from permissionOption.kind to a human-friendly label.
PERMISSION_KIND_LABEL: dict[str, str] = {
    "allow_once": "Allow once",
    "allow_always": "Allow always",
    "reject_once": "Deny once",
    "reject_always": "Deny always",
}


@dataclass
class PermissionOption:
    """One choice offered by a ``session/request_permission`` server request."""

    option_id: str
    name: str
    kind: str  # allow_once | allow_always | reject_once | reject_always


@dataclass
class InboundPermissionRequest:
    """Parsed ``session/request_permission`` server→client request."""

    jsonrpc_id: int  # must echo back in the response
    session_id: str
    tool_call: dict[str, Any]
    options: list[PermissionOption] = field(default_factory=list)


def parse_permission_request(msg: dict[str, Any]) -> InboundPermissionRequest:
    """Parse a ``session/request_permission`` inbound server request."""
    params: dict[str, Any] = msg.get("params") or {}
    raw_options: list[dict[str, Any]] = params.get("options") or []
    options = [
        PermissionOption(
            option_id=opt.get("optionId", ""),
            name=opt.get("name", ""),
            kind=opt.get("kind", ""),
        )
        for opt in raw_options
    ]
    return InboundPermissionRequest(
        jsonrpc_id=msg["id"],
        session_id=params.get("sessionId", ""),
        tool_call=params.get("toolCall") or {},
        options=options,
    )


def permission_response_result(option_id: str) -> dict[str, Any]:
    """Build the ``result`` for a ``session/request_permission`` response.

    Args:
        option_id: The chosen ``PermissionOption.option_id`` string.
    """
    return {"outcome": {"selected": {"optionId": option_id}}}


def permission_cancel_result() -> dict[str, Any]:
    """Build a cancellation result for a ``session/request_permission`` response."""
    return {"outcome": {"cancelled": {}}}
