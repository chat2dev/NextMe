"""ACPRuntime — one instance per bot session, manages subprocess lifecycle.

Each Session owns exactly one ACPRuntime.  The runtime:

* Launches the ACP subprocess on first use (``ensure_ready``).
* Negotiates capabilities via JSON-RPC ``initialize``.
* Sends prompts and streams responses (``execute``).
* Handles permission round-trips inline via ``session/request_permission``.
* Can cancel an in-flight task (``cancel``).
* Gracefully terminates the subprocess (``stop``).

Protocol
--------
cc-acp speaks **JSON-RPC 2.0** over stdin/stdout (ndjson).

Inbound message kinds (classified by :func:`~nextme.acp.protocol.classify`):
    ``"response"``        — matched response to one of our outbound requests
    ``"notification"``    — push event (``session/update``) with no id
    ``"server_request"``  — cc-acp calling the bot (``session/request_permission``)
"""

from __future__ import annotations

import asyncio
import logging
import os
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
    cancel_params,
    classify,
    initialize_params,
    load_session_params,
    new_session_params,
    parse_permission_request,
    permission_cancel_result,
    permission_response_result,
    prompt_params,
)

logger = logging.getLogger(__name__)

_INIT_TIMEOUT_SECONDS = 30
_SESSION_TIMEOUT_SECONDS = 15
_STOP_GRACEFUL_TIMEOUT_SECONDS = 5

# Prefixes / exact names that must be stripped before passing the env to cc-acp.
# These are set by the *outer* Claude Code host process and cause failures in the
# inner claude process that cc-acp spawns:
#
#   CLAUDECODE              — exact var; triggers "nested session" rejection
#   CLAUDE_CODE_*           — prefix match; covers ENTRYPOINT, EXPERIMENTAL_*,
#                             API_USAGE_TELEMETRY, VERSION, etc. — all mark the
#                             current process as a running Claude Code session
#
# We intentionally KEEP ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL so that
# cc-acp's inner claude authenticates the same way as the outer process.
# (open-jieli: inherit full env + CI=true/TERM=xterm, no auth stripping)
_STRIP_ENV_EXACT: frozenset[str] = frozenset({"CLAUDECODE"})
_STRIP_ENV_PREFIX = "CLAUDE_CODE_"

# Option IDs that represent session-wide approval, ordered by preference.
# If any of these appear in the permission request options, we pick the first
# match so subsequent tool calls in the same session won't trigger again.
_SESSION_ALLOW_OPTION_IDS = ("session_level_allow", "allow_always", "always_allow")


def _pick_auto_approve_option(options: list) -> str:
    """Return the best option_id for auto-approval.

    Preference order:
    1. Any session-wide allow option (avoids repeated permission prompts).
    2. First allow-family option (kind contains "allow").
    3. First option in the list.
    4. Hard-coded ``"allow_once"`` fallback.
    """
    for preferred in _SESSION_ALLOW_OPTION_IDS:
        for opt in options:
            if opt.option_id == preferred:
                return preferred
    for opt in options:
        if "allow" in (opt.kind or "").lower() or "allow" in (opt.option_id or "").lower():
            return opt.option_id
    return options[0].option_id if options else "allow_once"


async def _notify_auto_approved(
    on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    req: PermissionRequest,
) -> None:
    """Call on_permission with a pre-resolved choice for informational display.

    The choice uses option_index=1 (first allow option) so the worker can show
    an informational card without waiting for user input.  Any exception is
    swallowed — this is a best-effort notification.
    """
    try:
        auto_choice = PermissionChoice(request_id=req.request_id, option_index=1)
        # Inject the auto-choice into a synthetic PermissionRequest that has no
        # interactive options (empty list), signalling the worker to skip the
        # permission card's clickable buttons and show a read-only notice.
        info_req = PermissionRequest(
            session_id=req.session_id,
            request_id=req.request_id,
            description=req.description,
            options=[],  # empty → worker builds a non-interactive info card
        )
        await on_permission(info_req)
    except Exception:
        pass  # notification is best-effort


