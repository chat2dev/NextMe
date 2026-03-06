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
import collections
import json
import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from ..acl.manager import AclManager
    from ..acl.schema import Role

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
    handle_done,
    handle_unbind,
    handle_help,
    handle_new,
    handle_project,
    handle_remember,
    handle_status,
    handle_stop,
    handle_threads_list,
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
        acl_manager: Optional["AclManager"] = None,
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
        self._acl_manager = acl_manager

        # worker_key (context_id:project_name) -> asyncio.Task (running worker)
        self._worker_tasks: dict[str, asyncio.Task] = {}
        # app_id -> [message_id, ...] for ACL review notification cards sent to reviewers.
        # Used to update (disable) those cards once the application is decided.
        self._review_notification_msgs: dict[int, list[str]] = {}
        # Dynamic chat→project bindings set via /project bind (chat_id → project_name).
        # Populated at startup from state.json and updated by handle_bind.
        self._dynamic_bindings: dict[str, str] = (
            state_store.get_all_bindings() if state_store is not None else {}
        )
        # Pending thread queue: chat_id → deque of Tasks waiting for a free slot.
        self._pending_thread_queue: dict[str, collections.deque] = {}
        # Optional callback invoked when a thread is closed, so the handler can
        # remove it from _active_threads.  Registered via register_thread_closed_callback().
        self._thread_closed_callback: Callable[[str, str], None] | None = None
        # Optional callback invoked when a thread is accepted (after limit check passes),
        # so the handler can add it to _active_threads.  Registered via register_thread_accept_callback().
        self._thread_accept_callback: Callable[[str, str], None] | None = None

    # ------------------------------------------------------------------
    # Public: callback registration
    # ------------------------------------------------------------------

    def register_thread_closed_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a thread is closed (chat_id, thread_root_id)."""
        self._thread_closed_callback = callback

    def register_thread_accept_callback(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a thread is accepted (chat_id, thread_root_id)."""
        self._thread_accept_callback = callback

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
        4. For group root messages: enforce the active-thread limit.  If the
           limit is reached, park the task in :attr:`_pending_thread_queue`
           and return after sending a queuing notice.
        5. Otherwise enqueue the task and ensure a worker is running.

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

        text = task.content.strip()

        # ------------------------------------------------------------------
        # ACL gate: /whoami and /help are always allowed without authorization.
        # For open commands we skip ACL check entirely and go straight to the
        # meta-command handler (which does not require a session).
        # For all other content: deny if user is not authorized.
        # ------------------------------------------------------------------
        if self._acl_manager is not None:
            _open_cmds = ("/whoami", "/help")
            is_open_cmd = any(text.lower().startswith(c) for c in _open_cmds)
            if is_open_cmd:
                # Bypass ACL — route directly to meta-command handler.
                user_ctx = self._session_registry.get_or_create(context_id)
                await self._handle_meta_command(task, user_ctx)
                return
            # Not an open command — check authorization.
            user_id = task.user_id or self._get_user_id(context_id)
            role = await self._acl_manager.get_role(user_id)
            if role is None:
                logger.info(
                    "TaskDispatcher: unauthorized user %r denied (task %s)",
                    user_id,
                    task.id,
                )
                try:
                    denied_card = replier.build_access_denied_card(user_id)
                    if in_thread:
                        # Group chat: post a plain prompt in the thread so other
                        # members are not presented with clickable apply buttons;
                        # send the actual apply card as a DM to the requester only.
                        await replier.reply_text(
                            task.message_id,
                            "🔒 你没有权限使用此机器人，已向你私信发送申请入口。",
                            in_thread=True,
                        )
                        await replier.send_to_user(user_id, denied_card, "interactive")
                    elif task.message_id:
                        await replier.reply_card(
                            task.message_id, denied_card, in_thread=False
                        )
                    else:
                        await replier.send_card(chat_id, denied_card)
                except Exception:
                    logger.exception(
                        "TaskDispatcher: failed to send denied card to %r", chat_id
                    )
                return

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

        # ------------------------------------------------------------------
        # 1. Meta-commands
        # ------------------------------------------------------------------
        if self._is_meta_command(text):
            # Acknowledge receipt before handling so the user knows the
            # command was seen even if the handler takes a moment.
            if task.message_id:
                try:
                    await replier.send_reaction(task.message_id, "OK")
                except Exception:
                    logger.warning(
                        "TaskDispatcher: failed to send reaction for meta-command task %s",
                        task.id,
                        exc_info=True,
                    )
            await self._handle_meta_command(task, user_ctx)
            return

        # ------------------------------------------------------------------
        # 2. Permission reply
        # ------------------------------------------------------------------
        if self._is_permission_reply(session, text):
            self._apply_permission_reply(session, text)
            return

        # ------------------------------------------------------------------
        # 3. Thread limit check (group root messages only).
        #
        # A "new thread" is identified by: chat_type == "group" AND
        # thread_root_id == message_id (the root message creates the thread).
        # When the active thread count for the chat has reached the configured
        # limit we park the task in a per-chat deque and send a queuing notice
        # to the user.  _on_thread_closed() drains the deque when a slot opens.
        # ------------------------------------------------------------------
        if (
            task.chat_type == "group"
            and task.thread_root_id
            and task.thread_root_id == task.message_id
            and self._state_store is not None
        ):
            active_count = self._state_store.get_active_thread_count(chat_id)
            limit = self._settings.max_active_threads_per_chat
            if active_count >= limit:
                queue = self._pending_thread_queue.setdefault(chat_id, collections.deque())
                queue_pos = len(queue) + 1
                # Revert handler's optimistic _active_threads entry — the thread is only queued, not active.
                if self._thread_closed_callback is not None:
                    try:
                        self._thread_closed_callback(chat_id, task.thread_root_id)
                    except Exception:
                        logger.exception(
                            "TaskDispatcher: failed to revert _active_threads for queued thread"
                        )
                queue.append(task)
                logger.info(
                    "TaskDispatcher: thread limit reached for chat %r "
                    "(active=%d limit=%d), queued task %s at position %d",
                    chat_id,
                    active_count,
                    limit,
                    task.id,
                    queue_pos,
                )
                try:
                    await replier.reply_text(
                        task.message_id,
                        f"⏳ 当前活跃话题已达上限（{limit} 个），"
                        f"你的请求排在第 {queue_pos} 位，将在有话题关闭后自动处理。",
                    )
                except Exception:
                    logger.exception(
                        "TaskDispatcher: failed to send queue-full message for task %s",
                        task.id,
                    )
                return
            # Within limit: register this new thread as active.
            self._state_store.register_thread(
                chat_id, task.thread_root_id, session.project_name
            )

        # ------------------------------------------------------------------
        # 4. Normal task — enqueue and ensure worker is running.
        # ------------------------------------------------------------------
        # Override task timeout from the resolved project config.
        project = self._config.get_project(session.project_name)
        if project is not None and project.task_timeout_seconds > 0:
            task.timeout = timedelta(seconds=project.task_timeout_seconds)

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

    def _on_thread_closed(self, chat_id: str, thread_root_id: str) -> None:
        """Release a thread slot and dispatch the next pending thread task if any.

        Called by the worker (or ``/done`` command) when a group thread is
        considered finished.  It:

        1. Unregisters the thread from :attr:`_state_store` so the slot is freed.
        2. Pops the oldest pending task for the chat from
           :attr:`_pending_thread_queue` and schedules a new ``dispatch`` call
           via :func:`asyncio.create_task`.

        Args:
            chat_id: The Feishu group chat ID (e.g. ``"oc_xxx"``).
            thread_root_id: The root message ID of the thread being closed.
        """
        if self._state_store is not None:
            self._state_store.unregister_thread(chat_id, thread_root_id)

        # Notify handler to remove from _active_threads so future messages in
        # this thread are ignored after /done.
        if self._thread_closed_callback is not None:
            try:
                self._thread_closed_callback(chat_id, thread_root_id)
            except Exception:
                logger.exception("_on_thread_closed: error in thread_closed_callback")

        queue = self._pending_thread_queue.get(chat_id)
        if queue:
            next_task = queue.popleft()
            logger.info(
                "TaskDispatcher: dequeuing pending thread task %s for chat %r",
                next_task.id,
                chat_id,
            )
            # Re-register in handler._active_threads so follow-up messages in this thread
            # are routed correctly once it becomes active.
            if self._thread_accept_callback is not None:
                try:
                    self._thread_accept_callback(chat_id, next_task.thread_root_id)
                except Exception:
                    logger.exception("_on_thread_closed: failed to re-register thread in handler")
            asyncio.create_task(self.dispatch(next_task))

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

        # For group thread sessions, replies go in-thread rather than to chat root.
        in_thread = task.chat_type == "group" and bool(task.thread_root_id)
        reply_msg_id = task.message_id if in_thread else ""

        # Resolve caller role for permission-gated commands.
        from ..acl.schema import Role as _Role
        caller_role: Optional[_Role] = None
        if self._acl_manager is not None:
            caller_role = await self._acl_manager.get_role(
                task.user_id or self._get_user_id(context_id)
            )

        if command == "/help":
            await handle_help(replier, chat_id, reply_msg_id=reply_msg_id)

        elif command == "/new":
            if session is None:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "当前没有活跃 Session。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            runtime = self._acp_registry.get(f"{context_id}:{session.project_name}")
            await handle_new(session, runtime, replier, chat_id, reply_msg_id=reply_msg_id)
            # Clear persisted session id so the next task starts a truly fresh session.
            if self._state_store is not None:
                self._state_store.save_project_actual_id(
                    context_id, session.project_name, ""
                )

        elif command == "/stop":
            if session is None:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "当前没有活跃 Session。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            runtime = self._acp_registry.get(f"{context_id}:{session.project_name}")
            await handle_stop(session, replier, chat_id, runtime=runtime, reply_msg_id=reply_msg_id)

        elif command == "/done":
            if task.chat_type != "group" or not task.thread_root_id:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "/done 仅在群聊话题内有效。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "/done 仅在群聊话题内有效。")
                return
            if session is None:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "当前没有活跃 Session。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "当前没有活跃 Session。")
                return
            runtime = self._acp_registry.get(f"{context_id}:{session.project_name}")
            chat_id_str = self._get_chat_id(context_id)

            def _on_closed() -> None:
                self._on_thread_closed(chat_id_str, task.thread_root_id)

            await handle_done(
                session=session,
                runtime=runtime,
                replier=replier,
                chat_id=chat_id_str,
                root_message_id=task.thread_root_id,
                acp_registry=self._acp_registry,
                on_thread_closed=_on_closed,
            )

        elif command == "/status":
            await handle_status(user_ctx, replier, chat_id, reply_msg_id=reply_msg_id)

        elif command == "/project":
            if not arg:
                active = user_ctx.active_project
                bound = self._resolve_bound_project(chat_id)
                proj_lines: list[str] = []
                for p in self._config.projects:
                    markers = []
                    if p.name == active:
                        markers.append("★ 活跃")
                    if bound and p.name == bound:
                        markers.append("⚓ 绑定")
                    marker_str = f"  `{'  '.join(markers)}`" if markers else ""
                    proj_lines.append(f"• **{p.name}**{marker_str}  `{p.path}`")
                proj_lines.append("\n用法: `/project <name>` 切换 | `/project bind <name>` 绑定 | `/project unbind` 解绑")
                proj_card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": "项目列表"},
                        "template": "blue",
                    },
                    "body": {"elements": [{"tag": "markdown", "content": "\n".join(proj_lines)}]},
                }
                proj_card_json = json.dumps(proj_card, ensure_ascii=False)
                if reply_msg_id:
                    await replier.reply_card(reply_msg_id, proj_card_json, in_thread=True)
                else:
                    await replier.send_card(chat_id, proj_card_json)
                return

            sub_parts = arg.split(maxsplit=1)
            sub_cmd = sub_parts[0].lower()

            if sub_cmd == "bind":
                bind_name = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if not bind_name:
                    if reply_msg_id:
                        await replier.reply_text(reply_msg_id, "用法: `/project bind <name>`", in_thread=True)
                    else:
                        await replier.send_text(chat_id, "用法: `/project bind <name>`")
                    return
                bound = await handle_bind(chat_id, bind_name, self._config, replier, reply_msg_id=reply_msg_id)
                if bound:
                    self._dynamic_bindings[chat_id] = bound
                    if self._state_store is not None:
                        self._state_store.set_binding(chat_id, bound)

            elif sub_cmd == "unbind":
                await handle_unbind(chat_id, replier, reply_msg_id=reply_msg_id)
                self._dynamic_bindings.pop(chat_id, None)
                if self._state_store is not None:
                    self._state_store.remove_binding(chat_id)

            else:
                await handle_project(
                    user_ctx, arg, self._config, self._settings, replier, chat_id,
                    reply_msg_id=reply_msg_id,
                )

        elif command == "/skill":
            if not arg:
                skills = self._skill_registry.list_all()
                if not skills:
                    skill_content = "当前没有已注册的 Skill。"
                else:
                    source_order = ["global", "nextme", "project"]
                    source_labels = {
                        "global": "Claude 全局",
                        "nextme": "NextMe 内置",
                        "project": "项目",
                    }
                    by_source: dict[str, list] = {}
                    for s in sorted(skills, key=lambda x: x.meta.trigger):
                        by_source.setdefault(s.source or "builtin", []).append(s)
                    skill_lines: list[str] = []
                    for src in source_order:
                        if src not in by_source:
                            continue
                        skill_lines.append(f"**{source_labels[src]}**")
                        for s in by_source[src]:
                            skill_lines.append(f"  • `/skill {s.meta.trigger}` — {s.meta.description}")
                    skill_content = "\n".join(skill_lines)
                skill_card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": "Skills"},
                        "template": "blue" if skills else "grey",
                    },
                    "body": {"elements": [{"tag": "markdown", "content": skill_content}]},
                }
                skill_card_json = json.dumps(skill_card, ensure_ascii=False)
                if reply_msg_id:
                    await replier.reply_card(reply_msg_id, skill_card_json, in_thread=True)
                else:
                    await replier.send_card(chat_id, skill_card_json)
                return
            # Look up the skill and enqueue a rendered prompt as a normal task.
            trigger, _, user_input = arg.partition(" ")
            skill = self._skill_registry.get(trigger.strip())
            if skill is None:
                available = ", ".join(
                    f"`{s.meta.trigger}`"
                    for s in sorted(self._skill_registry.list_all(), key=lambda x: x.meta.trigger)
                )
                if reply_msg_id:
                    await replier.reply_text(
                        reply_msg_id,
                        f"未找到 Skill `{trigger}`。\n可用: {available or '(无)'}",
                        in_thread=True,
                    )
                else:
                    await replier.send_text(
                        chat_id,
                        f"未找到 Skill `{trigger}`。\n可用: {available or '(无)'}",
                    )
                return
            logger.info(
                "TaskDispatcher: invoking skill %r for context_id=%r", trigger, context_id
            )
            enriched_input = user_input.strip()
            requester_open_id = task.user_id or (self._get_user_id(task.session_id) if task.session_id else "")
            # Build unified attendee block: @mentions + requester (deduped)
            seen_ids: set[str] = set()
            attendee_lines: list[str] = []
            for m in task.mentions:
                oid = m.get("open_id", "")
                if oid and oid not in seen_ids:
                    seen_ids.add(oid)
                    attendee_lines.append(f"- {m.get('name', '')} (open_id: {oid})")
            if requester_open_id and requester_open_id not in seen_ids:
                attendee_lines.append(f"- [预定人] (open_id: {requester_open_id})")
            if attendee_lines:
                enriched_input += "\n\n参与人(@mentions):\n" + "\n".join(attendee_lines)
            prompt = SkillInvoker().build_prompt(skill, user_input=enriched_input)
            skill_task = Task(
                id=str(uuid.uuid4()),
                content=prompt,
                session_id=task.session_id,
                reply_fn=task.reply_fn,
                message_id=task.message_id,
                chat_type=task.chat_type,
                timeout=task.timeout,
            )
            try:
                session.task_queue.put_nowait(skill_task)
                session.pending_tasks.append(skill_task)
                skill_task.was_queued = session.task_queue.qsize() > 1
            except asyncio.QueueFull:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "任务队列已满，请稍后再试。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "任务队列已满，请稍后再试。")
                return
            await self._ensure_worker(session, replier)

        elif command == "/task":
            lines: list[str] = []
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
            content = "\n".join(lines) if has_any else "当前没有进行中的任务。"
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "任务队列"},
                    "template": "blue" if has_any else "grey",
                },
                "body": {"elements": [{"tag": "markdown", "content": content}]},
            }
            task_card_json = json.dumps(card, ensure_ascii=False)
            if reply_msg_id:
                await replier.reply_card(reply_msg_id, task_card_json, in_thread=True)
            else:
                await replier.send_card(chat_id, task_card_json)

        elif command == "/remember":
            if not arg:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "用法: `/remember <text>`", in_thread=True)
                else:
                    await replier.send_text(chat_id, "用法: `/remember <text>`")
                return
            if self._memory_manager is None:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "记忆功能未启用。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "记忆功能未启用。")
                return
            user_id = task.user_id or self._get_user_id(context_id)
            await handle_remember(user_id, arg, self._memory_manager, replier, chat_id, reply_msg_id=reply_msg_id)

        elif command == "/whoami":
            if self._acl_manager is not None:
                from .commands import handle_whoami
                await handle_whoami(
                    task.user_id or self._get_user_id(context_id),
                    self._acl_manager,
                    replier,
                    chat_id,
                )
            else:
                uid = task.user_id or self._get_user_id(context_id)
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, f"open_id: `{uid}`\n角色: (未启用 ACL)", in_thread=True)
                else:
                    await replier.send_text(chat_id, f"open_id: `{uid}`\n角色: (未启用 ACL)")

        elif command == "/thread":
            if task.chat_type != "group":
                msg = "/thread 仅在群聊中有效。"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return
            if self._state_store is None:
                await replier.send_text(chat_id, "状态存储未启用，无法查看话题列表。")
                return

            if not arg:
                # List active threads for this chat.
                threads = self._state_store.get_threads_for_chat(chat_id)
                await handle_threads_list(chat_id, threads, replier, reply_msg_id=reply_msg_id)
                return

            sub_parts = arg.split(maxsplit=1)
            sub_cmd = sub_parts[0].lower()

            if sub_cmd != "close":
                await replier.send_text(
                    chat_id, "未知子命令。用法: `/thread` 或 `/thread close <short_id>`"
                )
                return

            short_id = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            if not short_id:
                msg = "用法: `/thread close <short_id>`"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return

            # Permission check: only Owner/Admin can force-close threads.
            if caller_role not in (_Role.ADMIN, _Role.OWNER):
                msg = "权限不足：仅 Owner/Admin 可强制关闭话题。"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return

            # Find the thread by short_id prefix.
            threads = self._state_store.get_threads_for_chat(chat_id)
            matches = [t for t in threads if t.thread_root_id.startswith(short_id)]
            if not matches:
                msg = f"未找到话题 `{short_id}`，请用 `/thread` 查看当前列表。"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return
            if len(matches) > 1:
                msg = f"短 ID `{short_id}` 匹配多个话题，请提供更多字符。"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return

            target_thread = matches[0]
            target_session_id = f"{chat_id}:{target_thread.thread_root_id}"
            target_user_ctx = self._session_registry.get_or_create(target_session_id)
            target_session = target_user_ctx.get_active_session()

            if target_session is None:
                # No in-memory session, just unregister the thread record.
                self._on_thread_closed(chat_id, target_thread.thread_root_id)
                msg = f"✅ 话题 `{short_id}…` 已关闭（无活跃 Session）。"
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, msg, in_thread=True)
                else:
                    await replier.send_text(chat_id, msg)
                return

            target_runtime = self._acp_registry.get(
                f"{target_session_id}:{target_session.project_name}"
            )

            def _on_target_closed() -> None:
                self._on_thread_closed(chat_id, target_thread.thread_root_id)

            await handle_done(
                session=target_session,
                runtime=target_runtime,
                replier=replier,
                chat_id=chat_id,
                root_message_id=target_thread.thread_root_id,
                acp_registry=self._acp_registry,
                on_thread_closed=_on_target_closed,
            )

        elif command == "/acl":
            if self._acl_manager is None:
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "ACL 功能未启用。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "ACL 功能未启用。")
            elif caller_role not in (_Role.ADMIN, _Role.OWNER, _Role.COLLABORATOR):
                if reply_msg_id:
                    await replier.reply_text(reply_msg_id, "权限不足。", in_thread=True)
                else:
                    await replier.send_text(chat_id, "权限不足。")
            else:
                await self._handle_acl_command(
                    arg, caller_role, task.user_id or self._get_user_id(context_id), replier, chat_id
                )

        else:
            logger.debug(
                "TaskDispatcher: unknown command %r from context_id=%r",
                command,
                context_id,
            )
            await handle_help(replier, chat_id, reply_msg_id=reply_msg_id)

    async def _handle_acl_command(
        self,
        arg: str,
        caller_role: "Role",
        caller_id: str,
        replier: Replier,
        chat_id: str,
    ) -> None:
        """Dispatch /acl sub-commands."""
        from .commands import (
            handle_acl_add,
            handle_acl_approve,
            handle_acl_list,
            handle_acl_pending,
            handle_acl_reject,
            handle_acl_remove,
        )
        from ..acl.schema import Role as _Role

        parts = arg.split(maxsplit=2) if arg else []
        sub = parts[0].lower() if parts else ""

        if not sub or sub == "list":
            await handle_acl_list(self._acl_manager, replier, chat_id)

        elif sub == "add":
            if len(parts) < 2:
                await replier.send_text(
                    chat_id, "用法: `/acl add <open_id> [owner|collaborator]`"
                )
                return
            target_id = parts[1]
            target_role_str = parts[2] if len(parts) > 2 else "collaborator"
            await handle_acl_add(
                actor_id=caller_id,
                actor_role=caller_role,
                target_id=target_id,
                target_role_str=target_role_str,
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub == "remove":
            if len(parts) < 2:
                await replier.send_text(chat_id, "用法: `/acl remove <open_id>`")
                return
            await handle_acl_remove(
                actor_id=caller_id,
                actor_role=caller_role,
                target_id=parts[1],
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub == "pending":
            if caller_role not in (_Role.ADMIN, _Role.OWNER):
                await replier.send_text(chat_id, "权限不足：需要 Owner 或 Admin 权限。")
                return
            await handle_acl_pending(
                viewer_role=caller_role,
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub in ("approve", "reject"):
            if caller_role not in (_Role.ADMIN, _Role.OWNER):
                await replier.send_text(chat_id, "权限不足：需要 Owner 或 Admin 权限。")
                return
            if len(parts) < 2:
                await replier.send_text(
                    chat_id, f"用法: `/acl {sub} <申请ID>`"
                )
                return
            try:
                app_id = int(parts[1])
            except ValueError:
                await replier.send_text(chat_id, "申请ID 必须是数字。")
                return
            if sub == "approve":
                await handle_acl_approve(
                    app_id=app_id,
                    reviewer_id=caller_id,
                    reviewer_role=caller_role,
                    acl_manager=self._acl_manager,
                    replier=replier,
                    chat_id=chat_id,
                )
            else:
                await handle_acl_reject(
                    app_id=app_id,
                    reviewer_id=caller_id,
                    reviewer_role=caller_role,
                    acl_manager=self._acl_manager,
                    replier=replier,
                    chat_id=chat_id,
                )
        else:
            await replier.send_text(
                chat_id,
                "未知子命令。可用: `list` `add` `remove` `pending` `approve` `reject`",
            )

    # ------------------------------------------------------------------
    # Public: ACL card action handling
    # ------------------------------------------------------------------

    async def handle_acl_card_action(self, action_data: dict) -> None:
        """Dispatch an ACL-related card button action.

        Called from the Feishu card action handler when action is
        ``acl_apply`` or ``acl_review``.

        Args:
            action_data: Parsed action value dict from the card event,
                with an ``operator_id`` key injected by the handler.
        """
        if self._acl_manager is None:
            logger.warning("handle_acl_card_action: no ACL manager configured")
            return

        action = action_data.get("action")
        replier = self._feishu_client.get_replier()

        if action == "acl_apply":
            await self._handle_acl_apply_action(action_data, replier)
        elif action == "acl_review":
            await self._handle_acl_review_action(action_data, replier)
        else:
            logger.warning("handle_acl_card_action: unknown action %r", action)

    async def _handle_acl_apply_action(self, data: dict, replier: Replier) -> None:
        """Process an acl_apply card button click."""
        from ..acl.schema import Role as _Role

        open_id: str = data.get("open_id", "")
        operator_id: str = data.get("operator_id", "")
        role_str: str = data.get("role", "collaborator")

        if not open_id:
            logger.warning("_handle_acl_apply_action: missing open_id")
            return

        # Reject clicks made by someone other than the card's intended recipient.
        if operator_id and operator_id != open_id:
            logger.warning(
                "_handle_acl_apply_action: operator %r tried to apply on behalf of %r — ignored",
                operator_id,
                open_id,
            )
            return

        try:
            requested_role = _Role(role_str)
        except ValueError:
            logger.warning("_handle_acl_apply_action: invalid role %r", role_str)
            return

        if requested_role == _Role.ADMIN:
            logger.warning("_handle_acl_apply_action: attempt to apply as admin denied")
            return

        # Check if already authorized
        existing_role = await self._acl_manager.get_role(open_id)
        if existing_role is not None:
            logger.info(
                "_handle_acl_apply_action: %r already has role %s, skipping",
                open_id, existing_role.value,
            )
            return

        app_id, existing_app = await self._acl_manager.create_application(
            open_id, "", requested_role
        )

        if existing_app is not None:
            logger.info(
                "_handle_acl_apply_action: duplicate pending app #%d for %r",
                existing_app.id, open_id,
            )
            return

        logger.info(
            "_handle_acl_apply_action: created application #%d for %r role=%s",
            app_id, open_id, requested_role.value,
        )

        # Notify reviewers and record message_ids so the card can be updated later.
        reviewer_ids = await self._acl_manager.get_reviewers_for_role(requested_role)
        notification_card = replier.build_acl_review_notification_card(
            app_id=app_id,
            applicant_name="",
            applicant_id=open_id,
            requested_role=requested_role.value,
        )
        notification_msg_ids: list[str] = []
        for reviewer_id in reviewer_ids:
            try:
                msg_id = await replier.send_to_user(reviewer_id, notification_card, "interactive")
                if msg_id:
                    notification_msg_ids.append(msg_id)
            except Exception:
                logger.exception(
                    "_handle_acl_apply_action: failed to notify reviewer %r", reviewer_id
                )
        if notification_msg_ids:
            self._review_notification_msgs[app_id] = notification_msg_ids

    async def _handle_acl_review_action(self, data: dict, replier: Replier) -> None:
        """Process an acl_review card button click (approve/reject)."""
        app_id_str: str = data.get("app_id", "")
        decision: str = data.get("decision", "")
        operator_id: str = data.get("operator_id", "")

        if not app_id_str or not decision or not operator_id:
            logger.warning(
                "_handle_acl_review_action: missing fields app_id=%r decision=%r operator=%r",
                app_id_str, decision, operator_id,
            )
            return

        try:
            app_id = int(app_id_str)
        except ValueError:
            logger.warning("_handle_acl_review_action: invalid app_id %r", app_id_str)
            return

        # Verify reviewer still has permission
        reviewer_role = await self._acl_manager.get_role(operator_id)
        if reviewer_role is None:
            logger.warning(
                "_handle_acl_review_action: reviewer %r no longer authorized", operator_id
            )
            return

        app = await self._acl_manager.get_application(app_id)
        if app is None or app.status != "pending":
            logger.info(
                "_handle_acl_review_action: app #%d not pending (status=%s)",
                app_id, app.status if app else "not found",
            )
            status_text = app.status if app else "不存在"
            try:
                await replier.send_to_user(
                    operator_id,
                    f'{{"text":"⚠️ 申请 #{app_id} 无法处理，当前状态：{status_text}。"}}',
                    "text",
                )
            except Exception:
                logger.exception(
                    "_handle_acl_review_action: failed to notify operator %r of stale app",
                    operator_id,
                )
            return

        if not self._acl_manager.can_review(reviewer_role, app.requested_role):
            logger.warning(
                "_handle_acl_review_action: reviewer %r (role=%s) cannot review %s app",
                operator_id, reviewer_role.value, app.requested_role.value,
            )
            return

        if decision == "approved":
            result = await self._acl_manager.approve(app_id, operator_id)
            if result:
                logger.info(
                    "_handle_acl_review_action: approved app #%d for %r",
                    app_id, app.applicant_id,
                )
                role_label = "Owner" if result.requested_role.value == "owner" else "Collaborator"
                done_card = replier.build_acl_review_done_card(
                    app_id, "approved", app.applicant_id, result.requested_role.value
                )
                # Update all reviewer notification cards to show the decision.
                for msg_id in self._review_notification_msgs.pop(app_id, []):
                    try:
                        await replier.update_card(msg_id, done_card)
                    except Exception:
                        logger.exception(
                            "_handle_acl_review_action: failed to update notification card %s",
                            msg_id,
                        )
                # Notify applicant.
                try:
                    await replier.send_to_user(
                        app.applicant_id,
                        '{"text":"✅ 您的权限申请已批准，您现在是 ' + role_label + '。"}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify applicant %r",
                        app.applicant_id,
                    )
                # Notify the reviewer who acted.
                try:
                    await replier.send_to_user(
                        operator_id,
                        f'{{"text":"✅ 已批准申请 #{app_id}，{app.applicant_id} 现在是 {role_label}。"}}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify operator %r", operator_id
                    )
        elif decision == "rejected":
            result = await self._acl_manager.reject(app_id, operator_id)
            if result:
                logger.info(
                    "_handle_acl_review_action: rejected app #%d for %r",
                    app_id, app.applicant_id,
                )
                done_card = replier.build_acl_review_done_card(
                    app_id, "rejected", app.applicant_id, result.requested_role.value
                )
                # Update all reviewer notification cards.
                for msg_id in self._review_notification_msgs.pop(app_id, []):
                    try:
                        await replier.update_card(msg_id, done_card)
                    except Exception:
                        logger.exception(
                            "_handle_acl_review_action: failed to update notification card %s",
                            msg_id,
                        )
                # Notify applicant.
                try:
                    await replier.send_to_user(
                        app.applicant_id,
                        '{"text":"❌ 您的权限申请已被拒绝。如有疑问请联系管理员。"}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify applicant %r",
                        app.applicant_id,
                    )
                # Notify the reviewer who acted.
                try:
                    await replier.send_to_user(
                        operator_id,
                        f'{{"text":"❌ 已拒绝申请 #{app_id}。"}}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify operator %r", operator_id
                    )
        else:
            logger.warning("_handle_acl_review_action: unknown decision %r", decision)

    # ------------------------------------------------------------------
    # Public: permission card action handling
    # ------------------------------------------------------------------

    def handle_card_action(
        self, session_id: str, index: int, project_name: str = ""
    ) -> None:
        """Resolve a pending permission via a card button click.

        Must be called from the asyncio event loop thread (use
        ``loop.call_soon_threadsafe`` when bridging from another thread).

        Args:
            session_id: The ``context_id`` stored in the button ``value``.
            index: The option index the user clicked.
            project_name: Project name stored in the button ``value``; used to
                locate the exact session in multi-project setups.  Falls back to
                searching all sessions for one with a pending permission.
        """
        user_ctx = self._session_registry.get(session_id)
        if user_ctx is None:
            logger.warning(
                "handle_card_action: no context for session_id=%r", session_id
            )
            return

        # Prefer the exact project session; fall back to scanning all sessions
        # so that cards sent before this fix (without project_name) still work.
        if project_name and project_name in user_ctx.sessions:
            candidate = user_ctx.sessions[project_name]
            sessions_to_check = [candidate]
        else:
            sessions_to_check = list(user_ctx.sessions.values())

        for session in sessions_to_check:
            if self._is_permission_reply(session, str(index)):
                self._apply_permission_reply(session, str(index))
                return

        logger.debug(
            "handle_card_action: no pending permission for session_id=%r project=%r",
            session_id,
            project_name,
        )

    # ------------------------------------------------------------------
    # Private: permission reply handling
    # ------------------------------------------------------------------

    def _apply_permission_reply(
        self, session: Session, text: str
    ) -> None:
        """Resolve the pending permission future with the user's choice.

        Args:
            session: The session that is waiting for permission.
            text: The user's digit reply (e.g. ``"1"``).
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
