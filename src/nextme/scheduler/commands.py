"""Command parser and handler for /schedule command."""
from __future__ import annotations

import re
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .schema import ScheduledTask, ScheduleType

logger = logging.getLogger(__name__)

_USAGE = (
    "用法: /schedule <子命令>\n\n"
    "子命令:\n"
    "  <提示词> at <ISO时间>         — 在指定时间执行一次\n"
    "  <提示词> every <N><s|m|h|d>  — 每隔 N 秒/分/时/天执行\n"
    "  <提示词> cron <5部分表达式>   — 按 cron 表达式执行\n"
    "  list                          — 列出当前会话的所有任务\n"
    "  pause <任务ID>                — 暂停任务\n"
    "  resume <任务ID>               — 恢复任务\n"
    "  delete <任务ID>               — 删除任务\n\n"
    "示例:\n"
    "  /schedule 发送日报 at 2026-03-20T09:00:00+08:00\n"
    "  /schedule 检查新闻 every 2h\n"
    "  /schedule 周报 cron 0 9 * * 1\n\n"
    "支持自然语言:\n"
    "  /schedule 每小时提醒我喝水\n"
    "  /schedule 每天9点发日报\n"
    "  /schedule 30分钟后提醒我开会\n"
    "  /schedule 30分钟后提醒我开会"
)

# Interval unit multipliers: s=1, m=60, h=3600, d=86400
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

# Chinese unit → seconds
_CN_SEC: dict[str, int] = {
    "秒": 1,
    "分": 60,
    "分钟": 60,
    "小时": 3600,
    "时": 3600,
    "天": 86400,
    "日": 86400,
}

# Ordered from most-specific to least-specific to avoid partial matches
_CN_PATTERNS: list[tuple[str, str]] = [
    # "X分钟/小时后" — once in the future (must come before interval patterns)
    (r"^(\d+)(分钟|分|小时|时)后\s*(.+)$", "future_prefix"),
    (r"^(.+?)\s*(\d+)(分钟|分|小时|时)后$", "future_suffix"),
    # 每天N点 — daily cron (before plain 每天 so 每天9点 doesn't hit 每天)
    (r"^每天\s*(\d+)\s*[点时]\s*(.+)$", "daily_at_prefix"),
    (r"^(.+?)\s*每天\s*(\d+)\s*[点时]$", "daily_at_suffix"),
    # Prefix: 每(N?)(unit)(prompt)
    (r"^每(\d+)?(秒|分钟|分|小时|时|天|日)\s*(.+)$", "interval_prefix"),
    # Suffix: (prompt)每(N?)(unit)
    (r"^(.+?)\s*每(\d+)?(秒|分钟|分|小时|时|天|日)$", "interval_suffix"),
]

# Regex patterns
_RE_AT = re.compile(r"^(.+?)\s+at\s+(\S+)$", re.IGNORECASE)
_RE_EVERY = re.compile(r"^(.+?)\s+every\s+(\d+)([smhd])$", re.IGNORECASE)
_RE_CRON = re.compile(r"^(.+?)\s+cron\s+(.+)$", re.IGNORECASE)
_RE_LIST = re.compile(r"^list$", re.IGNORECASE)
_RE_ACTION = re.compile(r"^(pause|resume|delete)\s+(\S+)$", re.IGNORECASE)

# Validate a 5-part cron expression: each part is a valid cron field
_RE_CRON_PART = re.compile(
    r"^(\*|\d+|\d+-\d+|\*/\d+|\d+(,\d+)*)$"
)


