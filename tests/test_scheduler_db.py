"""Unit tests for nextme.scheduler.db (SchedulerDb)."""
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from nextme.scheduler.db import SchedulerDb
from nextme.scheduler.schema import ScheduledTask, ScheduleType, TaskRunLog


@pytest.fixture
async def db(tmp_path):
    d = SchedulerDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


def _make_task(task_id="t1", chat_id="oc_x", prompt="hello") -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        chat_id=chat_id,
        creator_open_id="ou_y",
        prompt=prompt,
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-03-20T10:00:00+08:00",
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
    )


async def test_create_and_get(db):
    task = _make_task()
    await db.create(task)
    fetched = await db.get(task.id)
    assert fetched is not None
    assert fetched.prompt == "hello"
    assert fetched.session_id == "oc_x:ou_y"


async def test_list_due_returns_overdue(db):
    task = _make_task()
    await db.create(task)
    # now is after next_run_at
    now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=timezone.utc)
    due = await db.list_due(now)
    assert len(due) == 1


async def test_list_due_not_yet(db):
    task = _make_task()
    await db.create(task)
    # now is before next_run_at
    now = datetime(2026, 3, 19, 0, 0, 0, tzinfo=timezone.utc)
    due = await db.list_due(now)
    assert len(due) == 0


async def test_update_after_run_marks_done(db):
    task = _make_task()
    await db.create(task)
    await db.update_after_run(task.id, next_run_at=None, new_status="done")
    fetched = await db.get(task.id)
    assert fetched.status == "done"
    assert fetched.run_count == 1
    assert fetched.last_run_at is not None


async def test_update_after_run_interval(db):
    task = _make_task()
    await db.create(task)
    next_run = datetime(2026, 3, 21, 2, 0, 0, tzinfo=timezone.utc)
    await db.update_after_run(task.id, next_run_at=next_run, new_status="active")
    fetched = await db.get(task.id)
    assert fetched.status == "active"
    assert fetched.run_count == 1


async def test_list_by_chat(db):
    await db.create(_make_task("t1", "oc_x"))
    await db.create(_make_task("t2", "oc_x"))
    await db.create(_make_task("t3", "oc_y"))
    tasks = await db.list_by_chat("oc_x")
    assert len(tasks) == 2


async def test_update_status_pause(db):
    task = _make_task()
    await db.create(task)
    await db.update_status(task.id, "paused")
    fetched = await db.get(task.id)
    assert fetched.status == "paused"
    # Paused task should not appear in list_due
    now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=timezone.utc)
    due = await db.list_due(now)
    assert len(due) == 0


async def test_delete(db):
    task = _make_task()
    await db.create(task)
    await db.delete(task.id)
    fetched = await db.get(task.id)
    assert fetched is None


async def test_add_and_get_run_logs(db):
    task = _make_task()
    await db.create(task)
    log = TaskRunLog(task_id=task.id, run_at=datetime.now(timezone.utc), success=True, duration_seconds=1.5)
    await db.add_run_log(log)
    logs = await db.get_run_logs(task.id)
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].duration_seconds == 1.5


async def test_get_run_logs_order(db):
    task = _make_task()
    await db.create(task)
    t1 = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 11, 11, 0, 0, tzinfo=timezone.utc)
    await db.add_run_log(TaskRunLog(task_id=task.id, run_at=t1, success=True))
    await db.add_run_log(TaskRunLog(task_id=task.id, run_at=t2, success=False, error_message="boom"))
    logs = await db.get_run_logs(task.id, limit=10)
    # Most recent first
    assert logs[0].run_at >= logs[1].run_at
