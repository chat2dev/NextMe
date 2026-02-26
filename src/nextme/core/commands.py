"""Meta-command handlers for the NextMe bot.

Each ``handle_*`` coroutine corresponds to a slash command that users can
invoke directly in Feishu.  Commands are stateless helpers — they receive the
objects they need and return after sending exactly one reply.
"""

from __future__ import annotations

import logging

import json
from typing import TYPE_CHECKING, Optional

from ..config.schema import AppConfig, Settings
from ..protocol.types import TaskStatus
from .interfaces import AgentRuntime, Replier
from .session import Session, UserContext

if TYPE_CHECKING:
    from ..memory.manager import MemoryManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command registry (shown in /help output)
# ---------------------------------------------------------------------------

HELP_COMMANDS: list[tuple[str, str]] = [
    ("/new", "开启新对话（清除当前对话历史）"),
    ("/stop", "取消当前执行中的任务"),
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
]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_new(
    session: Session,
    runtime: AgentRuntime | None,
    replier: Replier,
    chat_id: str,
) -> None:
    """Reset the agent session, clearing conversation history.

    Clears ``session.actual_id`` and calls ``reset_session()`` if a
    runtime exists.  Sends a confirmation text message.

    Args:
        session: The active session to reset.
        runtime: The associated agent runtime, if any.
        replier: Feishu message sender.
        chat_id: Target chat.
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
        await replier.send_text(chat_id, "已开启新对话，历史记录已清除。")
    except Exception:
        logger.exception("handle_new: failed to send confirmation to chat %r", chat_id)


async def handle_stop(
    session: Session,
    replier: Replier,
    chat_id: str,
) -> None:
    """Cancel the task currently executing in *session*.

    Sets the active task's ``canceled`` flag and cancels any pending permission
    future.  Sends a confirmation text message.

    Args:
        session: The active session.
        replier: Feishu message sender.
        chat_id: Target chat.
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

    try:
        await replier.send_text(chat_id, "已发送取消信号，当前任务将尽快停止。")
    except Exception:
        logger.exception(
            "handle_stop: failed to send confirmation to chat %r", chat_id
        )


async def handle_help(replier: Replier, chat_id: str) -> None:
    """Send the help card listing all available commands.

    Args:
        replier: Feishu message sender.
        chat_id: Target chat.
    """
    logger.debug("handle_help: sending help card to chat %r", chat_id)
    try:
        card = replier.build_help_card(HELP_COMMANDS)
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_help: failed to send help card to chat %r", chat_id)


async def handle_status(
    user_ctx: UserContext,
    replier: Replier,
    chat_id: str,
) -> None:
    """Send a status card showing all active project sessions for *user_ctx*.

    Each session is rendered as a separate section showing project name, path,
    current status, executor, ACP session ID, active task and queue depth.
    The active project is highlighted with a star prefix.

    Args:
        user_ctx: The user context containing all sessions.
        replier: Feishu message sender.
        chat_id: Target chat.
    """
    logger.debug(
        "handle_status: sending status for context_id=%r", user_ctx.context_id
    )

    if not user_ctx.sessions:
        try:
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
            session_label = session.actual_id
        elif session.status.value == "executing":
            session_label = "(初始化中…)"
        else:
            session_label = "(无)"

        # Active task label: show content preview instead of raw UUID
        if session.active_task:
            preview = session.active_task.content.replace("\n", " ")
            if len(preview) > 40:
                preview = preview[:40] + "…"
            task_label = f"`{preview}`"
        else:
            task_label = "无"

        section = "\n".join([
            f"**{active_marker}{session.project_name}**",
            f"路径: `{session.project_path}`",
            f"状态: {session.status.value}  执行器: {session.executor}",
            f"Session: {session_label}",
            f"当前任务: {task_label}  队列: {queue_size}",
        ])
        sections.append(section)

    content = "\n\n---\n\n".join(sections)

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Session 状态"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
            ]
        },
    }

    try:
        await replier.send_card(chat_id, json.dumps(card, ensure_ascii=False))
    except Exception:
        logger.exception(
            "handle_status: failed to send status card to chat %r", chat_id
        )


async def handle_bind(
    chat_id: str,
    project_name: str,
    config: AppConfig,
    replier: Replier,
) -> Optional[str]:
    """Bind *chat_id* to *project_name*, returning the name on success or ``None``.

    Validates that the project exists in *config* before accepting the binding.
    Sends a confirmation or error message to the chat.

    Args:
        chat_id: Feishu chat identifier to bind.
        project_name: Name of the project to bind this chat to.
        config: Application configuration containing the project list.
        replier: Feishu message sender.

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
            await replier.send_text(chat_id, msg)
        except Exception:
            logger.exception(
                "handle_bind: failed to send 'not found' message to chat %r", chat_id
            )
        return None

    logger.info("handle_bind: binding chat %r → project %r", chat_id, project_name)
    try:
        await replier.send_text(
            chat_id,
            f"已将当前群聊绑定到项目 **{project.name}**\n"
            f"路径: `{project.path}`\n"
            "后续消息将自动路由到该项目。",
        )
    except Exception:
        logger.exception(
            "handle_bind: failed to send confirmation to chat %r", chat_id
        )
    return project_name


async def handle_unbind(chat_id: str, replier: Replier) -> bool:
    """Remove a chat→project binding for *chat_id*.

    Always sends a confirmation message.  Returns ``True`` so the caller can
    update its in-memory binding map and the persistent store.

    Args:
        chat_id: Feishu chat identifier whose binding should be removed.
        replier: Feishu message sender.

    Returns:
        ``True`` (always; the caller decides what to do if no binding existed).
    """
    logger.info("handle_unbind: removing binding for chat %r", chat_id)
    try:
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
        await replier.send_text(chat_id, f"已记住：{text}")
    except Exception:
        logger.exception(
            "handle_remember: failed to send confirmation to chat %r", chat_id
        )


async def handle_project(
    user_ctx: UserContext,
    project_name: str,
    config: AppConfig,
    settings: Settings,
    replier: Replier,
    chat_id: str,
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

    msg = (
        f"已切换到项目 **{session.project_name}**\n"
        f"路径: `{session.project_path}`"
    )
    try:
        await replier.send_text(chat_id, msg)
    except Exception:
        logger.exception(
            "handle_project: failed to send confirmation to chat %r", chat_id
        )
