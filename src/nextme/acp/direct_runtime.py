"""DirectClaudeRuntime — drives the locally-installed ``claude`` CLI directly.

Bypasses cc-acp entirely.  Uses ``claude --print --output-format stream-json``
to submit prompts and parses the streaming ndjson events.

Why not cc-acp?
---------------
cc-acp bundles ``@anthropic-ai/claude-code@1.0.128`` internally and uses its
own auth-checking logic.  When running NextMe through a custom proxy endpoint
(``ANTHROPIC_BASE_URL``) the bundled old version is incompatible and exits with
code 1.  The locally installed ``claude`` CLI (v2.x) already works correctly
with the user's proxy, so calling it directly is simpler and more reliable.

stream-json event types (claude v2+)
--------------------------------------
``system``      — init event; carries ``session_id``, ``model``, tools list
``assistant``   — text/tool content from the model (may arrive in chunks)
``tool_use``    — a tool is being invoked; carries ``name`` and ``input``
``tool_result`` — tool execution result
``result``      — final event; ``subtype="success"`` or ``subtype="error_*"``

Session continuity
------------------
The ``session_id`` from the ``result`` event is stored in ``_actual_id`` and
passed as ``--resume SESSION_ID`` on subsequent calls so the conversation
context is preserved across multiple prompts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from ..config.schema import Settings
from ..protocol.types import PermissionChoice, PermissionRequest, Task

logger = logging.getLogger(__name__)

_STOP_GRACEFUL_TIMEOUT_SECONDS = 5


class DirectClaudeRuntime:
    """Drives a locally-installed ``claude`` CLI without going through cc-acp.

    Satisfies the :class:`~nextme.core.interfaces.AgentRuntime` protocol.

    Each ``execute()`` call spawns a fresh ``claude --print`` subprocess.
    Conversation history is preserved via ``--resume <session_id>``.

    Args:
        session_id: The bot-level session identifier (``"chatID:userID"``).
        cwd: Working directory for the claude subprocess.
        settings: Application settings (debounce, timeouts, …).
        executor: Path or name of the claude binary.  Defaults to ``"claude"``.
    """

    def __init__(
        self,
        session_id: str,
        cwd: str,
        settings: Settings,
        executor: str = "claude",
    ) -> None:
        self._session_id = session_id
        self._cwd = cwd
        self._settings = settings
        self._executor = executor

        # Claude's own session id (from the ``result`` event ``session_id``).
        self._actual_id: Optional[str] = None

        self._last_access: datetime = datetime.now()
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._cancel_flag: bool = False

    # ------------------------------------------------------------------
    # Public properties (AgentRuntime protocol)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while a claude subprocess is active."""
        return self._current_proc is not None and self._current_proc.returncode is None

    @property
    def last_access(self) -> datetime:
        """Timestamp of the most recent ``execute`` call."""
        return self._last_access

    @property
    def actual_id(self) -> Optional[str]:
        """The claude session id (set after first successful execute)."""
        return self._actual_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """No-op — DirectClaudeRuntime spawns fresh processes per prompt."""

    async def stop(self) -> None:
        """Terminate any running claude subprocess."""
        proc = self._current_proc
        if proc is None or proc.returncode is not None:
            return
        logger.info("DirectClaudeRuntime[%s]: terminating subprocess", self._session_id)
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACEFUL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
        self._current_proc = None

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str, str], Awaitable[None]],
        on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    ) -> str:
        """Run *task* with the local claude CLI and stream the response.

        Flow
        ----
        1. Build ``claude --print --output-format stream-json [--resume ID]``.
        2. Write ``task.content`` to stdin, then close it.
        3. Parse ndjson events from stdout:

           * ``system``   — capture ``session_id``.
           * ``assistant`` — accumulate text; call ``on_progress`` (debounced).
           * ``tool_use``  — call ``on_progress`` with tool name.
           * ``result``    — final event; raise on error; return compiled text.

        ``on_permission`` is **not** invoked because
        ``--dangerously-skip-permissions`` auto-approves all tool calls.
        This keeps the integration simple; opt-in permission gating can be
        added later via ``--permission-prompt-tool``.
        """
        self._last_access = datetime.now()
        self._cancel_flag = False

        args = [
            self._executor,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self._actual_id:
            args.extend(["--resume", self._actual_id])

        # Inherit full env — the locally installed claude already handles
        # ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL correctly.
        # Strip CLAUDECODE and CLAUDE_CODE_* to prevent "nested session" errors
        # when NextMe itself is running inside a Claude Code terminal.
        env = {
            k: v for k, v in os.environ.items()
            if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
        }
        env["CI"] = "true"
        env.setdefault("TERM", "xterm")

        logger.info(
            "DirectClaudeRuntime[%s]: spawning %r in %r (session=%s)",
            self._session_id,
            self._executor,
            self._cwd,
            self._actual_id or "new",
        )

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
        )
        self._current_proc = proc

        # Feed prompt via stdin, then close the write-end.
        try:
            proc.stdin.write(task.content.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self.stop()
            raise RuntimeError(
                f"DirectClaudeRuntime[{self._session_id}]: stdin write failed: {exc}"
            ) from exc

        # Drain stderr asynchronously to prevent pipe blockage.
        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                async for raw in proc.stderr:
                    logger.debug(
                        "DirectClaudeRuntime[%s] stderr: %s",
                        self._session_id,
                        raw.decode("utf-8", errors="replace").rstrip(),
                    )
            except Exception:
                pass

        stderr_task = asyncio.create_task(
            _drain_stderr(), name=f"direct-stderr-{self._session_id}"
        )

        accumulated: list[str] = []
        last_progress_time: float = 0.0
        debounce: float = self._settings.progress_debounce_seconds
        timeout_secs = task.timeout.total_seconds()

        async def _flush_progress(delta: str = "", tool_name: str = "") -> None:
            nonlocal last_progress_time
            if delta or tool_name:
                last_progress_time = time.monotonic()
                try:
                    await on_progress(delta, tool_name)
                except Exception as exc:
                    logger.warning(
                        "DirectClaudeRuntime[%s]: on_progress raised: %s",
                        self._session_id,
                        exc,
                    )

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + timeout_secs

            async for raw_line in proc.stdout:
                if self._cancel_flag:
                    logger.info(
                        "DirectClaudeRuntime[%s]: cancelled mid-stream", self._session_id
                    )
                    proc.terminate()
                    return "".join(accumulated)

                if time.monotonic() > deadline:
                    proc.terminate()
                    raise RuntimeError(
                        f"DirectClaudeRuntime[{self._session_id}]: timed out after {task.timeout}"
                    )

                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug(
                        "DirectClaudeRuntime[%s]: non-JSON line: %s",
                        self._session_id,
                        text[:100],
                    )
                    continue

                etype: str = event.get("type", "")

                if etype == "system":
                    # init event — capture session_id early
                    sid = event.get("session_id")
                    if sid:
                        self._actual_id = sid
                    logger.debug(
                        "DirectClaudeRuntime[%s]: system init, model=%s session=%s",
                        self._session_id,
                        event.get("model"),
                        sid,
                    )

                elif etype == "assistant":
                    # Text/content blocks from the model (may be chunked).
                    msg = event.get("message") or {}
                    for block in msg.get("content") or []:
                        if block.get("type") == "text":
                            delta: str = block.get("text", "")
                            if delta:
                                accumulated.append(delta)
                                now = time.monotonic()
                                if (now - last_progress_time) >= debounce:
                                    await _flush_progress(delta=delta)

                elif etype == "tool_use":
                    tool_name: str = event.get("name") or "tool"
                    logger.debug(
                        "DirectClaudeRuntime[%s]: tool_use %r",
                        self._session_id,
                        tool_name,
                    )
                    await _flush_progress(tool_name=tool_name)

                elif etype == "result":
                    # Final event — always update session_id.
                    sid = event.get("session_id")
                    if sid:
                        self._actual_id = sid

                    if event.get("is_error"):
                        err_text = event.get("result") or "unknown error"
                        raise RuntimeError(
                            f"DirectClaudeRuntime[{self._session_id}]: {err_text}"
                        )

                    # Prefer the compiled ``result`` field over accumulated chunks
                    # (it is the canonical final answer).
                    final = event.get("result") or "".join(accumulated)
                    logger.debug(
                        "DirectClaudeRuntime[%s]: done (len=%d, session=%s)",
                        self._session_id,
                        len(final),
                        self._actual_id,
                    )
                    return final

                else:
                    logger.debug(
                        "DirectClaudeRuntime[%s]: unhandled event type %r",
                        self._session_id,
                        etype,
                    )

        finally:
            stderr_task.cancel()
            self._current_proc = None

        # stdout exhausted without a ``result`` event — return what we have.
        return "".join(accumulated)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        """Signal the in-flight execute to stop after the current chunk."""
        self._cancel_flag = True
        proc = self._current_proc
        if proc is not None and proc.returncode is None:
            logger.info("DirectClaudeRuntime[%s]: sending SIGTERM", self._session_id)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def reset_session(self) -> None:
        """Clear the stored session id so the next execute starts fresh."""
        logger.info("DirectClaudeRuntime[%s]: resetting session id", self._session_id)
        self._actual_id = None
