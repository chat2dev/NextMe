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
import logging
import time
from typing import Optional

from ..acp.janitor import ACPRuntimeRegistry
from ..config.schema import Settings
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
    ) -> None:
        self._session = session
        self._acp_registry = acp_registry
        self._replier = replier
        self._settings = settings
        self._path_lock_registry = path_lock_registry

        # State maintained across _on_progress calls for a single task.
        self._progress_message_id: Optional[str] = None
        self._progress_buffer: list[str] = []
        self._last_progress_update: float = 0.0
        self._task_start: float = 0.0
        self._active_message_id: str = ""  # message_id of the current task

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

        # Step 1 — Send initial progress card as a thread reply so it appears
        # inline with the user's original message.
        chat_id = self._session.context_id.split(":")[0]
        initial_card = self._replier.build_progress_card(
            status="",
            content="思考中...",
            title="思考中...",
        )
        try:
            if task.message_id:
                self._progress_message_id = await self._replier.reply_card(
                    task.message_id, initial_card
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
                session_id=self._session.context_id,
                cwd=str(self._session.project_path),
                settings=self._settings,
                executor=self._session.executor,
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
        """Receive a progress delta from ACPRuntime and debounce card updates.

        Accumulates content in an internal buffer and only updates the card if
        ``settings.progress_debounce_seconds`` have elapsed since the last update.

        Args:
            delta: Text delta emitted by the ACP subprocess.
            tool_name: Name of the tool currently being used (may be empty).
        """
        if delta:
            self._progress_buffer.append(delta)

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
            title="思考中...",
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
            # Fallback: send a new card if we somehow lack a message_id.
            chat_id = self._session.context_id.split(":")[0]
            try:
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
        card = self._replier.build_permission_card(
            description=req.description,
            options=req.options,
            session_id=self._session.context_id,
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

    async def _send_result(self, task: Task, content: str) -> None:
        """Send the final result card for *task*."""
        elapsed_s = int(time.monotonic() - self._task_start)
        result_card = self._replier.build_result_card(
            content=content or "(无输出)",
            title="完成",
            template="blue",
            session_id=self._session.context_id,
            elapsed=_format_elapsed(elapsed_s),
        )
        reply = Reply(
            type=ReplyType.CARD,
            content=result_card,
            title="完成",
            template="blue",
        )
        try:
            await task.reply_fn(reply)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send result reply for task %s",
                self._session.context_id,
                task.id,
            )

    async def _send_error(self, task: Task, error: str) -> None:
        """Send an error card for *task*."""
        error_card = self._replier.build_error_card(error)
        reply = Reply(
            type=ReplyType.CARD,
            content=error_card,
            title="出错了",
            template="red",
        )
        try:
            await task.reply_fn(reply)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send error reply for task %s",
                self._session.context_id,
                task.id,
            )

    async def _send_cancelled(self, task: Task) -> None:
        """Send a cancellation notification for *task*."""
        reply = Reply(
            type=ReplyType.MARKDOWN,
            content="已取消",
        )
        try:
            await task.reply_fn(reply)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send cancel reply for task %s",
                self._session.context_id,
                task.id,
            )
