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
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher
        self._feishu_client = feishu_client
        self._poll_interval = poll_interval

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
            await self._fire(sched_task, now)

    async def _fire(self, sched_task: ScheduledTask, fired_at: datetime) -> None:
        """Dispatch one scheduled task and update the DB."""
        from ..protocol.types import Reply, ReplyType, Task

        start = time.monotonic()
        success = False
        error_msg: str | None = None

        try:
            replier = self._feishu_client.get_replier(sched_task.chat_id)

            async def reply_fn(reply: Reply) -> None:
                try:
                    if reply.type == ReplyType.CARD:
                        await replier.send_card(sched_task.chat_id, reply.content or "")
                    else:
                        await replier.send_text(sched_task.chat_id, reply.content or "")
                except Exception:
                    logger.exception("SchedulerEngine: reply_fn failed for task %s", sched_task.id)

            dispatched_task = Task(
                id=str(uuid.uuid4()),
                content=sched_task.prompt,
                session_id=sched_task.session_id,
                reply_fn=reply_fn,
                chat_type="p2p",
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
