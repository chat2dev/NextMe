"""Meta-command handlers for the NextMe bot.

Each ``handle_*`` coroutine corresponds to a slash command that users can
invoke directly in Feishu.  Commands are stateless helpers — they receive the
objects they need and return after sending exactly one reply.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

import json
from typing import TYPE_CHECKING, Any, Optional

from ..config.schema import AppConfig, Settings
from ..protocol.types import TaskStatus
from .interfaces import AgentRuntime, Replier
from .session import Session, UserContext

if TYPE_CHECKING:
    from ..acl.manager import AclManager
    from ..acl.schema import Role
    from ..memory.manager import MemoryManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command registry (shown in /help output)
# ---------------------------------------------------------------------------

HELP_COMMANDS: list[tuple[str, str]] = [
    ("/whoami", "查看我的 open_id 和角色"),
    ("/new", "开启新对话（清除当前对话历史）"),
    ("/stop", "取消当前执行中的任务"),
    ("/done", "关闭当前话题，释放 Claude 进程（仅群聊话题内有效）"),
    ("/thread", "查看当前群聊的活跃话题列表"),
    ("/thread close <short_id>", "强制关闭指定话题（Owner/Admin 可用）"),
    ("/help", "显示帮助"),
    ("/skill", "列出所有 Skill"),
    ("/skill <trigger>", "触发指定 Skill"),
    ("/status", "显示所有 Session 状态"),
    ("/task", "显示当前任务队列"),
    ("/project", "列出所有项目"),
    ("/project <name>", "切换活跃项目"),
    ("/project bind <name>", "将当前群聊绑定到指定项目"),
    ("/project unbind", "解除当前群聊的项目绑定"),
    ("/remember <text>", "记住一条信息（长期记忆）"),
    ("/acl list", "查看访问控制列表"),
    ("/acl add <open_id> [owner|collaborator]", "添加用户（owner/admin 可用）"),
    ("/acl remove <open_id>", "移除用户（owner/admin 可用）"),
    ("/acl pending", "查看待审批申请（owner/admin 可用）"),
    ("/acl approve <id>", "批准申请（owner/admin 可用）"),
    ("/acl reject <id>", "拒绝申请（owner/admin 可用）"),
    ("/schedule <prompt> at <time>", "单次定时任务"),
    ("/schedule <prompt> every <N><s/m/h/d>", "周期定时任务"),
    ("/schedule <prompt> cron <expr>", "Cron 定时任务"),
    ("/schedule list", "查看定时任务"),
    ("/schedule pause/resume/delete <id>", "管理定时任务"),
]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_new(
    session: Session,
    runtime: AgentRuntime | None,
    replier: Replier,
    chat_id: str,
    reply_msg_id: str = "",
) -> None:
    """Reset the agent session, clearing conversation history.

    Clears ``session.actual_id`` and calls ``reset_session()`` if a
    runtime exists.  Sends a confirmation text message.

    Args:
        session: The active session to reset.
        runtime: The associated agent runtime, if any.
        replier: Feishu message sender.
        chat_id: Target chat.
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    logger.info(
        "handle_new: resetting session for context_id=%r project=%r",
        session.context_id,
        session.project_name,
    )
    session.actual_id = ""

    if runtime is not None:
        try:
            await runtime.reset_session()
        except Exception:
            logger.exception(
                "handle_new: error resetting ACPRuntime for session %r",
                session.context_id,
            )

    try:
        if reply_msg_id:
            await replier.reply_text(reply_msg_id, "已开启新对话，历史记录已清除。", in_thread=True)
        else:
            await replier.send_text(chat_id, "已开启新对话，历史记录已清除。")
    except Exception:
        logger.exception("handle_new: failed to send confirmation to chat %r", chat_id)