def _validate_cron(expr: str) -> bool:
    """Basic validation of a 5-part cron expression."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    for part in parts:
        if not _RE_CRON_PART.match(part):
            return False
    return True


@dataclass
class ParsedSchedule:
    """Result of parsing a /schedule command argument string."""

    action: str  # "create" | "list" | "pause" | "resume" | "delete"
    prompt: str | None = None
    schedule_type: str | None = None  # "once" | "interval" | "cron"
    schedule_value: str | None = None
    next_run_at: datetime | None = None
    target_id: str | None = None  # for pause/resume/delete


def parse_schedule_command(raw_args: str) -> ParsedSchedule | None:
    """Parse /schedule arguments into a ParsedSchedule.

    Returns None if the input cannot be parsed into a valid command.
    """
    text = raw_args.strip()

    # list
    if _RE_LIST.match(text):
        return ParsedSchedule(action="list")

    # pause / resume / delete <id>
    m = _RE_ACTION.match(text)
    if m:
        return ParsedSchedule(action=m.group(1).lower(), target_id=m.group(2))

    # <prompt> at <ISO datetime>
    m = _RE_AT.match(text)
    if m:
        prompt = m.group(1).strip()
        dt_str = m.group(2)
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return ParsedSchedule(
            action="create",
            prompt=prompt,
            schedule_type="once",
            schedule_value=dt_str,
            next_run_at=dt,
        )

    # <prompt> every <N><unit>
    m = _RE_EVERY.match(text)
    if m:
        prompt = m.group(1).strip()
        n = int(m.group(2))
        unit = m.group(3).lower()
        seconds = n * _UNIT_SECONDS[unit]
        next_run = datetime.now(timezone.utc)
        return ParsedSchedule(
            action="create",
            prompt=prompt,
            schedule_type="interval",
            schedule_value=str(seconds),
            next_run_at=next_run,
        )

    # <prompt> cron <5-part expression>
    m = _RE_CRON.match(text)
    if m:
        prompt = m.group(1).strip()
        cron_expr = m.group(2).strip()
        if not _validate_cron(cron_expr):
            return None
        # Compute next_run_at using croniter if available, else use now
        next_run = _next_cron_run(cron_expr)
        return ParsedSchedule(
            action="create",
            prompt=prompt,
            schedule_type="cron",
            schedule_value=cron_expr,
            next_run_at=next_run,
        )

    # Fallback: try Chinese natural language
    return _parse_chinese_schedule(text)


def _parse_chinese_schedule(text: str) -> "ParsedSchedule | None":
    """Try to extract schedule intent from Chinese natural language."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    for pattern, kind in _CN_PATTERNS:
        m = re.match(pattern, text.strip())
        if not m:
            continue

        if kind == "future_prefix":
            n, unit, prompt = int(m.group(1)), m.group(2), m.group(3).strip()
            if not prompt:
                continue
            seconds = n * _CN_SEC[unit]
            run_at = now + timedelta(seconds=seconds)
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="once",
                schedule_value=run_at.isoformat(),
                next_run_at=run_at,
            )

        elif kind == "future_suffix":
            prompt, n, unit = m.group(1).strip(), int(m.group(2)), m.group(3)
            if not prompt:
                continue
            seconds = n * _CN_SEC[unit]
            run_at = now + timedelta(seconds=seconds)
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="once",
                schedule_value=run_at.isoformat(),
                next_run_at=run_at,
            )

        elif kind == "daily_at_prefix":
            hour, prompt = int(m.group(1)), m.group(2).strip()
            if not prompt or hour > 23:
                return None
            cron_expr = f"0 {hour} * * *"
            next_run = _next_cron_run(cron_expr)
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="cron",
                schedule_value=cron_expr,
                next_run_at=next_run,
            )

        elif kind == "daily_at_suffix":
            prompt, hour = m.group(1).strip(), int(m.group(2))
            if not prompt or hour > 23:
                return None
            cron_expr = f"0 {hour} * * *"
            next_run = _next_cron_run(cron_expr)
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="cron",
                schedule_value=cron_expr,
                next_run_at=next_run,
            )

        elif kind == "interval_prefix":
            n_str, unit, prompt = m.group(1), m.group(2), m.group(3).strip()
            n = int(n_str) if n_str else 1
            if not prompt:
                continue
            seconds = n * _CN_SEC[unit]
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="interval",
                schedule_value=str(seconds),
                next_run_at=now + timedelta(seconds=seconds),
            )

        elif kind == "interval_suffix":
            prompt, n_str, unit = m.group(1).strip(), m.group(2), m.group(3)
            n = int(n_str) if n_str else 1
            if not prompt:
                continue
            seconds = n * _CN_SEC[unit]
            return ParsedSchedule(
                action="create",
                prompt=prompt,
                schedule_type="interval",
                schedule_value=str(seconds),
                next_run_at=now + timedelta(seconds=seconds),
            )

    return None


