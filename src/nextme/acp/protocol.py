"""ACP (Agent Control Protocol) message types and ndjson serialization.

The protocol is ndjson over subprocess stdin/stdout.

Bot → ACP (written to subprocess stdin):
    new_session, load_session, prompt, permission_response, cancel

ACP → Bot (read from subprocess stdout):
    ready, session_created, content_delta, tool_use,
    permission_request, done, error
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Bot → ACP messages (sent to subprocess stdin)
# ---------------------------------------------------------------------------


@dataclass
class NewSessionMsg:
    """Request ACP to create a brand-new session."""

    session_id: str = ""
    cwd: str = ""
    type: str = field(default="new_session", init=False)


@dataclass
class LoadSessionMsg:
    """Request ACP to resume an existing session by its ACP-assigned id."""

    session_id: str = ""
    type: str = field(default="load_session", init=False)


@dataclass
class PromptMsg:
    """Send a user prompt to the active session."""

    session_id: str = ""
    content: str = ""
    type: str = field(default="prompt", init=False)


@dataclass
class PermissionResponseMsg:
    """Reply to a permission_request from ACP."""

    request_id: str = ""
    # 1-based index matching the user's chosen option
    choice: int = 1
    type: str = field(default="permission_response", init=False)


@dataclass
class CancelMsg:
    """Ask ACP to cancel the currently running task for a session."""

    session_id: str = ""
    type: str = field(default="cancel", init=False)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize_msg(msg: Any) -> str:
    """Serialize a dataclass instance to a single ndjson line (no trailing newline).

    The ``type`` field is always included first in the output object so that
    the wire format is human-readable and easy to grep.

    Args:
        msg: A dataclass instance (NewSessionMsg, PromptMsg, …).

    Returns:
        A JSON string without a trailing newline character.
    """
    if not dataclasses.is_dataclass(msg) or isinstance(msg, type):
        raise TypeError(f"serialize_msg expects a dataclass instance, got {type(msg)!r}")

    raw: dict[str, Any] = dataclasses.asdict(msg)

    # Move 'type' to the front for readability.
    type_value = raw.pop("type", None)
    ordered: dict[str, Any] = {}
    if type_value is not None:
        ordered["type"] = type_value
    ordered.update(raw)

    return json.dumps(ordered, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Parsing helpers (ACP → Bot)
# ---------------------------------------------------------------------------

# Known ACP → Bot message types for documentation / IDE assistance.
_KNOWN_INBOUND_TYPES: frozenset[str] = frozenset(
    {
        "ready",
        "session_created",
        "content_delta",
        "tool_use",
        "permission_request",
        "done",
        "error",
    }
)


def parse_acp_message(line: str) -> dict[str, Any]:
    """Parse one ndjson line received from the ACP subprocess stdout.

    Args:
        line: A single UTF-8 text line (may have a trailing newline).

    Returns:
        A plain Python dict with at least a ``"type"`` key.

    Raises:
        ValueError: If the line is not valid JSON or is not a JSON object.
    """
    line = line.strip()
    if not line:
        raise ValueError("parse_acp_message received an empty line")

    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ndjson line: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"ACP message must be a JSON object, got {type(data).__name__}"
        )

    return data