class ACPRuntime:
    """Manages one ACP subprocess and drives the prompt/response lifecycle.

    Args:
        session_id: The bot-level session identifier (``"chatID:userID"``).
        cwd: Working directory for the ACP subprocess.
        settings: Application settings (idle timeout, debounce, …).
        executor: Executable name / path for the ACP subprocess.
        executor_args: Extra arguments appended to *executor* when spawning
            the subprocess (e.g. ``["acp", "serve"]`` for ``coco acp serve``).
    """

    def __init__(
        self,
        session_id: str,
        cwd: str,
        settings: Settings,
        executor: str = "claude-code-acp",
        executor_args: list[str] | None = None,
    ) -> None:
        self._session_id = session_id
        self._cwd = cwd
        self._settings = settings
        self._executor = executor
        self._executor_args: list[str] = list(executor_args) if executor_args else []

        # ACP's own session id (returned in ``session/new`` response).
        self._actual_id: Optional[str] = None

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._client: Optional[ACPClient] = None

        # Background task that drains stderr.
        self._stderr_drain_task: Optional[asyncio.Task] = None

        # Background reader task that feeds _msg_queue.
        self._reader_task: Optional[asyncio.Task] = None

        # Single queue shared by all messages from cc-acp.
        self._msg_queue: Optional[asyncio.Queue] = None

        self._last_access: datetime = datetime.now()
        self._ready: bool = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while the subprocess is alive."""
        return self._proc is not None and self._proc.returncode is None

    @property
    def last_access(self) -> datetime:
        """Timestamp of the most recent ``execute`` call."""
        return self._last_access

    @property
    def actual_id(self) -> Optional[str]:
        """The ACP-assigned session id (set after first ``session/new``)."""
        return self._actual_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Start the subprocess if not running; negotiate via ``initialize``.

        Idempotent: no-op when the subprocess is already live and ready.

        Raises:
            RuntimeError: If initialization times out or fails.
        """
        if self._ready and self.is_running:
            return

        cmd = [self._executor, *self._executor_args]
        logger.info(
            "ACPRuntime[%s]: starting subprocess %r in %r",
            self._session_id,
            cmd,
            self._cwd,
        )

        # Build the child environment: inherit full env, strip CLAUDECODE and
        # CLAUDE_CODE_* (nested-session markers), add CI=true + TERM=xterm.
        # ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL are intentionally kept so
        # cc-acp's inner claude authenticates the same way as the outer process.
        child_env = {
            k: v for k, v in os.environ.items()
            if k not in _STRIP_ENV_EXACT and not k.startswith(_STRIP_ENV_PREFIX)
        }
        child_env["CI"] = "true"
        child_env.setdefault("TERM", "xterm")

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=child_env,
            start_new_session=True,  # process group isolation
        )

        self._client = ACPClient(self._proc)
        self._ready = False
        self._msg_queue = asyncio.Queue()

        self._reader_task = asyncio.create_task(
            self._run_reader(), name=f"acp-reader-{self._session_id}"
        )
        self._stderr_drain_task = asyncio.create_task(
            self._drain_stderr(), name=f"acp-stderr-{self._session_id}"
        )

        # Negotiate capabilities.
        try:
            await asyncio.wait_for(
                self._do_initialize(), timeout=_INIT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await self.stop()
            raise RuntimeError(
                f"ACPRuntime[{self._session_id}]: initialize timed out after {_INIT_TIMEOUT_SECONDS}s"
            )

        logger.info("ACPRuntime[%s]: subprocess is ready", self._session_id)

    async def _do_initialize(self) -> None:
        """Send ``initialize`` and wait for the matching response."""
        assert self._client is not None
        req_id = await self._client.send_request("initialize", initialize_params())
        resp = await self._wait_response(req_id, timeout=_INIT_TIMEOUT_SECONDS)
        proto = resp.get("protocolVersion", "?")
        logger.debug(
            "ACPRuntime[%s]: initialized, protocolVersion=%s", self._session_id, proto
        )
        self._ready = True

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str, str], Awaitable[None]],
        on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    ) -> str:
        """Send *task* to ACP and stream responses until ``session/prompt`` completes.

        Flow
        ----
        1. ``ensure_ready`` — start / verify subprocess.
        2. ``session/new`` or ``session/load`` — create / resume Claude session.
        3. ``session/prompt`` — submit the task content.
        4. Process ``session/update`` notifications:

           * ``agent_message_chunk``  — accumulate text; call ``on_progress``
             (debounced by ``settings.progress_debounce_seconds``).
           * ``tool_call``            — call ``on_progress`` with the tool name.
           * ``agent_thought_chunk``  — silently accumulated (not shown to user).

        5. Handle ``session/request_permission`` inbound server requests
           by calling ``on_permission`` and sending the chosen response.
        6. When the ``session/prompt`` response arrives → return final content.

        Args:
            task: The Task whose ``content`` is sent as the prompt.
            on_progress: Async callback ``(delta: str, tool_name: str) -> None``
                         called on content deltas and tool-use events.
            on_permission: Async callback that receives a
                           :class:`~nextme.protocol.types.PermissionRequest`
                           and must return a
                           :class:`~nextme.protocol.types.PermissionChoice`.

        Returns:
            The final accumulated text content from ACP.

        Raises:
            RuntimeError: On ACP errors or subprocess failure.
        """
        await self.ensure_ready()

        assert self._client is not None
        assert self._msg_queue is not None

        self._last_access = datetime.now()

        # --- Step 1: create or resume session ----------------------------
        if self._actual_id:
            try:
                load_id = await self._client.send_request(
                    "session/load",
                    load_session_params(self._actual_id, self._cwd),
                )
                await self._wait_response(load_id, timeout=_SESSION_TIMEOUT_SECONDS)
                logger.debug(
                    "ACPRuntime[%s]: loaded ACP session %r",
                    self._session_id,
                    self._actual_id,
                )
            except Exception as exc:
                # session/load may fail if the executor doesn't persist sessions
                # across restarts (e.g. coco stores sessions in memory only).
                # Fall through to session/new so the task still runs, albeit
                # without prior conversation context.
                logger.warning(
                    "ACPRuntime[%s]: session/load failed (%s); "
                    "starting fresh session — prior context will be lost",
                    self._session_id,
                    exc,
                )
                self._actual_id = None

        if not self._actual_id:
            new_id = await self._client.send_request(
                "session/new",
                new_session_params(self._cwd),
            )
            resp = await self._wait_response(new_id, timeout=_SESSION_TIMEOUT_SECONDS)
            # Different ACP implementations use different key names for the session
            # id in the session/new response.  Try all known variants.
            self._actual_id = (
                resp.get("sessionId")
                or resp.get("session_id")
                or resp.get("id")
                or resp.get("session")
                or ""
            )
            if self._actual_id:
                logger.debug(
                    "ACPRuntime[%s]: created ACP session %r",
                    self._session_id,
                    self._actual_id,
                )
            else:
                logger.warning(
                    "ACPRuntime[%s]: session/new response contains no session id; "
                    "conversation history will not persist across prompts. "
                    "Response keys: %s",
                    self._session_id,
                    list(resp.keys()),
                )

        # --- Step 2: send prompt -----------------------------------------
        prompt_req_id = await self._client.send_request(
            "session/prompt",
            prompt_params(self._actual_id or "", task.content),
        )

        # --- Step 3: stream messages until the prompt response -----------
        accumulated: list[str] = []
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
                        "ACPRuntime[%s]: on_progress raised: %s", self._session_id, exc
                    )

        timeout_secs = task.timeout.total_seconds()

        while True:
            if task.canceled:
                await self.cancel()
                return "".join(accumulated)

            try:
                msg = await asyncio.wait_for(
                    self._msg_queue.get(), timeout=timeout_secs
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"ACPRuntime[{self._session_id}]: timed out waiting for "
                    f"prompt response after {task.timeout}"
                )

            if isinstance(msg, Exception):
                raise RuntimeError(
                    f"ACPRuntime[{self._session_id}]: reader error: {msg}"
                ) from msg

            kind = classify(msg)

            # --- JSON-RPC response to one of our requests ----------------
            if kind == "response":
                msg_id: int = msg.get("id", -1)

                if msg_id == prompt_req_id:
                    # Final response for the prompt — done.
                    await _flush_progress()
                    if "error" in msg:
                        err_msg = (msg["error"] or {}).get("message", "unknown ACP error")
                        raise RuntimeError(
                            f"ACPRuntime[{self._session_id}]: prompt error: {err_msg}"
                        )
                    final = "".join(accumulated)
                    if not final:
                        # Fallback: some ACP implementations embed the output
                        # directly in the result dict instead of streaming it
                        # as agent_message_chunk notifications.
                        result_data = msg.get("result") or {}
                        for key in ("output", "text", "content"):
                            val = result_data.get(key, "")
                            if isinstance(val, str) and val:
                                final = val
                                logger.debug(
                                    "ACPRuntime[%s]: extracted text from result.%s",
                                    self._session_id,
                                    key,
                                )
                                break
                        if not final:
                            logger.debug(
                                "ACPRuntime[%s]: prompt response has empty accumulated; "
                                "result=%s",
                                self._session_id,
                                str(msg.get("result", {}))[:300],
                            )
                    logger.debug(
                        "ACPRuntime[%s]: prompt done (len=%d)", self._session_id, len(final)
                    )
                    return final
                else:
                    # Stale response from a prior session/new or session/load
                    # that arrived late — safely ignore.
                    logger.debug(
                        "ACPRuntime[%s]: ignoring stale response id=%d", self._session_id, msg_id
                    )

            # --- session/update notification -----------------------------
            elif kind == "notification":
                params: dict = msg.get("params") or {}
                update: dict = params.get("update") or {}
                update_type: str = update.get("sessionUpdate", "")

                if update_type == "agent_message_chunk":
                    content_block = update.get("content") or {}
                    delta: str = content_block.get("text", "")
                    accumulated.append(delta)
                    pending_delta.append(delta)
                    now = time.monotonic()
                    if (now - last_progress_time) >= debounce:
                        await _flush_progress()

                elif update_type == "tool_call":
                    tool_name: str = (
                        update.get("title")
                        or update.get("name")
                        or update.get("tool")
                        or "tool"
                    )
                    await _flush_progress(tool_name=tool_name)

                elif update_type == "agent_thought_chunk":
                    # Internal reasoning — accumulate silently.
                    pass

                else:
                    logger.debug(
                        "ACPRuntime[%s]: unhandled update type %r: %s",
                        self._session_id,
                        update_type,
                        str(update)[:300],
                    )

            # --- session/request_permission (server → client request) ----
            elif kind == "server_request":
                logger.info(
                    "ACPRuntime[%s]: permission request received (jsonrpc_id=%r, method=%r)",
                    self._session_id,
                    msg.get("id"),
                    msg.get("method"),
                )
                await self._handle_permission(msg, on_permission)

            else:
                logger.debug(
                    "ACPRuntime[%s]: unknown message: %s",
                    self._session_id,
                    str(msg)[:200],
                )

    # ------------------------------------------------------------------
    # Permission handling
    # ------------------------------------------------------------------

    async def _handle_permission(
        self,
        msg: dict,
        on_permission: Callable[[PermissionRequest], Awaitable[PermissionChoice]],
    ) -> None:
        """Parse an inbound ``session/request_permission`` request, call
        ``on_permission``, and send the chosen response back to the subprocess.

        Note on ACP subprocess timeouts
        --------------------------------
        Some ACP executors (e.g. coco) impose a short internal timeout on
        permission responses (~10 s).  If the user takes longer than that
        timeout to click the permission card, the subprocess will reject the
        tool call before our response arrives.

        The ``permission_timeout_seconds`` setting controls how long we wait
        for the user.  Set it to a value less than the executor's internal
        timeout (e.g. 8 s for coco) via ``~/.nextme/nextme.json`` so that our
        bot responds before the executor rejects the request.
        """
        if msg.get("method") != "session/request_permission":
            logger.debug(
                "ACPRuntime[%s]: ignoring unknown server request %r",
                self._session_id,
                msg.get("method"),
            )
            return

        assert self._client is not None

        try:
            perm_req = parse_permission_request(msg)
        except Exception as exc:
            logger.warning(
                "ACPRuntime[%s]: failed to parse permission request: %s",
                self._session_id,
                exc,
            )
            await self._client.send_error_response(
                msg.get("id", 0), -32600, "invalid permission request"
            )
            return

        # Map to our internal PermissionRequest type.
        tool_call = perm_req.tool_call or {}
        description = tool_call.get("title") or tool_call.get("description") or ""
        perm_options: list[PermOption] = [
            PermOption(
                index=i + 1,
                label=opt.option_id,
                description=opt.name,
            )
            for i, opt in enumerate(perm_req.options)
        ]

        internal_req = PermissionRequest(
            session_id=self._session_id,
            request_id=perm_req.option_id if hasattr(perm_req, "option_id") else "",
            description=description,
            options=perm_options,
        )

        # ------------------------------------------------------------------
        # Auto-approve mode: respond immediately without waiting for user.
        #
        # Some ACP executors (e.g. coco acp serve) have a very short internal
        # timeout (~2-4 s) for permission responses.  By the time the user
        # receives the Feishu card on mobile and clicks a button, coco has
        # already timed out and rejected the tool call.
        #
        # When permission_auto_approve=True we send the broadest available
        # allow option (session_level_allow > first allow > first option)
        # immediately, then fire on_permission as a background task so the
        # user still sees an informational card (without clickable buttons).
        # ------------------------------------------------------------------
        if self._settings.permission_auto_approve:
            # Pick the best "allow" option: prefer session-wide allow.
            option_id = _pick_auto_approve_option(perm_req.options)
            result = permission_response_result(option_id)
            await self._client.send_response(perm_req.jsonrpc_id, result)
            logger.info(
                "ACPRuntime[%s]: permission auto-approved (option=%r, description=%r)",
                self._session_id,
                option_id,
                description,
            )
            # Notify the user asynchronously (best-effort, does not block).
            asyncio.create_task(
                _notify_auto_approved(on_permission, internal_req),
                name=f"perm-notify-{self._session_id}",
            )
            return

        # Block indefinitely — no timeout, no fallback.
        # CancelledError (e.g. /stop) propagates naturally to the execute loop.
        # Any other unexpected exception sends an error response to the
        # subprocess so it is not left hanging on an unanswered request.
        try:
            choice: PermissionChoice = await on_permission(internal_req)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "ACPRuntime[%s]: on_permission raised unexpectedly: %s",
                self._session_id,
                exc,
            )
            await self._client.send_error_response(
                perm_req.jsonrpc_id, -32000, "permission handling failed"
            )
            return

        # Map the 1-based index back to the option_id string.
        chosen_index = max(1, choice.option_index) - 1
        if perm_req.options and chosen_index < len(perm_req.options):
            option_id = perm_req.options[chosen_index].option_id
        elif perm_req.options:
            option_id = perm_req.options[0].option_id
        else:
            option_id = "allow_once"

        result = permission_response_result(option_id)
        await self._client.send_response(perm_req.jsonrpc_id, result)
        logger.info(
            "ACPRuntime[%s]: permission response sent (option=%r, description=%r)",
            self._session_id,
            option_id,
            description,
        )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        """Send ``session/cancel`` to cc-acp.

        Silently does nothing if the subprocess is not running.
        """
        if not self.is_running or self._client is None or not self._actual_id:
            return
        logger.info("ACPRuntime[%s]: sending cancel", self._session_id)
        try:
            await self._client.send_request("session/cancel", cancel_params(self._actual_id))
        except Exception as exc:
            logger.warning("ACPRuntime[%s]: cancel failed: %s", self._session_id, exc)

    async def reset_session(self) -> None:
        """Clear *actual_id* so the next ``execute`` creates a fresh ACP session."""
        logger.info("ACPRuntime[%s]: resetting ACP session id", self._session_id)
        self._actual_id = None

    async def restore_session(self, actual_id: str) -> None:
        """Set the ACP session id so the next ``execute`` loads a prior session."""
        logger.info(
            "ACPRuntime[%s]: restoring ACP session id %r", self._session_id, actual_id
        )
        self._actual_id = actual_id if actual_id else None

    async def stop(self) -> None:
        """Terminate the ACP subprocess gracefully (SIGTERM → SIGKILL)."""
        if self._proc is None:
            return

        proc = self._proc
        self._proc = None
        self._client = None
        self._ready = False

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
            return

        logger.info("ACPRuntime[%s]: terminating subprocess (SIGTERM)", self._session_id)
        try:
            proc.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACEFUL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "ACPRuntime[%s]: subprocess did not exit, sending SIGKILL",
                self._session_id,
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

    async def _wait_response(self, req_id: int, timeout: float) -> dict:
        """Block until a ``response`` message with *req_id* arrives on the queue.

        Other messages (notifications, server requests) encountered while
        waiting are **re-queued** so they are not lost.

        Raises:
            RuntimeError: On error response or reader exception.
        """
        assert self._msg_queue is not None
        stashed: list = []

        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    msg = await asyncio.wait_for(
                        self._msg_queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"ACPRuntime[{self._session_id}]: timed out waiting "
                        f"for response id={req_id}"
                    )

                if isinstance(msg, Exception):
                    raise RuntimeError(
                        f"ACPRuntime[{self._session_id}]: reader error: {msg}"
                    ) from msg

                kind = classify(msg)
                if kind == "response" and msg.get("id") == req_id:
                    if "error" in msg:
                        err = (msg["error"] or {}).get("message", "unknown error")
                        raise RuntimeError(
                            f"ACPRuntime[{self._session_id}]: RPC error: {err}"
                        )
                    return msg.get("result") or {}
                else:
                    # Re-queue so the execute loop can process it.
                    stashed.append(msg)
        finally:
            for m in stashed:
                await self._msg_queue.put(m)

    async def _run_reader(self) -> None:
        """Read all stdout lines and push parsed dicts into ``_msg_queue``."""
        assert self._client is not None
        assert self._msg_queue is not None

        try:
            async for msg in self._client.read_lines():
                await self._msg_queue.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ACPRuntime[%s]: reader error: %s", self._session_id, exc)
            await self._msg_queue.put(exc)
        else:
            # Normal EOF — subprocess closed stdout before we received the
            # final session/prompt response.  Put a sentinel so the execute
            # loop unblocks and surfaces a clear error instead of hanging.
            logger.info("ACPRuntime[%s]: subprocess stdout closed (EOF)", self._session_id)
            await self._msg_queue.put(
                EOFError(f"ACPRuntime[{self._session_id}]: subprocess closed stdout unexpectedly")
            )

    async def _drain_stderr(self) -> None:
        """Continuously read stderr to prevent pipe blockage."""
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
                logger.info("ACPRuntime[%s] stderr: %s", self._session_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("ACPRuntime[%s]: stderr drain ended: %s", self._session_id, exc)
