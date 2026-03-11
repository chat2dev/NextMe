"""SQLite CRUD for scheduled_tasks and task_run_logs tables in nextme.db."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .schema import ScheduledTask, ScheduleType, TaskRunLog

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path("~/.nextme").expanduser() / "nextme.db"

_CREATE_SCHEDULED_TASKS = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id               TEXT PRIMARY KEY,
    chat_id          TEXT NOT NULL,
    creator_open_id  TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    schedule_type    TEXT NOT NULL CHECK(schedule_type IN ('once', 'interval', 'cron')),
    schedule_value   TEXT NOT NULL,
    next_run_at      TEXT,
    last_run_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active', 'paused', 'done')),
    run_count        INTEGER NOT NULL DEFAULT 0,
    max_runs         INTEGER,
    created_at       TEXT NOT NULL,
    project_name     TEXT
)
"""

_CREATE_TASK_RUN_LOGS = """
CREATE TABLE IF NOT EXISTS task_run_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    run_at           TEXT NOT NULL,
    success          INTEGER NOT NULL,
    error_message    TEXT,
    duration_seconds REAL
)
"""


def _row_to_task(row: aiosqlite.Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        chat_id=row["chat_id"],
        creator_open_id=row["creator_open_id"],
        prompt=row["prompt"],
        schedule_type=ScheduleType(row["schedule_type"]),
        schedule_value=row["schedule_value"],
        next_run_at=_parse_dt(row["next_run_at"]) or datetime.now(timezone.utc),
        last_run_at=_parse_dt(row["last_run_at"]),
        status=row["status"],
        run_count=row["run_count"],
        max_runs=row["max_runs"],
        created_at=_parse_dt(row["created_at"]),
        project_name=row["project_name"],
    )


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_log(row: aiosqlite.Row) -> TaskRunLog:
    return TaskRunLog(
        id=row["id"],
        task_id=row["task_id"],
        run_at=_parse_dt(row["run_at"]) or datetime.now(timezone.utc),
        success=bool(row["success"]),
        error_message=row["error_message"],
        duration_seconds=row["duration_seconds"],
    )


class SchedulerDb:
    """Async SQLite data layer for scheduler tables."""

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        async with self._lock:
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute(_CREATE_SCHEDULED_TASKS)
            await self._conn.execute(_CREATE_TASK_RUN_LOGS)
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create(self, task: ScheduledTask) -> None:
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO scheduled_tasks
                   (id, chat_id, creator_open_id, prompt, schedule_type, schedule_value,
                    next_run_at, status, run_count, max_runs, created_at, project_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.chat_id,
                    task.creator_open_id,
                    task.prompt,
                    task.schedule_type.value,
                    task.schedule_value,
                    task.next_run_at.isoformat() if task.next_run_at else None,
                    task.status,
                    task.run_count,
                    task.max_runs,
                    task.created_at.isoformat() if task.created_at else datetime.now(timezone.utc).isoformat(),
                    task.project_name,
                ),
            )
            await self._conn.commit()

    async def get(self, task_id: str) -> ScheduledTask | None:
        async with self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def list_due(self, now: datetime) -> list[ScheduledTask]:
        """Return active tasks whose next_run_at <= now."""
        now_iso = now.isoformat()
        async with self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE status = 'active' AND next_run_at <= ?",
            (now_iso,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def list_by_chat(self, chat_id: str) -> list[ScheduledTask]:
        async with self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE chat_id = ? ORDER BY created_at DESC",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def update_after_run(
        self,
        task_id: str,
        next_run_at: datetime | None,
        new_status: str = "active",
    ) -> None:
        async with self._lock:
            await self._conn.execute(
                """UPDATE scheduled_tasks
                   SET next_run_at = ?, last_run_at = ?, status = ?, run_count = run_count + 1
                   WHERE id = ?""",
                (
                    next_run_at.isoformat() if next_run_at else None,
                    datetime.now(timezone.utc).isoformat(),
                    new_status,
                    task_id,
                ),
            )
            await self._conn.commit()

    async def update_status(self, task_id: str, status: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE scheduled_tasks SET status = ? WHERE id = ?",
                (status, task_id),
            )
            await self._conn.commit()

    async def delete(self, task_id: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
            )
            await self._conn.commit()

    async def add_run_log(self, log: TaskRunLog) -> None:
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO task_run_logs (task_id, run_at, success, error_message, duration_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    log.task_id,
                    log.run_at.isoformat(),
                    int(log.success),
                    log.error_message,
                    log.duration_seconds,
                ),
            )
            await self._conn.commit()

    async def get_run_logs(self, task_id: str, limit: int = 10) -> list[TaskRunLog]:
        async with self._conn.execute(
            "SELECT * FROM task_run_logs WHERE task_id = ? ORDER BY run_at DESC LIMIT ?",
            (task_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_log(r) for r in rows]