async def handle_stop(
    session: Session,
    replier: Replier,
    chat_id: str,
    runtime: Optional[AgentRuntime] = None,
    reply_msg_id: str = "",
) -> None:
    """Cancel the task currently executing in *session*.

    Sets the active task's ``canceled`` flag, cancels any pending permission
    future, and calls ``runtime.cancel()`` to interrupt the agent subprocess.
    Sends a confirmation text message.

    Args:
        session: The active session.
        replier: Feishu message sender.
        chat_id: Target chat.
        runtime: The associated agent runtime, if any.  When provided,
            ``runtime.cancel()`` is called to immediately interrupt the
            in-flight subprocess (SIGTERM / session/cancel).
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    logger.info(
        "handle_stop: stopping active task for context_id=%r", session.context_id
    )
    if session.active_task is not None:
        session.active_task.canceled = True
        logger.debug(
            "handle_stop: marked task %s as cancelled", session.active_task.id
        )
    else:
        logger.debug(
            "handle_stop: no active task for context_id=%r", session.context_id
        )

    session.cancel_permission()

    if runtime is not None:
        try:
            await runtime.cancel()
        except Exception:
            logger.exception(
                "handle_stop: error calling runtime.cancel() for context_id=%r",
                session.context_id,
            )

    try:
        if reply_msg_id:
            await replier.reply_text(reply_msg_id, "已发送取消信号，当前任务将尽快停止。", in_thread=True)
        else:
            await replier.send_text(chat_id, "已发送取消信号，当前任务将尽快停止。")
    except Exception:
        logger.exception(
            "handle_stop: failed to send confirmation to chat %r", chat_id
        )


async def handle_done(
    session: Any,
    runtime: Any | None,
    replier: Any,
    chat_id: str,
    root_message_id: str,
    acp_registry: Any,
    on_thread_closed: Any,
) -> None:
    """Close a thread: cancel tasks, stop Claude subprocess, release slot, add reaction.

    Args:
        session: The thread's Session object.
        runtime: The associated agent runtime, if any.
        replier: Feishu message sender.
        chat_id: The group chat id.
        root_message_id: Root message of the thread (receives DONE reaction).
        acp_registry: ACPRuntimeRegistry for subprocess removal.
        on_thread_closed: Callback to release thread slot and process queue.
    """
    logger.info(
        "handle_done: closing thread session context_id=%r", session.context_id
    )

    # 1. Cancel active task
    if session.active_task is not None:
        session.active_task.canceled = True

    # 2. Cancel pending permission
    if hasattr(session, "cancel_permission"):
        session.cancel_permission()
    elif session.perm_future is not None and not session.perm_future.done():
        session.perm_future.cancel()

    # 3. Cancel runtime if running
    if runtime is not None:
        try:
            await runtime.cancel()
        except Exception:
            logger.exception("handle_done: error calling runtime.cancel()")

    # 4. Drain task queue
    while not session.task_queue.empty():
        try:
            session.task_queue.get_nowait()
        except Exception:
            break
    session.pending_tasks.clear()

    # 5. Stop and remove subprocess
    runtime_key = f"{session.context_id}:{session.project_name}"
    try:
        await acp_registry.remove(runtime_key)
    except Exception:
        logger.exception("handle_done: error removing runtime %r", runtime_key)

    # 6. Release thread slot (triggers pending queue)
    try:
        on_thread_closed()
    except Exception:
        logger.exception("handle_done: error in on_thread_closed callback")

    # 7. Add DONE reaction to root message
    if root_message_id:
        try:
            await replier.send_reaction(root_message_id, "DONE")
        except Exception:
            logger.exception("handle_done: failed to send DONE reaction")

    # 8. Confirm in thread
    try:
        await replier.reply_text(
            root_message_id, "✅ 话题已关闭，资源已释放。"
        )
    except Exception:
        logger.exception("handle_done: failed to send confirmation")


async def handle_help(replier: Replier, chat_id: str, reply_msg_id: str = "") -> None:
    """Send the help card listing all available commands.

    Args:
        replier: Feishu message sender.
        chat_id: Target chat.
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    logger.debug("handle_help: sending help card to chat %r", chat_id)
    try:
        card = replier.build_help_card(HELP_COMMANDS)
        if reply_msg_id:
            await replier.reply_card(reply_msg_id, card, in_thread=True)
        else:
            await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_help: failed to send help card to chat %r", chat_id)


