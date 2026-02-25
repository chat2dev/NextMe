"""ACP (Agent Control Protocol) integration for NextMe.

Public surface
--------------
protocol  — JSON-RPC 2.0 message helpers + parse/classify utilities
client    — ACPClient (bidirectional JSON-RPC stdin/stdout wrapper)
runtime   — ACPRuntime (subprocess lifecycle + execute/cancel/stop)
janitor   — ACPRuntimeRegistry (global session map) + ACPJanitor (idle reaper)
"""

from .client import ACPClient
from .janitor import ACPJanitor, ACPRuntimeRegistry
from .protocol import (
    InboundPermissionRequest,
    PermissionOption,
    cancel_params,
    classify,
    initialize_params,
    load_session_params,
    make_request,
    make_response,
    new_session_params,
    parse_message,
    parse_permission_request,
    permission_cancel_result,
    permission_response_result,
    prompt_params,
)
from .runtime import ACPRuntime

__all__ = [
    # protocol helpers
    "make_request",
    "make_response",
    "parse_message",
    "classify",
    "initialize_params",
    "new_session_params",
    "load_session_params",
    "prompt_params",
    "cancel_params",
    "InboundPermissionRequest",
    "PermissionOption",
    "parse_permission_request",
    "permission_response_result",
    "permission_cancel_result",
    # client
    "ACPClient",
    # runtime
    "ACPRuntime",
    # janitor
    "ACPRuntimeRegistry",
    "ACPJanitor",
]
