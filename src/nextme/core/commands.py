"""Meta-command handlers for the NextMe bot.

Each ``handle_*`` coroutine corresponds to a slash command that users can
invoke directly in Feishu.  Commands are stateless helpers — they receive the
objects they need and return after sending exactly one reply.
"""

from __future__ import annotations

import logging

from ..config.schema import AppConfig, Settings
from ..protocol.types import TaskStatus
from .interfaces import AgentRuntime, Replier
from .session import Session, UserContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command registry (shown in /help output)
# ---------------------------------------------------------------------------

HELP_COMMANDS: list[tuple[str, str]] = [
    ("/new", "重置 ACP Session（清除对话历史）"),
    ("/stop", "取消当前执行中的任务"),
    ("/help", "显示帮助"),
    ("/skill <trigger>", "触发指定 Skill"),
    ("/status", "显示当前 Session 状态"),
    ("/project <name>", "切换活跃项目"),
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
        await replier.send_text(chat_id, "Session 已重置，对话历史已清除。")
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
    session: Session,
    replier: Replier,
    chat_id: str,
) -> None:
    """Send a status card showing the current session state.

    Displayed fields:
    - Project name and path
    - Current status
    - ACP session ID (if known)
    - Active task ID (if any)
    - Queue depth

    Args:
        session: The active session.
        replier: Feishu message sender.
        chat_id: Target chat.
    """
    logger.debug(
        "handle_status: sending status for context_id=%r", session.context_id
    )
    queue_size = session.task_queue.qsize()
    lines: list[str] = [
        f"**项目**: {session.project_name}",
        f"**路径**: `{session.project_path}`",
        f"**状态**: {session.status.value}",
        f"**执行器**: {session.executor}",
        f"**ACP Session**: {session.actual_id or '(未初始化)'}",
        f"**当前任务**: {session.active_task.id if session.active_task else '无'}",
        f"**队列深度**: {queue_size}",
    ]
    content = "\n".join(lines)

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

    import json

    try:
        await replier.send_card(chat_id, json.dumps(card, ensure_ascii=False))
    except Exception:
        logger.exception(
            "handle_status: failed to send status card to chat %r", chat_id
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
