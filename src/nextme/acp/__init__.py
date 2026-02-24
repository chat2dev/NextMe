"""ACP (Agent Control Protocol) integration for NextMe.

Public surface
--------------
protocol  — ndjson message dataclasses + parse/serialize helpers
client    — ACPClient (thin stdin/stdout wrapper)
runtime   — ACPRuntime (subprocess lifecycle + execute/cancel/stop)
janitor   — ACPRuntimeRegistry (global session map) + ACPJanitor (idle reaper)
"""

from .client import ACPClient
from .janitor import ACPJanitor, ACPRuntimeRegistry
from .protocol import (
    CancelMsg,
    LoadSessionMsg,
    NewSessionMsg,
    PermissionResponseMsg,
    PromptMsg,
    parse_acp_message,
    serialize_msg,
)
from .runtime import ACPRuntime

__all__ = [
    # protocol
    "NewSessionMsg",
    "LoadSessionMsg",
    "PromptMsg",
    "PermissionResponseMsg",
    "CancelMsg",
    "parse_acp_message",
    "serialize_msg",
    # client
    "ACPClient",
    # runtime
    "ACPRuntime",
    # janitor
    "ACPRuntimeRegistry",
    "ACPJanitor",
]