def _get_git_branch(path: str) -> str | None:
    """Return the current git branch for *path*, or ``None`` if unavailable.

    Uses a 0.8 s timeout so a slow/absent git never blocks the status card.
    Returns ``"detached@<sha>"`` when in detached HEAD state.
    """
    # Strip git worktree env vars so a caller running inside a worktree (e.g.
    # pytest or a git hook) cannot bleed its own GIT_DIR into the subprocess
    # and cause git to talk to the wrong repository.
    _strip = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}
    env = {k: v for k, v in os.environ.items() if k not in _strip}
    try:
        # symbolic-ref works even for unborn branches (no commits yet)
        result = subprocess.run(
            ["git", "-C", path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=0.8,
            env=env,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch else None
        # Detached HEAD — return short SHA
        sha_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=0.8,
            env=env,
        )
        if sha_result.returncode != 0:
            return None
        sha = sha_result.stdout.strip()
        return f"detached@{sha}" if sha else None
    except Exception:
        return None


async def handle_status(
    user_ctx: UserContext,
    replier: Replier,
    chat_id: str,
    reply_msg_id: str = "",
) -> None:
    """Send a status card showing all active project sessions for *user_ctx*.

    Each session is rendered as a separate section showing project name, path,
    current status, executor, ACP session ID, active task and queue depth.
    The active project is highlighted with a star prefix.

    Args:
        user_ctx: The user context containing all sessions.
        replier: Feishu message sender.
        chat_id: Target chat.
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    logger.debug(
        "handle_status: sending status for context_id=%r", user_ctx.context_id
    )

    if not user_ctx.sessions:
        try:
            if reply_msg_id:
                await replier.reply_text(reply_msg_id, "当前没有活跃 Session。", in_thread=True)
            else:
                await replier.send_text(chat_id, "当前没有活跃 Session。")
        except Exception:
            logger.exception(
                "handle_status: failed to send empty-status message to chat %r", chat_id
            )
        return

    sections: list[str] = []
    for project_name, session in user_ctx.sessions.items():
        active_marker = "★ " if project_name == user_ctx.active_project else ""
        queue_size = session.task_queue.qsize()

        # Session ID label
        if session.actual_id:
            session_label = f"`{session.actual_id[:16]}…`"
        elif session.status.value == "executing":
            session_label = "_(初始化中…)_"
        else:
            session_label = "_(无)_"

        # Status emoji
        status_emoji = {
            "idle": "💤",
            "executing": "⚙️",
            "waiting_permission": "🔐",
            "canceled": "🚫",
            "done": "✅",
        }.get(session.status.value, "❓")

        # Active task label: show content preview instead of raw UUID
        if session.active_task:
            preview = session.active_task.content.replace("\n", " ")
            if len(preview) > 50:
                preview = preview[:50] + "…"
            task_label = f"`{preview}`"
        else:
            task_label = "_无_"

        queue_label = f"**{queue_size}** 个待处理" if queue_size > 0 else "_无_"

        branch = await asyncio.to_thread(_get_git_branch, str(session.project_path))
        branch_line = f"🌿 分支: `{branch}`" if branch else ""

        section_lines = [
            f"**{active_marker}{session.project_name}**　{status_emoji} {session.status.value}",
            f"📁 `{session.project_path}`",
        ]
        if branch_line:
            section_lines.append(branch_line)
        section_lines += [
            f"🔧 执行器: `{session.executor}`　　Session: {session_label}",
            f"📝 当前任务: {task_label}",
            f"📋 队列: {queue_label}",
        ]
        section = "\n".join(section_lines)
        sections.append(section)

    content = "\n\n---\n\n".join(sections)

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 Session 状态"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
            ]
        },
    }

    try:
        card_json = json.dumps(card, ensure_ascii=False)
        if reply_msg_id:
            await replier.reply_card(reply_msg_id, card_json, in_thread=True)
        else:
            await replier.send_card(chat_id, card_json)
    except Exception:
        logger.exception(
            "handle_status: failed to send status card to chat %r", chat_id
        )


async def handle_bind(
    chat_id: str,
    project_name: str,
    config: AppConfig,
    replier: Replier,
    reply_msg_id: str = "",
) -> Optional[str]:
    """Bind *chat_id* to *project_name*, returning the name on success or ``None``.

    Validates that the project exists in *config* before accepting the binding.
    Sends a confirmation or error message to the chat.

    Args:
        chat_id: Feishu chat identifier to bind.
        project_name: Name of the project to bind this chat to.
        config: Application configuration containing the project list.
        replier: Feishu message sender.
        reply_msg_id: When set, reply in-thread to this message id (group chats).

    Returns:
        *project_name* when the project was found and the binding is accepted;
        ``None`` when the project does not exist.
    """
    project = config.get_project(project_name)
    if project is None:
        available = ", ".join(f"`{p.name}`" for p in config.projects)
        msg = (
            f"未找到项目 `{project_name}`。\n"
            f"可用项目: {available or '(无)'}。\n"
            "请检查配置文件。"
        )
        try:
            if reply_msg_id:
                await replier.reply_text(reply_msg_id, msg, in_thread=True)
            else:
                await replier.send_text(chat_id, msg)
        except Exception:
            logger.exception(
                "handle_bind: failed to send 'not found' message to chat %r", chat_id
            )
        return None

    logger.info("handle_bind: binding chat %r → project %r", chat_id, project_name)
    confirm_msg = (
        f"已将当前群聊绑定到项目 **{project.name}**\n"
        f"路径: `{project.path}`\n"
        "后续消息将自动路由到该项目。"
    )
    try:
        if reply_msg_id:
            await replier.reply_text(reply_msg_id, confirm_msg, in_thread=True)
        else:
            await replier.send_text(chat_id, confirm_msg)
    except Exception:
        logger.exception(
            "handle_bind: failed to send confirmation to chat %r", chat_id
        )
    return project_name


async def handle_unbind(chat_id: str, replier: Replier, reply_msg_id: str = "") -> bool:
    """Remove a chat→project binding for *chat_id*.

    Always sends a confirmation message.  Returns ``True`` so the caller can
    update its in-memory binding map and the persistent store.

    Args:
        chat_id: Feishu chat identifier whose binding should be removed.
        replier: Feishu message sender.
        reply_msg_id: When set, reply in-thread to this message id (group chats).

    Returns:
        ``True`` (always; the caller decides what to do if no binding existed).
    """
    logger.info("handle_unbind: removing binding for chat %r", chat_id)
    try:
        if reply_msg_id:
            await replier.reply_text(reply_msg_id, "已解除当前群聊的项目绑定，恢复使用活跃项目。", in_thread=True)
        else:
            await replier.send_text(chat_id, "已解除当前群聊的项目绑定，恢复使用活跃项目。")
    except Exception:
        logger.exception(
            "handle_unbind: failed to send confirmation to chat %r", chat_id
        )
    return True


async def handle_remember(
    user_id: str,
    text: str,
    memory_manager: "MemoryManager",
    replier: Replier,
    chat_id: str,
    reply_msg_id: str = "",
) -> None:
    """Save a user-supplied fact to long-term memory (global across all chats).

    Facts are keyed by *user_id* so the same memory is shared regardless of
    which chat the user interacts from.

    Args:
        user_id: Feishu user identifier (``ou_xxx``).
        text: The fact text to remember.
        memory_manager: The :class:`~nextme.memory.manager.MemoryManager` instance.
        replier: Feishu message sender.
        chat_id: Target chat.
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    from ..memory.schema import Fact

    logger.info(
        "handle_remember: saving fact for user_id=%r: %r", user_id, text
    )
    try:
        await memory_manager.load(user_id)
        fact = Fact(text=text, source="user_command")
        memory_manager.add_fact(user_id, fact)
    except Exception:
        logger.exception(
            "handle_remember: error saving fact for user_id=%r", user_id
        )
    try:
        if reply_msg_id:
            await replier.reply_text(reply_msg_id, f"已记住：{text}", in_thread=True)
        else:
            await replier.send_text(chat_id, f"已记住：{text}")
    except Exception:
        logger.exception(
            "handle_remember: failed to send confirmation to chat %r", chat_id
        )


async def handle_threads_list(
    chat_id: str,
    threads: list,
    replier: Replier,
    reply_msg_id: str = "",
) -> None:
    """Send a card listing all active threads for *chat_id*.

    Args:
        chat_id: Feishu group chat identifier.
        threads: List of ThreadRecord objects sorted by created_at.
        replier: Feishu message sender.
        reply_msg_id: When set, reply in-thread to this message id.
    """
    if not threads:
        msg = "当前群聊没有活跃话题。"
        try:
            if reply_msg_id:
                await replier.reply_text(reply_msg_id, msg, in_thread=True)
            else:
                await replier.send_text(chat_id, msg)
        except Exception:
            logger.exception("handle_threads_list: failed to send to chat %r", chat_id)
        return

    lines: list[str] = []
    for i, t in enumerate(threads, 1):
        short_id = t.thread_root_id[:8]
        created = t.created_at.strftime("%m-%d %H:%M")
        lines.append(f"**{i}.** `{short_id}…`  📁 {t.project_name}  🕐 {created}")

    lines.append("")
    lines.append("关闭话题: `/thread close <short_id>`")

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"活跃话题（{len(threads)} 个）"},
            "template": "blue",
        },
        "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
    }
    card_json = json.dumps(card, ensure_ascii=False)
    try:
        if reply_msg_id:
            await replier.reply_card(reply_msg_id, card_json, in_thread=True)
        else:
            await replier.send_card(chat_id, card_json)
    except Exception:
        logger.exception("handle_threads_list: failed to send card to chat %r", chat_id)


