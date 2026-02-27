"""SessionWorker — serial task consumer for a single Session.

Each :class:`Session` has at most one :class:`SessionWorker` running as an
asyncio Task.  The worker:

1. Pulls :class:`~nextme.protocol.types.Task` objects from the session queue.
2. Acquires the :class:`~nextme.core.path_lock.PathLockRegistry` lock for the
   project path so no two sessions write to the same directory concurrently.
3. Drives :class:`~nextme.acp.runtime.ACPRuntime` (``ensure_ready`` +
   ``execute``).
4. Streams progress updates (debounced) and final results back via the task's
   ``reply_fn``.
5. Handles task cancellation, errors, and permission requests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Optional

from ..acp.janitor import ACPRuntimeRegistry
from ..config.schema import Settings
from ..config.state_store import StateStore
from ..memory.manager import MemoryManager
from ..protocol.types import (
    PermissionChoice,
    PermissionRequest,
    Reply,
    ReplyType,
    Task,
    TaskStatus,
)
from .interfaces import Replier
from .path_lock import PathLockRegistry
from .session import Session

logger = logging.getLogger(__name__)


def _format_elapsed(seconds: int) -> str:
    """Return a compact human-readable elapsed time string (e.g. '5s', '1m 30s')."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


class SessionWorker:
    """Serially consume tasks from a :class:`~nextme.core.session.Session`'s queue.

    Args:
        session: The session this worker is bound to.
        acp_registry: Global registry of :class:`~nextme.acp.runtime.ACPRuntime` instances.
        replier: Helper for sending Feishu messages / cards.
        settings: Application settings.
        path_lock_registry: Global path-based lock registry.
    """

    def __init__(
        self,
        session: Session,
        acp_registry: ACPRuntimeRegistry,
        replier: Replier,
        settings: Settings,
        path_lock_registry: PathLockRegistry,
        state_store: Optional[StateStore] = None,
        memory_manager: Optional[MemoryManager] = None,
    ) -> None:
        self._session = session
        self._acp_registry = acp_registry
        self._replier = replier
        self._settings = settings
        self._path_lock_registry = path_lock_registry
        self._state_store = state_store
        self._memory_manager = memory_manager

        # State maintained across _on_progress calls for a single task.
        self._progress_message_id: Optional[str] = None
        self._progress_buffer: list[str] = []
        self._last_progress_update: float = 0.0
        self._task_start: float = 0.0
        self._active_message_id: str = ""   # original Feishu message_id
        self._active_in_thread: bool = False  # thread vs quote-reply mode

        # Streaming card state (reset per-task).
        self._card_id: Optional[str] = None   # cardkit card_id (None = use fallback)
        self._sequence: int = 0               # strictly-increasing sequence counter

    @property
    def _proj(self) -> str:
        """Return a bracketed project name tag for card titles, e.g. '【myproject】'."""
        return f"【{self._session.project_name}】"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Consume tasks from the session queue until cancelled.

        This coroutine is designed to run as an ``asyncio.Task``.
        Cancellation is handled gracefully: the active task (if any) is
        cancelled before the loop exits.
        """
        logger.info(
            "SessionWorker[%s]: started for project %r",
            self._session.context_id,
            self._session.project_name,
        )
        try:
            while True:
                task = await self._session.task_queue.get()

                # Remove from pending list now that it has been dequeued.
                try:
                    self._session.pending_tasks.remove(task)
                except ValueError:
                    pass

                if task.canceled:
                    logger.debug(
                        "SessionWorker[%s]: skipping already-cancelled task %s",
                        self._session.context_id,
                        task.id,
                    )
                    self._session.task_queue.task_done()
                    continue

                self._session.active_task = task
                try:
                    await self._execute_task(task)
                except asyncio.CancelledError:
                    logger.info(
                        "SessionWorker[%s]: worker cancelled during task %s",
                        self._session.context_id,
                        task.id,
                    )
                    # Mark task as cancelled and send feedback before re-raising.
                    task.canceled = True
                    self._session.status = TaskStatus.CANCELED
                    await self._send_cancelled(task)
                    raise
                finally:
                    self._session.active_task = None
                    self._session.status = TaskStatus.IDLE
                    self._session.task_queue.task_done()

        except asyncio.CancelledError:
            logger.info(
                "SessionWorker[%s]: shutting down", self._session.context_id
            )
            raise
        except Exception:
            logger.exception(
                "SessionWorker[%s]: unhandled error in run loop",
                self._session.context_id,
            )

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    async def _execute_task(self, task: Task) -> None:
        """Execute *task* end-to-end with progress and permission handling.

        Steps:
        1. Send an initial "思考中..." progress card.
        2. Acquire path lock for the project directory.
        3. Ensure the ACP runtime is ready.
        4. Execute via ACP, streaming progress and handling permissions.
        5. Send the final result card.
        """
        self._session.status = TaskStatus.EXECUTING

        # Reset per-task progress state.
        self._progress_message_id = None
        self._progress_buffer = []
        self._last_progress_update = 0.0
        self._task_start = time.monotonic()
        self._active_message_id = task.message_id
        self._active_in_thread = task.chat_type == "group"
        self._card_id = None
        self._sequence = 0

        # group → thread reply; p2p → quote reply; no message_id → top-level.
        in_thread = self._active_in_thread

        # Step 1 — Try cardkit-first (true streaming) then fall back to a
        # regular im/v1 card with debounced full-card updates.
        chat_id = self._session.context_id.split(":")[0]
        try:
            streaming_card = self._replier.build_streaming_progress_card(
                content="思考中...",
                title=f"思考中... {self._proj}",
            )
            card_id = await self._replier.create_card(streaming_card)
        except Exception:
            card_id = ""

        streaming_ok = False
        if card_id:
            # Cardkit-first: reference the cardkit card_id from im/v1.
            self._card_id = card_id
            try:
                if task.message_id:
                    sent_id = await self._replier.reply_card_by_id(
                        task.message_id, card_id, in_thread=in_thread
                    )
                else:
                    sent_id = await self._replier.send_card_by_id(chat_id, card_id)
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to send streaming progress card",
                    self._session.context_id,
                )
                sent_id = ""

            if sent_id:
                self._progress_message_id = sent_id
                streaming_ok = True
                logger.debug(
                    "SessionWorker[%s]: streaming card card_id=%s message_id=%s",
                    self._session.context_id,
                    card_id,
                    sent_id,
                )
            else:
                # Feishu rejected the card_id reference (e.g. 230099) —
                # clear streaming mode and fall through to the regular card path.
                logger.warning(
                    "SessionWorker[%s]: streaming card send returned empty "
                    "(card_id=%s), falling back to regular card",
                    self._session.context_id,
                    card_id,
                )
                self._card_id = None

        if not streaming_ok:
            # Fallback: regular im/v1 card with debounced full-card PATCH.
            self._card_id = None
            initial_card = self._replier.build_progress_card(
                status="",
                content="思考中...",
                title=f"思考中... {self._proj}",
            )
            try:
                if task.message_id:
                    self._progress_message_id = await self._replier.reply_card(
                        task.message_id, initial_card, in_thread=in_thread
                    )
                else:
                    self._progress_message_id = await self._replier.send_card(
                        chat_id, initial_card
                    )
                logger.debug(
                    "SessionWorker[%s]: sent initial progress card message_id=%s",
                    self._session.context_id,
                    self._progress_message_id,
                )
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to send initial progress card",
                    self._session.context_id,
                )

        # Step 2 — Acquire path lock.
        path_lock = self._path_lock_registry.get(self._session.project_path)
        self._session.status = TaskStatus.WAITING_LOCK
        logger.debug(
            "SessionWorker[%s]: acquiring path lock for %s",
            self._session.context_id,
            self._session.project_path,
        )
        async with path_lock:
            self._session.status = TaskStatus.EXECUTING

            # Step 3 — Obtain and ready the ACP runtime.
            runtime = self._acp_registry.get_or_create(
                session_id=f"{self._session.context_id}:{self._session.project_name}",
                cwd=str(self._session.project_path),
                settings=self._settings,
                executor=self._session.executor,
                executor_args=self._session.executor_args,
            )

            try:
                await runtime.ensure_ready()
            except Exception as exc:
                logger.error(
                    "SessionWorker[%s]: ACPRuntime.ensure_ready failed: %s",
                    self._session.context_id,
                    exc,
                )
                await self._send_error(task, str(exc))
                self._session.status = TaskStatus.DONE
                return

            # Sync actual_id from runtime to session (may be set from prior run).
            if runtime.actual_id and not self._session.actual_id:
                self._session.actual_id = runtime.actual_id

            # Restore persisted session id to runtime (enables --resume on restart).
            if not runtime.actual_id and self._state_store is not None:
                persisted_id = self._state_store.get_project_actual_id(
                    self._session.context_id, self._session.project_name
                )
                if persisted_id:
                    await runtime.restore_session(persisted_id)
                    self._session.actual_id = persisted_id
                    logger.info(
                        "SessionWorker[%s]: restored session id %r for project %r",
                        self._session.context_id,
                        persisted_id,
                        self._session.project_name,
                    )

            # Inject user memory facts into the task prompt for new sessions only.
            # Facts are keyed by user_id (global across all chats) not context_id.
            if not runtime.actual_id and self._memory_manager is not None:
                user_id = self._session.context_id.rsplit(":", 1)[-1]
                try:
                    await self._memory_manager.load(user_id)
                except Exception:
                    logger.exception(
                        "SessionWorker[%s]: failed to load memory", self._session.context_id
                    )
                facts = self._memory_manager.get_top_facts(user_id, n=10)
                if facts:
                    fact_lines = "\n".join(f"- {f.text}" for f in facts)
                    task = dataclasses.replace(
                        task,
                        content=f"[用户记忆]\n{fact_lines}\n\n[用户消息]\n{task.content}",
                    )
                    logger.debug(
                        "SessionWorker[%s]: injected %d memory facts",
                        self._session.context_id,
                        len(facts),
                    )

            # Step 4 — Execute.
            try:
                final_content = await runtime.execute(
                    task=task,
                    on_progress=self._on_progress,
                    on_permission=self._on_permission,
                )
            except asyncio.CancelledError:
                self._session.status = TaskStatus.CANCELED
                await self._send_cancelled(task)
                raise
            except Exception as exc:
                logger.error(
                    "SessionWorker[%s]: ACPRuntime.execute failed: %s",
                    self._session.context_id,
                    exc,
                )
                self._session.status = TaskStatus.DONE
                await self._send_error(task, str(exc))
                return

            # Sync actual_id back to session after execute.
            if runtime.actual_id:
                self._session.actual_id = runtime.actual_id

            # Persist session id for restart resumption.
            if self._state_store is not None and runtime.actual_id:
                self._state_store.save_project_actual_id(
                    self._session.context_id,
                    self._session.project_name,
                    runtime.actual_id,
                )

        # Step 5 — Send final result card.
        self._session.status = TaskStatus.DONE

        if task.canceled:
            await self._send_cancelled(task)
            return

        await self._send_result(task, final_content)

    # ------------------------------------------------------------------
    # Progress callback
    # ------------------------------------------------------------------

    async def _on_progress(self, delta: str, tool_name: str) -> None:
        """Receive a progress delta from ACPRuntime and forward to the card.

        Two modes:
        - **Streaming** (``_card_id`` set): directly patches card elements via
          the cardkit API — no debounce needed, no full card re-render.
        - **Fallback** (``_card_id`` is None): accumulates text and rebuilds
          the full card on a debounce timer (``progress_debounce_seconds``).

        Args:
            delta: Text delta emitted by the ACP subprocess.
            tool_name: Name (and optional args) of the tool being invoked.
        """
        if delta:
            self._progress_buffer.append(delta)

        # ------------------------------------------------------------------
        # Streaming path — cardkit element-level updates (no debounce).
        # ------------------------------------------------------------------
        if self._card_id:
            elapsed_str = _format_elapsed(int(time.monotonic() - self._task_start))
            if delta:
                self._sequence += 1
                try:
                    await self._replier.stream_append_text(
                        self._card_id, delta, self._sequence
                    )
                except Exception as exc:
                    logger.debug(
                        "SessionWorker[%s]: stream_append_text failed (seq=%d): %s",
                        self._session.context_id,
                        self._sequence,
                        exc,
                    )
            if tool_name:
                # Append a formatted tool-status line inline (avoids the PUT/content
                # endpoint which returns Feishu 300313 for empty/new elements).
                self._sequence += 1
                status_line = f"\n\n_{tool_name} · {elapsed_str}_"
                try:
                    await self._replier.stream_append_text(
                        self._card_id, status_line, self._sequence
                    )
                except Exception as exc:
                    logger.debug(
                        "SessionWorker[%s]: stream_append_text (status) failed "
                        "(seq=%d): %s",
                        self._session.context_id,
                        self._sequence,
                        exc,
                    )
            return

        # ------------------------------------------------------------------
        # Fallback path — debounced full-card PATCH.
        # ------------------------------------------------------------------
        now = time.monotonic()
        elapsed = now - self._last_progress_update

        if elapsed < self._settings.progress_debounce_seconds and not tool_name:
            # Not enough time has passed and no tool-use event; skip update.
            return

        accumulated = "".join(self._progress_buffer)

        if not accumulated and not tool_name:
            return

        elapsed_s = int(now - self._task_start)
        elapsed_str = _format_elapsed(elapsed_s)
        if tool_name:
            status_text = f"工具调用: {tool_name} · {elapsed_str}"
        else:
            status_text = elapsed_str

        self._last_progress_update = now

        card = self._replier.build_progress_card(
            status=status_text,
            content=accumulated or "思考中...",
            title=f"思考中... {self._proj}",
        )

        if self._progress_message_id:
            try:
                await self._replier.update_card(self._progress_message_id, card)
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to update progress card",
                    self._session.context_id,
                )
        else:
            # Fallback: initial card send failed; send a new card keeping the
            # same reply mode (thread / quote / top-level) as the original task.
            chat_id = self._session.context_id.split(":")[0]
            try:
                if self._active_message_id:
                    self._progress_message_id = await self._replier.reply_card(
                        self._active_message_id, card, in_thread=self._active_in_thread
                    )
                else:
                    self._progress_message_id = await self._replier.send_card(
                        chat_id, card
                    )
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to send fallback progress card",
                    self._session.context_id,
                )

    # ------------------------------------------------------------------
    # Permission callback
    # ------------------------------------------------------------------

    async def _on_permission(self, req: PermissionRequest) -> PermissionChoice:
        """Block until the user replies to the permission request.

        Sends a permission card to the chat, then waits on the session's
        permission future (set when the user sends a numbered reply).

        Args:
            req: The permission request from ACPRuntime.

        Returns:
            The user's :class:`~nextme.protocol.types.PermissionChoice`.
        """
        chat_id = self._session.context_id.split(":")[0]
        logger.info(
            "SessionWorker[%s]: permission request id=%r description=%r",
            self._session.context_id,
            req.request_id,
            req.description,
        )

        # Build and send the permission card.
        # session_id = context_id so handle_card_action can look up the
        # UserContext in the registry; display_id = actual_id for the footer.
        card = self._replier.build_permission_card(
            description=req.description,
            options=req.options,
            session_id=self._session.context_id,
            project_name=self._session.project_name,
            executor=self._session.executor,
            display_id=self._session.actual_id or "",
        )
        try:
            await self._replier.send_card(chat_id, card)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send permission card",
                self._session.context_id,
            )

        # Register the pending future on the session.
        future = self._session.set_permission_pending(req.options)

        try:
            choice = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._settings.permission_timeout_seconds,
            )
            logger.info(
                "SessionWorker[%s]: permission resolved index=%d",
                self._session.context_id,
                choice.option_index,
            )
            return choice
        except asyncio.TimeoutError:
            logger.warning(
                "SessionWorker[%s]: permission timed out after %.0fs, defaulting to 1",
                self._session.context_id,
                self._settings.permission_timeout_seconds,
            )
            self._session.cancel_permission()
            return PermissionChoice(request_id=req.request_id, option_index=1)
        except asyncio.CancelledError:
            self._session.cancel_permission()
            raise

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    async def _update_or_reply(self, task: Task, card_json: str) -> None:
        """Update the existing progress card in-place, or send a new reply.

        For **regular** (non-streaming) progress cards — ``_card_id`` is ``None``:
        PATCH in place so the user sees one card transition from "思考中..." to the
        final state.  If PATCH fails for any reason, fall through and send a new
        reply so the result is never silently dropped.

        For **streaming** (cardkit) progress cards — ``_card_id`` is set:
        The IM message holds a ``{"card_id":"xxx"}`` reference.  Feishu rejects
        PATCH requests on such messages with error 230099.  Skip the PATCH and
        send a new reply directly (the streaming card remains as a progress log).
        """
        if self._progress_message_id and self._card_id is None:
            # Non-streaming: try PATCH in place.
            try:
                await self._replier.update_card(self._progress_message_id, card_json)
                return
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to update card in-place, "
                    "falling back to new reply",
                    self._session.context_id,
                )
                # Fall through to send a new reply.

        # Streaming mode or PATCH failed: send a new reply.
        try:
            if task.message_id:
                await self._replier.reply_card(
                    task.message_id, card_json, in_thread=self._active_in_thread
                )
            else:
                chat_id = self._session.context_id.split(":")[0]
                await self._replier.send_card(chat_id, card_json)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send fallback reply card",
                self._session.context_id,
            )

    async def _send_result(self, task: Task, content: str) -> None:
        """Update the progress card to show the final result."""
        elapsed_s = int(time.monotonic() - self._task_start)
        result_card = self._replier.build_result_card(
            content=content or "(无输出)",
            title=f"完成 {self._proj}",
            template="blue",
            session_id=self._session.actual_id or "",
            elapsed=_format_elapsed(elapsed_s),
            executor=self._session.executor,
        )
        await self._update_or_reply(task, result_card)

    async def _send_error(self, task: Task, error: str) -> None:
        """Update the progress card to show an error."""
        error_card = self._replier.build_error_card(error, title=f"出错了 {self._proj}")
        await self._update_or_reply(task, error_card)

    async def _send_cancelled(self, task: Task) -> None:
        """Update the progress card to show a cancellation notice."""
        cancel_card = self._replier.build_result_card(
            content="操作已取消",
            title=f"已取消 {self._proj}",
            template="grey",
        )
        try:
            await self._update_or_reply(task, cancel_card)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send cancel reply for task %s",
                self._session.context_id,
                task.id,
            )
