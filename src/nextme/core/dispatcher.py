"""TaskDispatcher — route incoming Tasks to the correct session worker.

The dispatcher is the main entry point called by
:class:`~nextme.feishu.handler.MessageHandler` for every incoming message.
It is responsible for:

* Handling meta-commands (``/new``, ``/stop``, ``/help``, ``/status``,
  ``/project``).
* Detecting and forwarding permission replies (``"1"``, ``"2"``, ``"3"`` …
  when a permission future is pending).
* Routing normal messages into the appropriate session's task queue.
* Ensuring a :class:`~nextme.core.worker.SessionWorker` asyncio Task is
  running for each active session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from ..acp.janitor import ACPRuntimeRegistry
from ..config.schema import AppConfig, Settings
from ..protocol.types import (
    PermissionChoice,
    Reply,
    ReplyType,
    Task,
    TaskStatus,
)
from .interfaces import IMAdapter, Replier
from .commands import (
    handle_help,
    handle_new,
    handle_project,
    handle_status,
    handle_stop,
)
from .path_lock import PathLockRegistry
from .session import Session, SessionRegistry, UserContext
from .worker import SessionWorker

logger = logging.getLogger(__name__)


class TaskDispatcher:
    """Route incoming :class:`~nextme.protocol.types.Task` objects to session workers.

    Args:
        config: Application configuration (projects list, credentials).
        settings: Behaviour settings (queue capacity, timeouts, …).
        session_registry: Global singleton mapping context IDs to sessions.
        acp_registry: Global registry of ACP subprocess runtimes.
        path_lock_registry: Global path-based write lock registry.
        feishu_client: The :class:`~nextme.feishu.client.FeishuClient` instance
            used to obtain per-chat :class:`~nextme.feishu.reply.FeishuReplier`
            objects.
    """

    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        session_registry: SessionRegistry,
        acp_registry: ACPRuntimeRegistry,
        path_lock_registry: PathLockRegistry,
        feishu_client: IMAdapter,
    ) -> None:
        self._config = config
        self._settings = settings
        self._session_registry = session_registry
        self._acp_registry = acp_registry
        self._path_lock_registry = path_lock_registry
        self._feishu_client = feishu_client

        # session_id -> asyncio.Task (running worker)
        self._worker_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def dispatch(self, task: Task) -> None:
        """Route *task* to the appropriate handler.

        Called by :class:`~nextme.feishu.handler.MessageHandler` for every
        incoming Feishu message.

        Processing order:
        1. Resolve (or create) the :class:`~nextme.core.session.UserContext`
           and the active :class:`~nextme.core.session.Session`.
        2. If the text matches a meta-command, handle it and return.
        3. If there is a pending permission future and the text is a valid
           digit reply, resolve the permission and return.
        4. Otherwise enqueue the task and ensure a worker is running.

        Args:
            task: The incoming task containing the user's message.
        """
        context_id = task.session_id
        chat_id = self._get_chat_id(context_id)
        replier = self._feishu_client.get_replier()

        # ------------------------------------------------------------------
        # Build a proper reply_fn that sends Feishu messages for this task.
        # - group chat  → thread reply  (reply_in_thread=True)
        # - p2p chat    → quote reply   (reply_in_thread=False)
        # - no message_id → top-level chat message (fallback)
        # ------------------------------------------------------------------
        in_thread = task.chat_type == "group"

        async def reply_fn(reply: Reply) -> None:
            if reply.type == ReplyType.CARD:
                if task.message_id:
                    await replier.reply_card(task.message_id, reply.content, in_thread=in_thread)
                else:
                    await replier.send_card(chat_id, reply.content)
            elif reply.type == ReplyType.MARKDOWN:
                if task.message_id:
                    await replier.reply_text(task.message_id, reply.content, in_thread=in_thread)
                else:
                    await replier.send_text(chat_id, reply.content)
            else:
                # REACTION and FILE are not yet implemented at this layer.
                logger.warning(
                    "TaskDispatcher: unhandled reply type %r for task %s",
                    reply.type,
                    task.id,
                )

        task.reply_fn = reply_fn

        # ------------------------------------------------------------------
        # Resolve user context and ensure an active session.
        # ------------------------------------------------------------------
        user_ctx = self._session_registry.get_or_create(context_id)

        # Bootstrap the default project if the user has no active session.
        if user_ctx.get_active_session() is None:
            default_project = self._config.default_project
            if default_project is None:
                logger.error(
                    "TaskDispatcher: no projects configured; cannot create session "
                    "for context_id=%r",
                    context_id,
                )
                try:
                    await replier.send_text(
                        chat_id,
                        "配置错误：未找到任何项目，请检查 nextme.json。",
                    )
                except Exception:
                    logger.exception(
                        "TaskDispatcher: failed to send config-error message to %r",
                        chat_id,
                    )
                return
            user_ctx.get_or_create_session(default_project, self._settings)

        session = user_ctx.get_active_session()
        assert session is not None  # guaranteed by the block above

        text = task.content.strip()

        # ------------------------------------------------------------------
        # 1. Meta-commands
        # ------------------------------------------------------------------
        if self._is_meta_command(text):
            await self._handle_meta_command(task, user_ctx)
            return

        # ------------------------------------------------------------------
        # 2. Permission reply
        # ------------------------------------------------------------------
        if self._is_permission_reply(session, text):
            self._apply_permission_reply(session, text, task)
            return

        # ------------------------------------------------------------------
        # 3. Normal task — enqueue and ensure worker is running.
        # ------------------------------------------------------------------
        try:
            session.task_queue.put_nowait(task)
            session.pending_tasks.append(task)
            task.was_queued = session.task_queue.qsize() > 1
            logger.info(
                "TaskDispatcher: enqueued task %s for session %r (queue depth=%d)",
                task.id,
                context_id,
                session.task_queue.qsize(),
            )
        except asyncio.QueueFull:
            logger.warning(
                "TaskDispatcher: task queue full for session %r; dropping task %s",
                context_id,
                task.id,
            )
            try:
                await replier.send_text(
                    chat_id,
                    "任务队列已满，请稍后再试。",
                )
            except Exception:
                logger.exception(
                    "TaskDispatcher: failed to send queue-full message to %r",
                    chat_id,
                )
            return

        # Immediately acknowledge receipt with an "OK" emoji reaction so the
        # user knows their message was received before the worker starts.
        if task.message_id:
            try:
                await replier.send_reaction(task.message_id, "OK")
            except Exception:
                logger.warning(
                    "TaskDispatcher: failed to send reaction for task %s",
                    task.id,
                    exc_info=True,
                )

        await self._ensure_worker(session, replier)

    # ------------------------------------------------------------------
    # Private: chat / permission helpers
    # ------------------------------------------------------------------

    def _get_chat_id(self, session_id: str) -> str:
        """Extract ``chat_id`` from ``"chatID:userID"``."""
        return session_id.split(":")[0]

    def _is_permission_reply(self, session: Session, text: str) -> bool:
        """Return ``True`` if *text* is a digit matching a pending permission option.

        Args:
            session: The session to check for a pending permission future.
            text: The stripped message text.

        Returns:
            ``True`` when *text* is a 1-based index into ``session.perm_options``
            and the session actually has a pending future.
        """
        if session.perm_future is None or session.perm_future.done():
            return False
        if not text.isdigit():
            return False
        index = int(text)
        return any(opt.index == index for opt in session.perm_options)

    def _is_meta_command(self, text: str) -> bool:
        """Return ``True`` if *text* starts with ``/``."""
        return text.startswith("/")

    # ------------------------------------------------------------------
    # Private: meta-command dispatch
    # ------------------------------------------------------------------

    async def _handle_meta_command(self, task: Task, user_ctx: UserContext) -> None:
        """Parse and dispatch a slash command.

        Supported commands:
        - ``/new``
        - ``/stop``
        - ``/help``
        - ``/status``
        - ``/project <name>``
        - ``/skill <trigger>``  (acknowledged; actual skill invocation TBD)

        Unrecognised commands result in a help card being shown.

        Args:
            task: The incoming task (used for context_id / reply_fn).
            user_ctx: The user's context.
        """
        context_id = task.session_id
        chat_id = self._get_chat_id(context_id)
        text = task.content.strip()
        replier = self._feishu_client.get_replier()

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        session = user_ctx.get_active_session()

        if command == "/help":
            await handle_help(replier, chat_id)

        elif command == "/new":
            if session is None:
                await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            runtime = self._acp_registry.get(context_id)
            await handle_new(session, runtime, replier, chat_id)

        elif command == "/stop":
            if session is None:
                await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            await handle_stop(session, replier, chat_id)

        elif command == "/status":
            if session is None:
                await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            await handle_status(session, replier, chat_id)

        elif command == "/project":
            if not arg:
                available = ", ".join(
                    f"`{p.name}`" for p in self._config.projects
                )
                await replier.send_text(
                    chat_id,
                    f"用法: `/project <name>`\n可用项目: {available or '(无)'}",
                )
                return
            await handle_project(
                user_ctx, arg, self._config, self._settings, replier, chat_id
            )

        elif command == "/skill":
            if not arg:
                await replier.send_text(chat_id, "用法: `/skill <trigger>`")
                return
            # Skill invocation is handled at a higher layer; acknowledge here.
            logger.info(
                "TaskDispatcher: /skill trigger=%r from context_id=%r", arg, context_id
            )
            await replier.send_text(chat_id, f"正在触发 Skill: `{arg}`…")

        else:
            logger.debug(
                "TaskDispatcher: unknown command %r from context_id=%r",
                command,
                context_id,
            )
            await handle_help(replier, chat_id)

    # ------------------------------------------------------------------
    # Private: permission reply handling
    # ------------------------------------------------------------------

    def _apply_permission_reply(
        self, session: Session, text: str, task: Task
    ) -> None:
        """Resolve the pending permission future with the user's choice.

        Args:
            session: The session that is waiting for permission.
            text: The user's digit reply (e.g. ``"1"``).
            task: The incoming task (used for logging).
        """
        index = int(text)
        matching_option = next(
            (opt for opt in session.perm_options if opt.index == index), None
        )
        label = matching_option.label if matching_option else ""

        choice = PermissionChoice(
            request_id="",          # request_id filled from perm_future context
            option_index=index,
            option_label=label,
        )
        logger.info(
            "TaskDispatcher: resolving permission index=%d label=%r for session %r",
            index,
            label,
            session.context_id,
        )
        session.resolve_permission(choice)

    # ------------------------------------------------------------------
    # Private: worker lifecycle
    # ------------------------------------------------------------------

    async def _ensure_worker(self, session: Session, replier: Replier) -> None:
        """Start a :class:`~nextme.core.worker.SessionWorker` if one is not running.

        If a previous worker task has completed (normally or due to an error),
        it is discarded and a fresh worker is created.

        Args:
            session: The session that needs a running worker.
            replier: The :class:`~nextme.feishu.reply.FeishuReplier` passed to
                the worker for sending replies.
        """
        context_id = session.context_id
        existing = self._worker_tasks.get(context_id)

        if existing is not None and not existing.done():
            # Worker is still running — nothing to do.
            return

        if existing is not None and existing.done():
            # Clean up the completed / failed task reference.
            exc = existing.exception() if not existing.cancelled() else None
            if exc is not None:
                logger.error(
                    "TaskDispatcher: worker for session %r exited with error: %s",
                    context_id,
                    exc,
                )
            else:
                logger.debug(
                    "TaskDispatcher: worker for session %r has finished; restarting",
                    context_id,
                )
            del self._worker_tasks[context_id]

        logger.info(
            "TaskDispatcher: starting SessionWorker for session %r", context_id
        )
        worker = SessionWorker(
            session=session,
            acp_registry=self._acp_registry,
            replier=replier,
            settings=self._settings,
            path_lock_registry=self._path_lock_registry,
        )
        worker_task = asyncio.create_task(
            worker.run(),
            name=f"worker-{context_id}",
        )
        self._worker_tasks[context_id] = worker_task

        # Attach a done-callback for post-mortem logging.
        def _on_worker_done(t: asyncio.Task) -> None:
            if t.cancelled():
                logger.info(
                    "TaskDispatcher: worker for session %r was cancelled", context_id
                )
            elif t.exception():
                logger.error(
                    "TaskDispatcher: worker for session %r raised: %s",
                    context_id,
                    t.exception(),
                )

        worker_task.add_done_callback(_on_worker_done)