async def handle_project(
    user_ctx: UserContext,
    project_name: str,
    config: AppConfig,
    settings: Settings,
    replier: Replier,
    chat_id: str,
    reply_msg_id: str = "",
) -> None:
    """Switch the active project for *user_ctx*.

    Looks up *project_name* in ``config.projects``.  If found, creates or
    reuses the session for that project and sets it as active.  Sends a
    confirmation or error message.

    Args:
        user_ctx: The user's context (all sessions).
        project_name: Name of the project to activate.
        config: Application configuration containing the project list.
        settings: Application settings.
        replier: Feishu message sender.
        chat_id: Target chat.
        reply_msg_id: When set, reply in-thread to this message id (group chats).
    """
    logger.info(
        "handle_project: context_id=%r switching to project %r",
        user_ctx.context_id,
        project_name,
    )
    project = config.get_project(project_name)
    if project is None:
        available = ", ".join(f"`{p.name}`" for p in config.projects)
        msg = (
            f"未找到项目 `{project_name}`。\n"
            f"可用项目: {available or '(无)'}。\n"
            "请检查配置文件。"
        )
        try:
            if reply_msg_id:
                await replier.reply_text(reply_msg_id, msg, in_thread=True)
            else:
                await replier.send_text(chat_id, msg)
        except Exception:
            logger.exception(
                "handle_project: failed to send 'not found' message to chat %r",
                chat_id,
            )
        return

    session = user_ctx.get_or_create_session(project, settings)
    logger.info(
        "handle_project: activated project %r (path=%s) for context_id=%r",
        project.name,
        project.path,
        user_ctx.context_id,
    )

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 项目已切换"},
            "template": "green",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**项目**　`{session.project_name}`\n"
                        f"**路径**　`{session.project_path}`\n"
                        f"**执行器**　`{session.executor}`"
                    ),
                }
            ]
        },
    }
    try:
        card_json = json.dumps(card, ensure_ascii=False)
        if reply_msg_id:
            await replier.reply_card(reply_msg_id, card_json, in_thread=True)
        else:
            await replier.send_card(chat_id, card_json)
    except Exception:
        logger.exception(
            "handle_project: failed to send confirmation to chat %r", chat_id
        )


