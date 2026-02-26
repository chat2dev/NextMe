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
from ..config.state_store import StateStore
from ..memory.manager import MemoryManager
from ..skills.invoker import SkillInvoker
from ..skills.registry import SkillRegistry
from ..protocol.types import (
    PermissionChoice,
    Reply,
    ReplyType,
    Task,
    TaskStatus,
)
from .interfaces import IMAdapter, Replier
from .commands import (
    handle_bind,
    handle_unbind,
    handle_help,
    handle_new,
    handle_project,
    handle_remember,
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
        state_store: Optional persistent store used to save and load dynamic
            chat→project bindings (set via ``/project bind``).
    """

    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        session_registry: SessionRegistry,
        acp_registry: ACPRuntimeRegistry,
        path_lock_registry: PathLockRegistry,
        feishu_client: IMAdapter,
        state_store: Optional[StateStore] = None,
        skill_registry: Optional[SkillRegistry] = None,
        memory_manager: Optional[MemoryManager] = None,
    ) -> None:
        self._config = config
        self._settings = settings
        self._session_registry = session_registry
        self._acp_registry = acp_registry
        self._path_lock_registry = path_lock_registry
        self._feishu_client = feishu_client
        self._state_store = state_store
        self._skill_registry: SkillRegistry = skill_registry or SkillRegistry()
        self._memory_manager = memory_manager

        # worker_key (context_id:project_name) -> asyncio.Task (running worker)
        self._worker_tasks: dict[str, asyncio.Task] = {}
        # Dynamic chat→project bindings set via /project bind (chat_id → project_name).
        # Populated at startup from state.json and updated by handle_bind.
        self._dynamic_bindings: dict[str, str] = (
            state_store.get_all_bindings() if state_store is not None else {}
        )

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
        #
        # Priority:
        #   1. Static binding in nextme.json  (config.bindings[chat_id])
        #   2. Dynamic binding set via /project bind  (stored in state.json)
        #   3. UserContext.active_project  (set by /project <name>)
        #   4. First configured project  (bootstrap default)
        # ------------------------------------------------------------------
        user_ctx = self._session_registry.get_or_create(context_id)

        bound_project = self._resolve_bound_project(chat_id)
        if bound_project is not None:
            # Chat is permanently bound to a project — always route there.
            session = user_ctx.get_or_create_session(bound_project, self._settings)
            logger.debug(
                "TaskDispatcher: routing context_id=%r to bound project %r",
                context_id,
                bound_project.name,
            )
        else:
            # No binding — use active project or bootstrap with the default.
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

        assert session is not None  # guaranteed by the blocks above

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

    def _get_user_id(self, session_id: str) -> str:
        """Extract ``user_id`` from ``"chatID:userID"``."""
        return session_id.rsplit(":", 1)[-1]

    def _resolve_bound_project(self, chat_id: str):
        """Return the :class:`~nextme.config.schema.Project` bound to *chat_id*, or ``None``.

        Checks static config bindings (``nextme.json``) and dynamic bindings
        stored in :attr:`_dynamic_bindings` (set via ``/project bind``).
        Static config takes precedence.
        """
        project_name = self._config.get_binding(chat_id) or self._dynamic_bindings.get(chat_id)
        if not project_name:
            return None
        project = self._config.get_project(project_name)
        if project is None:
            logger.warning(
                "TaskDispatcher: binding for chat %r points to unknown project %r; ignoring",
                chat_id,
                project_name,
            )
        return project

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
            runtime = self._acp_registry.get(f"{context_id}:{session.project_name}")
            await handle_new(session, runtime, replier, chat_id)
            # Clear persisted session id so the next task starts a truly fresh session.
            if self._state_store is not None:
                self._state_store.save_project_actual_id(
                    context_id, session.project_name, ""
                )

        elif command == "/stop":
            if session is None:
                await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            await handle_stop(session, replier, chat_id)

        elif command == "/status":
            await handle_status(user_ctx, replier, chat_id)

        elif command == "/project":
            if not arg:
                active = user_ctx.active_project
                bound = self._resolve_bound_project(chat_id)
                lines = ["**项目列表:**\n"]
                for p in self._config.projects:
                    markers = []
                    if p.name == active:
                        markers.append("★ 活跃")
                    if bound and p.name == bound:
                        markers.append("⚓ 绑定")
                    marker_str = f"  `{'  '.join(markers)}`" if markers else ""
                    lines.append(f"• **{p.name}**{marker_str}  `{p.path}`")
                lines.append("\n用法: `/project <name>` 切换 | `/project bind <name>` 绑定 | `/project unbind` 解绑")
                await replier.send_text(chat_id, "\n".join(lines))
                return

            sub_parts = arg.split(maxsplit=1)
            sub_cmd = sub_parts[0].lower()

            if sub_cmd == "bind":
                bind_name = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if not bind_name:
                    await replier.send_text(chat_id, "用法: `/project bind <name>`")
                    return
                bound = await handle_bind(chat_id, bind_name, self._config, replier)
                if bound:
                    self._dynamic_bindings[chat_id] = bound
                    if self._state_store is not None:
                        self._state_store.set_binding(chat_id, bound)

            elif sub_cmd == "unbind":
                await handle_unbind(chat_id, replier)
                self._dynamic_bindings.pop(chat_id, None)
                if self._state_store is not None:
                    self._state_store.remove_binding(chat_id)

            else:
                await handle_project(
                    user_ctx, arg, self._config, self._settings, replier, chat_id
                )

        elif command == "/skill":
            if not arg:
                skills = self._skill_registry.list_all()
                if not skills:
                    await replier.send_text(chat_id, "当前没有已注册的 Skill。")
                else:
                    source_order = ["project", "nextme", "claude", "builtin"]
                    source_labels = {
                        "project": "项目级",
                        "nextme": "NextMe 全局",
                        "claude": "Claude 全局",
                        "builtin": "内置",
                    }
                    by_source: dict[str, list] = {}
                    for s in sorted(skills, key=lambda x: x.meta.trigger):
                        by_source.setdefault(s.source or "builtin", []).append(s)
                    lines = ["**已注册 Skills:**"]
                    for src in source_order:
                        if src not in by_source:
                            continue
                        lines.append(f"\n**{source_labels[src]}**")
                        for s in by_source[src]:
                            lines.append(f"  • `/skill {s.meta.trigger}` — {s.meta.description}")
                    await replier.send_text(chat_id, "\n".join(lines))
                return
            # Look up the skill and enqueue a rendered prompt as a normal task.
            trigger, _, user_input = arg.partition(" ")
            skill = self._skill_registry.get(trigger.strip())
            if skill is None:
                available = ", ".join(
                    f"`{s.meta.trigger}`"
                    for s in sorted(self._skill_registry.list_all(), key=lambda x: x.meta.trigger)
                )
                await replier.send_text(
                    chat_id,
                    f"未找到 Skill `{trigger}`。\n可用: {available or '(无)'}",
                )
                return
            logger.info(
                "TaskDispatcher: invoking skill %r for context_id=%r", trigger, context_id
            )
            prompt = SkillInvoker().build_prompt(skill, user_input=user_input.strip())
            skill_task = Task(
                id=str(uuid.uuid4()),
                content=prompt,
                session_id=task.session_id,
                reply_fn=task.reply_fn,
                timeout=task.timeout,
            )
            try:
                session.task_queue.put_nowait(skill_task)
                session.pending_tasks.append(skill_task)
                skill_task.was_queued = session.task_queue.qsize() > 1
            except asyncio.QueueFull:
                await replier.send_text(chat_id, "任务队列已满，请稍后再试。")
                return
            await self._ensure_worker(session, replier)

        elif command == "/task":
            lines = ["**当前任务队列:**\n"]
            has_any = False
            for project_name, sess in user_ctx.sessions.items():
                active_marker = "★ " if project_name == user_ctx.active_project else ""
                active_task = sess.active_task
                queue_size = sess.task_queue.qsize()
                if active_task or queue_size > 0:
                    has_any = True
                    lines.append(f"**{active_marker}{project_name}**")
                    if active_task:
                        lines.append(f"  执行中: `{active_task.id[:8]}…` {active_task.content[:40]}")
                    if queue_size > 0:
                        lines.append(f"  队列等待: {queue_size} 个任务")
            if not has_any:
                await replier.send_text(chat_id, "当前没有进行中的任务。")
            else:
                await replier.send_text(chat_id, "\n".join(lines))

        elif command == "/remember":
            if not arg:
                await replier.send_text(chat_id, "用法: `/remember <text>`")
                return
            if self._memory_manager is None:
                await replier.send_text(chat_id, "记忆功能未启用。")
                return
            user_id = self._get_user_id(context_id)
            await handle_remember(user_id, arg, self._memory_manager, replier, chat_id)

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
        # Key is scoped to project so each project gets an independent worker.
        worker_key = f"{context_id}:{session.project_name}"
        existing = self._worker_tasks.get(worker_key)

        if existing is not None and not existing.done():
            # Worker is still running — nothing to do.
            return

        if existing is not None and existing.done():
            # Clean up the completed / failed task reference.
            exc = existing.exception() if not existing.cancelled() else None
            if exc is not None:
                logger.error(
                    "TaskDispatcher: worker for %r exited with error: %s",
                    worker_key,
                    exc,
                )
            else:
                logger.debug(
                    "TaskDispatcher: worker for %r has finished; restarting",
                    worker_key,
                )
            del self._worker_tasks[worker_key]

        logger.info(
            "TaskDispatcher: starting SessionWorker for %r", worker_key
        )
        worker = SessionWorker(
            session=session,
            acp_registry=self._acp_registry,
            replier=replier,
            settings=self._settings,
            path_lock_registry=self._path_lock_registry,
            state_store=self._state_store,
            memory_manager=self._memory_manager,
        )
        worker_task = asyncio.create_task(
            worker.run(),
            name=f"worker-{worker_key}",
        )
        self._worker_tasks[worker_key] = worker_task

        # Attach a done-callback for post-mortem logging.
        def _on_worker_done(t: asyncio.Task) -> None:
            if t.cancelled():
                logger.info(
                    "TaskDispatcher: worker for %r was cancelled", worker_key
                )
            elif t.exception():
                logger.error(
                    "TaskDispatcher: worker for %r raised: %s",
                    worker_key,
                    t.exception(),
                )

        worker_task.add_done_callback(_on_worker_done)
