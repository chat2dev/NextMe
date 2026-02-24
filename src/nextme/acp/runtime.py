"""ACPRuntime — one instance per bot session, manages subprocess lifecycle.

Each Session owns exactly one ACPRuntime.  The runtime:

* Launches the ACP subprocess on first use (``ensure_ready``).
* Sends prompts and streams responses (``execute``).
* Handles permission round-trips inline.
* Can cancel an in-flight task (``cancel``).
* Gracefully terminates the subprocess (``stop``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from ..config.schema import Settings
from ..protocol.types import (
    PermissionChoice,
    PermissionRequest,
    PermOption,
    Task,
)
from .client import ACPClient
from .protocol import (
    CancelMsg,
    LoadSessionMsg,
    NewSessionMsg,
    PermissionResponseMsg,
    PromptMsg,
)

logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 30
_STOP_GRACEFUL_TIMEOUT_SECONDS = 5


class ACPRuntime:
    """Manages one ACP subprocess and drives the prompt/response lifecycle.

    Args:
        session_id: The bot-level session identifier (``"chatID:userID"``).
        cwd: Working directory for the ACP subprocess.
        settings: Application settings (idle timeout, debounce, …).
        executor: Executable name / path for the ACP subprocess.
    """

    def __init__(
        self,
        session_id: str,
        cwd: str,
        settings: Settings,
        executor: str = "claude-code-acp",
    ) -> None:
        self._session_id = session_id
        self._cwd = cwd
        self._settings = settings
        self._executor = executor

        # ACP's own session id (returned in ``session_created`` messages).
        self._actual_id: Optional[str] = None

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._client: Optional[ACPClient] = None

        # Tracks the background task that drains stderr.
        self._stderr_drain_task: Optional[asyncio.Task] = None

        # A single asyncio.Queue used to multiplex ACP messages to the
        # current ``execute`` call.  Replaced on every ``execute`` call.
        self._msg_queue: Optional[asyncio.Queue] = None

        # Background reader task that feeds _msg_queue.
        self._reader_task: Optional[asyncio.Task] = None

        self._last_access: datetime = datetime.now()
        self._ready: bool = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while the subprocess is alive."""
        return (
            self._proc is not None
            and self._proc.returncode is None
        )

    @property
    def last_access(self) -> datetime:
        """Timestamp of the most recent ``execute`` call."""
        return self._last_access

    @property
    def actual_id(self) -> Optional[str]:
        """The ACP-assigned session id (set after first ``session_created``)."""
        return self._actual_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Start the subprocess if not running; wait for the ``ready`` message.

        Idempotent: calling it when the subprocess is already live is a no-op.

        Raises:
            RuntimeError: If the subprocess fails to send ``ready`` within
                :data:`_READY_TIMEOUT_SECONDS`.
        """
        if self._ready and self.is_running:
            return

        logger.info(
            "ACPRuntime[%s]: starting subprocess %r in %r",
            self._session_id,
            self._executor,
            self._cwd,
        )

        self._proc = await asyncio.create_subprocess_exec(
            self._executor,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            start_new_session=True,  # process group isolation
        )

        self._client = ACPClient(self._proc)
        self._ready = False

        # Start the persistent stdout reader that feeds the message queue.
        self._msg_queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(
            self._run_reader(), name=f"acp-reader-{self._session_id}"
        )

        # Drain stderr in the background so the pipe never blocks.
        self._stderr_drain_task = asyncio.create_task(
            self._drain_stderr(), name=f"acp-stderr-{self._session_id}"
        )

        # Wait for the ready message.
        try:
            await asyncio.wait_for(
                self._wait_for_ready(), timeout=_READY_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise RuntimeError(
                f"ACPRuntime[{self._session_id}]: timed out waiting for 'ready' "
                f"after {_READY_TIMEOUT_SECONDS}s"
            )

        logger.info("ACPRuntime[%s]: subprocess is ready", self._session_id)

    async def _wait_for_ready(self) -> None:
        """Block until a ``ready`` message arrives on the queue."""
        assert self._msg_queue is not None
        while True:
            msg = await self._msg_queue.get()
            if isinstance(msg, Exception):
                raise msg
            if msg.get("type") == "ready":
                self._ready = True
                return
            # Other messages before ready are unexpected but not fatal.
            logger.debug(
                "ACPRuntime[%s]: received %r before ready, ignoring",
                self._session_id,
                msg.get("type"),
            )

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str, str], Awaitable[None]],
        on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    ) -> str:
        """Send *task* to ACP and stream responses until ``done`` or ``error``.

        Flow
        ----
        1. Ensure the subprocess is running (``ensure_ready``).
        2. Send ``load_session`` (if a prior ACP session id exists) or
           ``new_session`` (first call or after ``reset_session``).
        3. Send a ``prompt`` message with the task content.
        4. Stream messages from the queue:

           * ``session_created`` — record the ACP session id.
           * ``content_delta``   — accumulate text; call ``on_progress``
             (debounced by ``settings.progress_debounce_seconds``).
           * ``tool_use``        — call ``on_progress`` with the tool name.
           * ``permission_request`` — call ``on_permission``, then send a
             ``permission_response``.
           * ``done``            — return accumulated / final content.
           * ``error``           — raise ``RuntimeError``.

        Args:
            task: The Task whose ``content`` is sent as the prompt.
            on_progress: Async callback ``(delta: str, tool_name: str) -> None``
                         invoked on content deltas and tool-use events.
            on_permission: Async callback that receives a
                           :class:`~nextme.protocol.types.PermissionRequest`
                           and must return a
                           :class:`~nextme.protocol.types.PermissionChoice`.

        Returns:
            The final accumulated text content from ACP.

        Raises:
            RuntimeError: On ACP-reported errors or subprocess failure.
        """
        await self.ensure_ready()

        assert self._client is not None
        assert self._msg_queue is not None

        self._last_access = datetime.now()

        # --- Step 1-2: session setup -------------------------------------
        if self._actual_id:
            logger.debug(
                "ACPRuntime[%s]: loading existing ACP session %r",
                self._session_id,
                self._actual_id,
            )
            await self._client.send(LoadSessionMsg(session_id=self._actual_id))
        else:
            logger.debug(
                "ACPRuntime[%s]: creating new ACP session", self._session_id
            )
            await self._client.send(
                NewSessionMsg(session_id=self._session_id, cwd=self._cwd)
            )

        # --- Step 3: send prompt -----------------------------------------
        await self._client.send(
            PromptMsg(session_id=self._session_id, content=task.content)
        )

        # --- Step 4: stream responses ------------------------------------
        accumulated_content: list[str] = []
        pending_delta: list[str] = []
        last_progress_time: float = 0.0
        debounce: float = self._settings.progress_debounce_seconds

        async def _flush_progress(tool_name: str = "") -> None:
            nonlocal last_progress_time
            if pending_delta or tool_name:
                combined = "".join(pending_delta)
                pending_delta.clear()
                last_progress_time = time.monotonic()
                try:
                    await on_progress(combined, tool_name)
                except Exception as exc:
                    logger.warning(
                        "ACPRuntime[%s]: on_progress callback raised: %s",
                        self._session_id,
                        exc,
                    )

        while True:
            # Respect task cancellation flag.
            if task.canceled:
                await self.cancel()
                return "".join(accumulated_content)

            try:
                msg = await asyncio.wait_for(
                    self._msg_queue.get(),
                    timeout=task.timeout.total_seconds(),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"ACPRuntime[{self._session_id}]: timed out waiting for ACP "
                    f"response after {task.timeout}"
                )

            if isinstance(msg, Exception):
                raise RuntimeError(
                    f"ACPRuntime[{self._session_id}]: subprocess error: {msg}"
                ) from msg

            msg_type: str = msg.get("type", "")

            if msg_type == "session_created":
                self._actual_id = msg.get("session_id") or self._actual_id
                logger.debug(
                    "ACPRuntime[%s]: ACP session_id set to %r",
                    self._session_id,
                    self._actual_id,
                )

            elif msg_type == "content_delta":
                delta: str = msg.get("delta") or msg.get("content") or ""
                accumulated_content.append(delta)
                pending_delta.append(delta)

                now = time.monotonic()
                if (now - last_progress_time) >= debounce:
                    await _flush_progress()

            elif msg_type == "tool_use":
                tool_name: str = msg.get("name") or msg.get("tool_name") or ""
                # Flush any accumulated delta first, then emit the tool event.
                await _flush_progress(tool_name=tool_name)

            elif msg_type == "permission_request":
                # Flush progress before pausing for user input.
                await _flush_progress()

                request_id: str = msg.get("request_id", "")
                description: str = msg.get("description", "")
                raw_options: list = msg.get("options") or []

                perm_options: list[PermOption] = [
                    PermOption(
                        index=opt.get("index", i + 1),
                        label=opt.get("label", ""),
                        description=opt.get("description", ""),
                    )
                    for i, opt in enumerate(raw_options)
                ]

                perm_request = PermissionRequest(
                    session_id=self._session_id,
                    request_id=request_id,
                    description=description,
                    options=perm_options,
                )

                try:
                    choice: PermissionChoice = await asyncio.wait_for(
                        on_permission(perm_request),
                        timeout=self._settings.permission_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "ACPRuntime[%s]: permission request timed out, defaulting to 1",
                        self._session_id,
                    )
                    choice = PermissionChoice(
                        request_id=request_id, option_index=1
                    )
                except Exception as exc:
                    logger.warning(
                        "ACPRuntime[%s]: on_permission callback raised: %s; defaulting to 1",
                        self._session_id,
                        exc,
                    )
                    choice = PermissionChoice(
                        request_id=request_id, option_index=1
                    )

                await self._client.send(
                    PermissionResponseMsg(
                        request_id=choice.request_id,
                        choice=choice.option_index,
                    )
                )

            elif msg_type == "done":
                # Final flush of any remaining buffered delta.
                await _flush_progress()

                # Prefer the explicit ``content`` field when present (ACP may
                # send the full final text here).
                final_content: str = msg.get("content") or "".join(accumulated_content)
                logger.debug(
                    "ACPRuntime[%s]: task done, content length=%d",
                    self._session_id,
                    len(final_content),
                )
                return final_content

            elif msg_type == "error":
                error_msg: str = msg.get("message") or msg.get("error") or "unknown ACP error"
                raise RuntimeError(
                    f"ACPRuntime[{self._session_id}]: ACP reported error: {error_msg}"
                )

            else:
                logger.debug(
                    "ACPRuntime[%s]: unhandled message type %r",
                    self._session_id,
                    msg_type,
                )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        """Send a ``cancel`` message to the ACP subprocess.

        Silently does nothing if the subprocess is not running.
        """
        if not self.is_running or self._client is None:
            return
        logger.info("ACPRuntime[%s]: sending cancel", self._session_id)
        try:
            await self._client.send(CancelMsg(session_id=self._session_id))
        except Exception as exc:
            logger.warning(
                "ACPRuntime[%s]: error sending cancel: %s", self._session_id, exc
            )

    async def reset_session(self) -> None:
        """Clear *actual_id* so the next ``execute`` creates a fresh ACP session."""
        logger.info("ACPRuntime[%s]: resetting ACP session id", self._session_id)
        self._actual_id = None

    async def stop(self) -> None:
        """Terminate the ACP subprocess gracefully.

        Sends SIGTERM first; if the process is still alive after
        :data:`_STOP_GRACEFUL_TIMEOUT_SECONDS`, sends SIGKILL.
        """
        if self._proc is None:
            return

        proc = self._proc
        self._proc = None
        self._client = None
        self._ready = False

        # Cancel background tasks.
        for bg_task in (self._reader_task, self._stderr_drain_task):
            if bg_task is not None and not bg_task.done():
                bg_task.cancel()
                try:
                    await bg_task
                except (asyncio.CancelledError, Exception):
                    pass

        self._reader_task = None
        self._stderr_drain_task = None

        if proc.returncode is not None:
            return  # already exited

        logger.info("ACPRuntime[%s]: stopping subprocess (SIGTERM)", self._session_id)
        try:
            proc.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACEFUL_TIMEOUT_SECONDS)
            logger.info(
                "ACPRuntime[%s]: subprocess exited with code %d",
                self._session_id,
                proc.returncode,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ACPRuntime[%s]: subprocess did not exit in %ds, sending SIGKILL",
                self._session_id,
                _STOP_GRACEFUL_TIMEOUT_SECONDS,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_reader(self) -> None:
        """Read all stdout lines and push parsed dicts into ``_msg_queue``.

        Runs as a background asyncio task for the lifetime of the subprocess.
        When stdout reaches EOF (subprocess exited), ``None`` is pushed so
        that any awaiting ``execute`` call can detect the EOF condition.
        """
        assert self._client is not None
        assert self._msg_queue is not None

        try:
            async for msg in self._client.read_lines():
                await self._msg_queue.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "ACPRuntime[%s]: reader task error: %s", self._session_id, exc
            )
            await self._msg_queue.put(exc)

    async def _drain_stderr(self) -> None:
        """Continuously read and log stderr so the pipe never fills up."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line_bytes = await self._proc.stderr.readline()
                if not line_bytes:
                    break
                try:
                    line = line_bytes.decode("utf-8").rstrip()
                except UnicodeDecodeError:
                    line = repr(line_bytes)
                logger.debug("ACPRuntime[%s] stderr: %s", self._session_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "ACPRuntime[%s]: stderr drain ended: %s", self._session_id, exc
            )
