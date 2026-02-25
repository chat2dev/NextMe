"""IM-agnostic typing Protocols for core business logic.

Defines three ``@runtime_checkable`` Protocol classes that decouple ``core/``
from specific IM platforms (Feishu / Slack / …) and Agent runtimes (ACP / …).

Concrete implementations that structurally satisfy these protocols:

- :class:`Replier`       ← :class:`nextme.feishu.reply.FeishuReplier`
- :class:`IMAdapter`     ← :class:`nextme.feishu.client.FeishuClient`
- :class:`AgentRuntime`  ← :class:`nextme.acp.runtime.ACPRuntime`

Design notes
------------
* Only depends on ``protocol/types.py`` — no circular-import risk.
* All three protocols are ``@runtime_checkable`` so ``isinstance()`` guards
  work in tests and defensive assertions.
* Default parameter values are omitted intentionally: callers should always
  use keyword arguments for optional parameters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from ..protocol.types import PermissionChoice, PermissionRequest, PermOption, Task


@runtime_checkable
class Replier(Protocol):
    """Sends messages and builds interactive card payloads for an IM platform.

    Async methods send content to a specific chat; sync ``build_*`` methods
    produce platform-specific JSON strings without performing any I/O.
    """

    # ------------------------------------------------------------------
    # Async send primitives
    # ------------------------------------------------------------------

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send a plain-text (markdown) message.  Returns the message_id."""
        ...

    async def send_card(self, chat_id: str, card_json: str) -> str:
        """Send an interactive card.  Returns the message_id."""
        ...

    async def update_card(self, message_id: str, card_json: str) -> None:
        """Replace the content of an existing interactive card in-place."""
        ...

    async def send_reaction(self, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to an existing message."""
        ...

    # ------------------------------------------------------------------
    # Sync card builders
    # ------------------------------------------------------------------

    def build_progress_card(
        self,
        status: str,
        content: str,
        title: str = "思考中...",
    ) -> str:
        """Return a card JSON string for in-progress status updates."""
        ...

    def build_result_card(
        self,
        content: str,
        title: str = "完成",
        template: str = "blue",
        reasoning: str = "",
        session_id: str = "",
    ) -> str:
        """Return a card JSON string for the final result."""
        ...

    def build_permission_card(
        self,
        description: str,
        options: list[PermOption],
        session_id: str = "",
    ) -> str:
        """Return a card JSON string for a permission request."""
        ...

    def build_error_card(self, error: str) -> str:
        """Return a card JSON string for an error message."""
        ...

    def build_help_card(self, commands: list[tuple[str, str]]) -> str:
        """Return a card JSON string listing available commands."""
        ...


@runtime_checkable
class IMAdapter(Protocol):
    """Manages the connection lifecycle of an IM platform client.

    Responsible for establishing and tearing down the long-running connection
    (e.g. WebSocket) and vending :class:`Replier` instances for sending
    messages.
    """

    async def start(self) -> None:
        """Establish the connection.  Blocks until stopped."""
        ...

    async def stop(self) -> None:
        """Gracefully disconnect."""
        ...

    def get_replier(self) -> Replier:
        """Return a :class:`Replier` backed by this adapter's connection."""
        ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Drives an agent subprocess for a single bot session.

    One instance per :class:`~nextme.core.session.Session`.  Handles subprocess
    lifecycle (start / stop), prompt execution, and permission round-trips.
    """

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` while the subprocess is alive."""
        ...

    @property
    def last_access(self) -> datetime:
        """Timestamp of the most recent ``execute`` call."""
        ...

    @property
    def actual_id(self) -> Optional[str]:
        """The agent-assigned session id (``None`` before first execution)."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Start the subprocess if not running and wait until ready."""
        ...

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str, str], Awaitable[None]],
        on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    ) -> str:
        """Send *task* to the agent and stream responses until completion.

        Args:
            task: The task whose ``content`` is sent as the prompt.
            on_progress: Async callback ``(delta, tool_name) -> None``.
            on_permission: Async callback returning a
                :class:`~nextme.protocol.types.PermissionChoice`.

        Returns:
            Final accumulated text content from the agent.
        """
        ...

    async def cancel(self) -> None:
        """Request cancellation of the in-flight task.  Safe to call if idle."""
        ...

    async def reset_session(self) -> None:
        """Clear the session id so the next ``execute`` starts a fresh session."""
        ...

    async def stop(self) -> None:
        """Terminate the subprocess gracefully.  Safe to call if not running."""
        ...