def _next_cron_run(cron_expr: str) -> datetime:
    """Compute next run time for a cron expression. Uses croniter if available."""
    try:
        from croniter import croniter  # type: ignore[import]
        now = datetime.now(timezone.utc)
        it = croniter(cron_expr, now)
        nxt = it.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt
    except ImportError:
        return datetime.now(timezone.utc)


def _find_task_by_prefix(tasks: list[ScheduledTask], prefix: str) -> ScheduledTask | None:
    """Find the first task whose id starts with the given prefix."""
    for task in tasks:
        if task.id.startswith(prefix):
            return task
    return None


async def handle_schedule(
    raw_args: str,
    chat_id: str,
    creator_open_id: str,
    replier: Any,
    db: Any,
    project_name: str | None = None,
) -> None:
    """Handle a /schedule command.

    Args:
        raw_args: Everything after "/schedule ".
        chat_id: Feishu chat ID.
        creator_open_id: Open ID of the user who issued the command.
        replier: Replier instance with send_text(chat_id, text) async method.
        db: SchedulerDb (or compatible async mock) instance.
        project_name: Optional project name to associate with created tasks.
    """
    parsed = parse_schedule_command(raw_args)

    if parsed is None:
        await replier.send_text(chat_id, _USAGE)
        return

    if parsed.action == "list":
        tasks = await db.list_by_chat(chat_id)
        if not tasks:
            await replier.send_text(chat_id, "没有找到任何定时任务。")
            return

        lines = ["当前定时任务列表:\n"]
        for t in tasks:
            short_id = t.id[:8]
            status_label = {"active": "运行中", "paused": "已暂停", "done": "已完成"}.get(t.status, t.status)
            next_run_str = t.next_run_at.strftime("%Y-%m-%d %H:%M UTC") if t.next_run_at else "-"
            lines.append(
                f"[{short_id}] {t.prompt}\n"
                f"  类型: {t.schedule_type.value}  状态: {status_label}\n"
                f"  下次执行: {next_run_str}\n"
            )
        await replier.send_text(chat_id, "\n".join(lines))
        return

    if parsed.action == "create":
        assert parsed.prompt is not None
        assert parsed.schedule_type is not None
        assert parsed.schedule_value is not None
        assert parsed.next_run_at is not None

        task_id = str(uuid.uuid4())
        task = ScheduledTask(
            id=task_id,
            chat_id=chat_id,
            creator_open_id=creator_open_id,
            prompt=parsed.prompt,
            schedule_type=ScheduleType(parsed.schedule_type),
            schedule_value=parsed.schedule_value,
            next_run_at=parsed.next_run_at,
            project_name=project_name,
        )
        await db.create(task)
        short_id = task_id[:8]
        await replier.send_text(
            chat_id,
            f"已创建定时任务 [{short_id}]: {parsed.prompt}\n类型: {parsed.schedule_type}",
        )
        return

    # pause / resume / delete
    assert parsed.target_id is not None
    tasks = await db.list_by_chat(chat_id)
    task = _find_task_by_prefix(tasks, parsed.target_id)
    if task is None:
        await replier.send_text(chat_id, f"未找到 ID 前缀为 {parsed.target_id!r} 的任务。")
        return

    if parsed.action == "pause":
        await db.update_status(task.id, "paused")
        await replier.send_text(chat_id, f"已暂停任务 [{task.id[:8]}]: {task.prompt}")
    elif parsed.action == "resume":
        await db.update_status(task.id, "active")
        await replier.send_text(chat_id, f"已恢复任务 [{task.id[:8]}]: {task.prompt}")
    elif parsed.action == "delete":
        await db.delete(task.id)
        await replier.send_text(chat_id, f"已删除任务 [{task.id[:8]}]: {task.prompt}")