async def handle_whoami(
    user_id: str,
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Show the caller's own open_id, role, and join info."""
    from ..acl.schema import Role as _Role

    role = await acl_manager.get_role(user_id)
    user = None
    if role not in (None, _Role.ADMIN):
        user = await acl_manager.get_user(user_id)

    card = replier.build_whoami_card(user_id, role, user)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_whoami: failed to send card to %r", chat_id)


async def handle_acl_list(
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Send the ACL list card (admins, owners, collaborators)."""
    from ..acl.schema import Role as _Role

    admin_ids = acl_manager.get_admin_ids()
    owners = await acl_manager.list_users(_Role.OWNER)
    collaborators = await acl_manager.list_users(_Role.COLLABORATOR)
    card = replier.build_acl_list_card(admin_ids, owners, collaborators)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_acl_list: failed to send card to %r", chat_id)


async def handle_acl_add(
    actor_id: str,
    actor_role: "Role",
    target_id: str,
    target_role_str: str,
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Add a user to the ACL. Enforces role-based permission."""
    from ..acl.schema import Role as _Role

    try:
        target_role = _Role(target_role_str.lower()) if target_role_str else _Role.COLLABORATOR
    except ValueError:
        await replier.send_text(
            chat_id, f"未知角色 `{target_role_str}`，可选值: owner / collaborator"
        )
        return

    if target_role == _Role.ADMIN:
        await replier.send_text(chat_id, "无法通过命令添加 Admin，请修改 settings.json。")
        return

    if not acl_manager.can_add(actor_role, target_role):
        await replier.send_text(chat_id, "权限不足：您无法添加该角色。")
        return

    role_label = "Owner（负责人）" if target_role == _Role.OWNER else "Collaborator（协作者）"
    try:
        await acl_manager.add_user(target_id, target_role, added_by=actor_id)
        await replier.send_text(
            chat_id, f"已将 `{target_id}` 添加为 {role_label}。"
        )
    except Exception:
        logger.exception("handle_acl_add: failed to add user %r", target_id)
        await replier.send_text(chat_id, "添加失败，请检查日志。")


async def handle_acl_remove(
    actor_id: str,
    actor_role: "Role",
    target_id: str,
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Remove a user from the ACL. Enforces role-based permission."""
    # Check admin guard first — admins have no DB row, so get_user returns None.
    if target_id in acl_manager.get_admin_ids():
        await replier.send_text(
            chat_id, "无法移除管理员，请修改 settings.json 中的 admin_users。"
        )
        return

    target = await acl_manager.get_user(target_id)
    if target is None:
        await replier.send_text(chat_id, f"未找到用户 `{target_id}`。")
        return

    if not acl_manager.can_remove(actor_role, target):
        await replier.send_text(chat_id, "权限不足：您无法移除该用户。")
        return

    try:
        await acl_manager.remove_user(target_id)
        await replier.send_text(chat_id, f"✅ 已移除用户 `{target_id}`。")
    except ValueError as e:
        await replier.send_text(chat_id, str(e))
    except Exception:
        logger.exception("handle_acl_remove: failed to remove user %r", target_id)
        await replier.send_text(chat_id, "移除失败，请检查日志。")


async def handle_acl_pending(
    viewer_role: "Role",
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Show pending applications the viewer is allowed to review."""
    applications = await acl_manager.list_pending(viewer_role)
    card = replier.build_acl_pending_card(applications, viewer_role)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_acl_pending: failed to send card to %r", chat_id)


async def handle_acl_approve(
    app_id: int,
    reviewer_id: str,
    reviewer_role: "Role",
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Approve a pending application."""
    app = await acl_manager.get_application(app_id)
    if app is None:
        await replier.send_text(chat_id, f"未找到申请 #{app_id}。")
        return
    if not acl_manager.can_review(reviewer_role, app.requested_role):
        await replier.send_text(chat_id, "权限不足：您无法审批该申请。")
        return

    result = await acl_manager.approve(app_id, reviewer_id)
    if result is None:
        await replier.send_text(chat_id, f"申请 #{app_id} 已处理或不存在。")
        return
    role_label = "Owner" if result.requested_role.value == "owner" else "Collaborator"
    await replier.send_text(
        chat_id,
        f"已批准申请 #{app_id}，{result.applicant_name or result.applicant_id} 现在是 {role_label}。",
    )


async def handle_acl_reject(
    app_id: int,
    reviewer_id: str,
    reviewer_role: "Role",
    acl_manager: AclManager,
    replier: Replier,
    chat_id: str,
) -> None:
    """Reject a pending application."""
    app = await acl_manager.get_application(app_id)
    if app is None:
        await replier.send_text(chat_id, f"未找到申请 #{app_id}。")
        return
    if not acl_manager.can_review(reviewer_role, app.requested_role):
        await replier.send_text(chat_id, "权限不足：您无法审批该申请。")
        return

    result = await acl_manager.reject(app_id, reviewer_id)
    if result is None:
        await replier.send_text(chat_id, f"申请 #{app_id} 已处理或不存在。")
        return
    await replier.send_text(
        chat_id,
        f"已拒绝申请 #{app_id}（{result.applicant_name or result.applicant_id}）。",
    )
