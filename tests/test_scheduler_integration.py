"""Integration test: SchedulerDb + SchedulerEngine end-to-end."""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from nextme.scheduler.db import SchedulerDb
from nextme.scheduler.schema import ScheduledTask, ScheduleType
from nextme.scheduler.engine import SchedulerEngine


@pytest.fixture
async def db(tmp_path):
    d = SchedulerDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


async def test_engine_fires_due_task_e2e(db):
    """Full stack: create task in DB, run tick, verify dispatch called."""
    dispatched = []

    class FakeDispatcher:
        async def dispatch(self, task):
            dispatched.append(task)

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=AsyncMock())

    engine = SchedulerEngine(
        db=db,
        dispatcher=FakeDispatcher(),
        feishu_client=feishu_client,
    )

    # Create a task that's already due (past time)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    task = ScheduledTask(
        id="e2e-001",
        chat_id="oc_test",
        creator_open_id="ou_test",
        prompt="ping",
        schedule_type=ScheduleType.ONCE,
        schedule_value=past.isoformat(),
        next_run_at=past,
    )
    await db.create(task)
    await engine._tick()

    assert len(dispatched) == 1
    assert dispatched[0].content == "ping"
    assert dispatched[0].session_id == "oc_test:ou_test"

    # Should be marked done
    updated = await db.get("e2e-001")
    assert updated is not None
    assert updated.status == "done"
    assert updated.run_count == 1


async def test_engine_interval_task_stays_active(db):
    """Interval task should remain active with updated next_run_at after fire."""
    dispatched = []

    class FakeDispatcher:
        async def dispatch(self, task):
            dispatched.append(task)

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=AsyncMock())

    engine = SchedulerEngine(db=db, dispatcher=FakeDispatcher(), feishu_client=feishu_client)

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    task = ScheduledTask(
        id="e2e-002",
        chat_id="oc_test",
        creator_open_id="ou_test",
        prompt="check",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value="3600",  # every hour
        next_run_at=past,
    )
    await db.create(task)
    await engine._tick()

    assert len(dispatched) == 1
    updated = await db.get("e2e-002")
    assert updated is not None
    assert updated.status == "active"
    assert updated.next_run_at is not None
    assert updated.next_run_at > past


async def test_paused_task_not_fired(db):
    """Paused tasks should not be picked up by the scheduler."""
    dispatched = []

    class FakeDispatcher:
        async def dispatch(self, task):
            dispatched.append(task)

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=AsyncMock())

    engine = SchedulerEngine(db=db, dispatcher=FakeDispatcher(), feishu_client=feishu_client)

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    task = ScheduledTask(
        id="e2e-003",
        chat_id="oc_test",
        creator_open_id="ou_test",
        prompt="ping",
        schedule_type=ScheduleType.ONCE,
        schedule_value=past.isoformat(),
        next_run_at=past,
        status="paused",
    )
    await db.create(task)
    await engine._tick()

    assert len(dispatched) == 0
