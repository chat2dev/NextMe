"""Scheduler polling loop — isolated asyncio task, never blocks main loop."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .db import SchedulerDb
from .schema import ScheduledTask, ScheduleType, TaskRunLog

if TYPE_CHECKING:
    from ..core.dispatcher import TaskDispatcher

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 30.0  # seconds


def compute_next_run(
    task: ScheduledTask,
    reference: datetime | None = None,
) -> datetime | None:
    """Compute next_run_at after a successful run. Returns None for ONCE tasks."""
    now = reference or datetime.now(timezone.utc)

    if task.schedule_type == ScheduleType.ONCE:
        return None

    if task.schedule_type == ScheduleType.INTERVAL:
        from datetime import timedelta
        seconds = float(task.schedule_value)
        return now + timedelta(seconds=seconds)

    if task.schedule_type == ScheduleType.CRON:
        try:
            from croniter import croniter  # type: ignore[import]
            cron = croniter(task.schedule_value, now)
            next_dt: datetime = cron.get_next(datetime)
            if next_dt.tzinfo is None:
                next_dt = next_dt.replace(tzinfo=timezone.utc)
            return next_dt
        except Exception:
            logger.exception("compute_next_run: bad cron expression %r", task.schedule_value)
            return None

    return None


class SchedulerEngine:
    """Polls the DB every poll_interval seconds and fires due tasks."""

    def __init__(
        self,
        db: SchedulerDb,
        dispatcher: Any,  # TaskDispatcher — Any to avoid circular import
        feishu_client: Any,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        overdue_grace_minutes: int = 5,
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher
        self._feishu_client = feishu_client
        self._poll_interval = poll_interval
        self._overdue_grace_minutes = overdue_grace_minutes

    async def run(self) -> None:
        """Entry point — call as asyncio.create_task(engine.run())."""
        logger.info("SchedulerEngine: started (poll_interval=%.1fs)", self._poll_interval)
        try:
            while True:
                try:
                    await self._tick()
                except Exception:
                    logger.exception("SchedulerEngine: error in tick (continuing)")
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("SchedulerEngine: stopped")
            raise

    async def _tick(self) -> None:
        """Single poll: find due tasks, dispatch each, update DB."""
        now = datetime.now(timezone.utc)
        due_tasks = await self._db.list_due(now)
        if not due_tasks:
            return
        logger.info("SchedulerEngine: %d task(s) due at %s", len(due_tasks), now.isoformat())
        for sched_task in due_tasks:
            overdue_seconds = max(0.0, (now - sched_task.next_run_at).total_seconds())
            grace_seconds = self._overdue_grace_minutes * 60

            # Skip overdue tasks that exceed the grace period (grace >= 0)
            if self._overdue_grace_minutes >= 0 and overdue_seconds > grace_seconds:
                next_run = compute_next_run(sched_task)
                new_status = "done" if next_run is None else "active"
                if sched_task.max_runs is not None and (sched_task.run_count + 1) >= sched_task.max_runs:
                    new_status = "done"
                    next_run = None
                await self._db.update_after_run(
                    sched_task.id,
                    next_run_at=next_run,
                    new_status=new_status,
                )
                logger.info(
                    "SchedulerEngine: skipped overdue task %s (overdue=%.0fs, grace=%ds)",
                    sched_task.id, overdue_seconds, grace_seconds,
                )
                continue

            await self._fire(sched_task, now, overdue_seconds=overdue_seconds)

    async def _fire(
        self,
        sched_task: ScheduledTask,
        fired_at: datetime,
        overdue_seconds: float = 0.0,
    ) -> None:
        """Dispatch one scheduled task and update the DB."""
        from ..protocol.types import Reply, ReplyType, Task
        import json as _json

        start = time.monotonic()
        success = False
        error_msg: str | None = None

        try:
            replier = self._feishu_client.get_replier()

            # Overdue notice prepended to first reply when task fired late
            overdue_notice: str | None = None
            if overdue_seconds > 0:
                overdue_minutes = int(overdue_seconds // 60) or 1
                overdue_notice = f"⏰ 定时任务延迟 {overdue_minutes} 分钟执行"

            first_reply_sent = False
            notify_chat = sched_task.notify_chat

            async def reply_fn(reply: Reply) -> None:
                nonlocal first_reply_sent
                try:
                    content = reply.content or ""
                    is_first = not first_reply_sent
                    first_reply_sent = True
                    if notify_chat:
                        # Broadcast to group chat
                        if reply.type == ReplyType.CARD:
                            if overdue_notice and is_first:
                                await replier.send_text(sched_task.chat_id, overdue_notice)
                            await replier.send_card(sched_task.chat_id, content)
                        else:
                            if overdue_notice and is_first:
                                content = f"{overdue_notice}\n\n{content}"
                            await replier.send_text(sched_task.chat_id, content)
                    else:
                        # Send to creator's DM via open_id
                        if reply.type == ReplyType.CARD:
                            if overdue_notice and is_first:
                                await replier.send_to_user(
                                    sched_task.creator_open_id,
                                    _json.dumps({"text": overdue_notice}),
                                    msg_type="text",
                                )
                            await replier.send_to_user(sched_task.creator_open_id, content)
                        else:
                            if overdue_notice and is_first:
                                content = f"{overdue_notice}\n\n{content}"
                            await replier.send_to_user(
                                sched_task.creator_open_id,
                                _json.dumps({"text": content}),
                                msg_type="text",
                            )
                except Exception:
                    logger.exception("SchedulerEngine: reply_fn failed for task %s", sched_task.id)

            # Wrap prompt with execution context so the agent treats this as
            # a scheduled notification to deliver, not a new user request to set up.
            fired_content = (
                f"[定时任务触发] 请直接执行以下操作，不要重新设置或确认计划：\n{sched_task.prompt}"
            )
            dispatched_task = Task(
                id=str(uuid.uuid4()),
                content=fired_content,
                session_id=sched_task.session_id,
                reply_fn=reply_fn,
                chat_type="group" if notify_chat else "p2p",
                user_id=sched_task.creator_open_id,
            )
            await self._dispatcher.dispatch(dispatched_task)
            success = True

        except Exception as exc:
            error_msg = str(exc)
            logger.exception("SchedulerEngine: failed to fire task %s", sched_task.id)

        finally:
            duration = time.monotonic() - start
            next_run = compute_next_run(sched_task)
            new_status = "done" if next_run is None else "active"

            # Enforce max_runs limit
            if sched_task.max_runs is not None and (sched_task.run_count + 1) >= sched_task.max_runs:
                new_status = "done"
                next_run = None

            await self._db.update_after_run(
                sched_task.id,
                next_run_at=next_run,
                new_status=new_status,
            )
            await self._db.add_run_log(
                TaskRunLog(
                    task_id=sched_task.id,
                    run_at=fired_at,
                    success=success,
                    error_message=error_msg,
                    duration_seconds=duration,
                )
            )
