"""Unit tests for nextme.scheduler.engine."""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.scheduler.engine import SchedulerEngine, compute_next_run
from nextme.scheduler.schema import ScheduledTask, ScheduleType


def _make_task(schedule_type=ScheduleType.ONCE, schedule_value="2026-03-20T10:00:00+08:00", **kw):
    return ScheduledTask(
        id="t1",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="hello",
        schedule_type=schedule_type,
        schedule_value=schedule_value,
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
        **kw,
    )


# -- compute_next_run tests --

def test_compute_next_run_once_returns_none():
    task = _make_task()
    assert compute_next_run(task) is None


def test_compute_next_run_interval():
    task = _make_task(schedule_type=ScheduleType.INTERVAL, schedule_value="3600")
    now = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)
    result = compute_next_run(task, reference=now)
    assert result is not None
    delta = (result - now).total_seconds()
    assert 3590 < delta < 3610


def test_compute_next_run_cron():
    task = _make_task(schedule_type=ScheduleType.CRON, schedule_value="0 9 * * *")
    reference = datetime(2026, 3, 11, 8, 0, 0, tzinfo=timezone.utc)
    result = compute_next_run(task, reference=reference)
    assert result is not None
    assert result.hour == 9
    assert result > reference


def test_compute_next_run_bad_cron_returns_none():
    task = _make_task(schedule_type=ScheduleType.CRON, schedule_value="not-a-cron")
    result = compute_next_run(task)
    assert result is None


def test_compute_next_run_max_runs_respected():
    """max_runs logic is handled in _fire, not compute_next_run — interval still returns value."""
    task = _make_task(schedule_type=ScheduleType.INTERVAL, schedule_value="60", max_runs=3, run_count=2)
    now = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)
    # compute_next_run itself doesn't check max_runs; engine._fire does
    result = compute_next_run(task, reference=now)
    assert result is not None


# -- SchedulerEngine tests --

@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.list_due = AsyncMock(return_value=[])
    db.update_after_run = AsyncMock()
    db.add_run_log = AsyncMock()
    return db


@pytest.fixture
def mock_dispatcher():
    d = AsyncMock()
    d.dispatch = AsyncMock()
    return d


@pytest.fixture
def mock_replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock()
    r.send_to_user = AsyncMock()
    return r


@pytest.fixture
def mock_feishu_client(mock_replier):
    client = MagicMock()
    client.get_replier = MagicMock(return_value=mock_replier)
    return client


@pytest.fixture
def engine(mock_db, mock_dispatcher, mock_feishu_client):
    return SchedulerEngine(
        db=mock_db,
        dispatcher=mock_dispatcher,
        feishu_client=mock_feishu_client,
        poll_interval=0.01,
    )


async def test_tick_no_due_tasks_no_dispatch(engine, mock_db, mock_dispatcher):
    mock_db.list_due = AsyncMock(return_value=[])
    await engine._tick()
    mock_dispatcher.dispatch.assert_not_called()


async def test_tick_fires_due_task(engine, mock_db, mock_dispatcher, mock_feishu_client):
    task = _make_task(schedule_type=ScheduleType.ONCE)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()
    mock_dispatcher.dispatch.assert_called_once()
    mock_db.update_after_run.assert_called_once()
    # Verify get_replier called with no arguments
    mock_feishu_client.get_replier.assert_called_with()  # no args


async def test_once_task_marked_done(engine, mock_db):
    task = _make_task(schedule_type=ScheduleType.ONCE)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()
    call_kwargs = mock_db.update_after_run.call_args
    assert call_kwargs.kwargs["new_status"] == "done"


async def test_interval_task_gets_next_run(engine, mock_db):
    task = _make_task(schedule_type=ScheduleType.INTERVAL, schedule_value="3600")
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()
    call_kwargs = mock_db.update_after_run.call_args
    assert call_kwargs is not None
    assert call_kwargs.kwargs["new_status"] == "active"
    assert call_kwargs.kwargs["next_run_at"] is not None


async def test_max_runs_marks_done(engine, mock_db):
    """Task with max_runs=1 and run_count=0 should be marked done after one run."""
    task = _make_task(schedule_type=ScheduleType.INTERVAL, schedule_value="60", max_runs=1, run_count=0)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()
    call_kwargs = mock_db.update_after_run.call_args
    assert "done" in str(call_kwargs)


async def test_dispatch_exception_still_updates_db(engine, mock_db, mock_dispatcher):
    """Even if dispatch raises, DB is updated (run logged as failure)."""
    task = _make_task(schedule_type=ScheduleType.ONCE)
    mock_db.list_due = AsyncMock(return_value=[task])
    mock_dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("dispatch failed"))
    await engine._tick()
    mock_db.update_after_run.assert_called_once()
    mock_db.add_run_log.assert_called_once()
    log = mock_db.add_run_log.call_args[0][0]
    assert log.success is False
    assert "dispatch failed" in (log.error_message or "")


async def test_run_loop_stops_on_cancel(engine):
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No exception leaked


async def test_fire_reply_fn_sends_text(engine, mock_db, mock_dispatcher, mock_replier):
    """notify_chat=True → text reply sent to group via send_text."""
    from nextme.protocol.types import Reply, ReplyType

    task = _make_task(schedule_type=ScheduleType.ONCE, notify_chat=True)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()

    # Retrieve the Task object that was passed to dispatcher.dispatch()
    dispatched: "Task" = mock_dispatcher.dispatch.call_args[0][0]
    reply = Reply(type=ReplyType.MARKDOWN, content="hello world")
    await dispatched.reply_fn(reply)

    mock_replier.send_text.assert_called_once_with(task.chat_id, "hello world")
    mock_replier.send_card.assert_not_called()


async def test_fire_reply_fn_sends_card(engine, mock_db, mock_dispatcher, mock_replier):
    """notify_chat=True → card reply sent to group via send_card."""
    from nextme.protocol.types import Reply, ReplyType

    task = _make_task(schedule_type=ScheduleType.ONCE, notify_chat=True)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()

    dispatched = mock_dispatcher.dispatch.call_args[0][0]
    reply = Reply(type=ReplyType.CARD, content='{"key": "value"}')
    await dispatched.reply_fn(reply)

    mock_replier.send_card.assert_called_once_with(task.chat_id, '{"key": "value"}')
    mock_replier.send_text.assert_not_called()


async def test_fire_reply_fn_suppresses_replier_exception(engine, mock_db, mock_dispatcher, mock_replier):
    """reply_fn swallows exceptions from the replier without propagating."""
    from nextme.protocol.types import Reply, ReplyType

    task = _make_task(schedule_type=ScheduleType.ONCE)
    mock_db.list_due = AsyncMock(return_value=[task])
    await engine._tick()

    mock_replier.send_to_user = AsyncMock(side_effect=RuntimeError("network error"))
    dispatched = mock_dispatcher.dispatch.call_args[0][0]
    reply = Reply(type=ReplyType.MARKDOWN, content="hi")

    # Must not raise
    await dispatched.reply_fn(reply)


async def test_run_tick_error_is_suppressed(engine, mock_db, mock_dispatcher):
    """Exceptions raised by _tick are suppressed; the loop continues ticking."""
    call_count = 0

    async def flaky_tick() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("tick failed")

    engine._tick = flaky_tick
    engine._poll_interval = 0.01

    run_task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.08)  # enough time for 3+ ticks at 10ms interval
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2  # loop continued after the first-tick error


# -- notify_chat (recipient routing) tests --

async def test_non_notify_chat_sends_to_user_dm(mock_db, mock_dispatcher, mock_feishu_client):
    """notify_chat=False → reply sent to creator DM via send_to_user."""
    from nextme.protocol.types import Reply, ReplyType

    eng = SchedulerEngine(db=mock_db, dispatcher=mock_dispatcher, feishu_client=mock_feishu_client)
    task = _make_task(schedule_type=ScheduleType.ONCE)
    # notify_chat=False is default
    mock_db.list_due = AsyncMock(return_value=[task])
    await eng._tick()

    dispatched = mock_dispatcher.dispatch.call_args[0][0]
    mock_replier = mock_feishu_client.get_replier()
    await dispatched.reply_fn(Reply(type=ReplyType.MARKDOWN, content="喝水啦！"))

    mock_replier.send_to_user.assert_called_once()
    call_args = mock_replier.send_to_user.call_args
    assert call_args[0][0] == task.creator_open_id
    import json as _json
    payload = _json.loads(call_args[0][1])
    assert "喝水啦！" in payload["text"]
    mock_replier.send_text.assert_not_called()


async def test_notify_chat_sends_to_group(mock_db, mock_dispatcher, mock_feishu_client):
    """notify_chat=True → reply sent to group chat_id via send_text."""
    from nextme.protocol.types import Reply, ReplyType

    eng = SchedulerEngine(db=mock_db, dispatcher=mock_dispatcher, feishu_client=mock_feishu_client)
    task = _make_task(schedule_type=ScheduleType.ONCE, notify_chat=True)
    mock_db.list_due = AsyncMock(return_value=[task])
    await eng._tick()

    dispatched = mock_dispatcher.dispatch.call_args[0][0]
    mock_replier = mock_feishu_client.get_replier()
    await dispatched.reply_fn(Reply(type=ReplyType.MARKDOWN, content="群通知！"))

    mock_replier.send_text.assert_called_once_with(task.chat_id, "群通知！")
    mock_replier.send_to_user.assert_not_called()


async def test_notify_chat_false_card_sends_to_user_dm(mock_db, mock_dispatcher, mock_feishu_client):
    """notify_chat=False + card reply → card sent via send_to_user with open_id."""
    from nextme.protocol.types import Reply, ReplyType

    eng = SchedulerEngine(db=mock_db, dispatcher=mock_dispatcher, feishu_client=mock_feishu_client)
    task = _make_task(schedule_type=ScheduleType.ONCE)
    # notify_chat=False is default
    mock_db.list_due = AsyncMock(return_value=[task])
    await eng._tick()

    dispatched = mock_dispatcher.dispatch.call_args[0][0]
    mock_replier = mock_feishu_client.get_replier()
    await dispatched.reply_fn(Reply(type=ReplyType.CARD, content='{"card":"data"}'))

    mock_replier.send_to_user.assert_called_once_with(task.creator_open_id, '{"card":"data"}')
    mock_replier.send_card.assert_not_called()


# -- _is_group_notify tests --

def test_is_group_notify_group_with_keyword():
    from nextme.scheduler.commands import _is_group_notify
    assert _is_group_notify("每天早上发一条群提醒", "group") is True
    assert _is_group_notify("群通知每周一", "group") is True
    assert _is_group_notify("通知大家喝水", "group") is True


def test_is_group_notify_p2p_ignores_keywords():
    from nextme.scheduler.commands import _is_group_notify
    assert _is_group_notify("群提醒", "p2p") is False


def test_is_group_notify_group_without_keyword():
    from nextme.scheduler.commands import _is_group_notify
    assert _is_group_notify("每小时提醒我喝水", "group") is False


# -- Overdue grace tests --

def _make_overdue_task(overdue_minutes: float, **kw):
    from datetime import timedelta
    next_run = datetime.now(timezone.utc) - timedelta(minutes=overdue_minutes)
    return ScheduledTask(
        id="t_overdue",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="remind",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value="3600",
        next_run_at=next_run,
        **kw,
    )


def _make_engine_with_grace(grace_minutes, mock_db, mock_dispatcher, mock_feishu_client):
    return SchedulerEngine(
        db=mock_db, dispatcher=mock_dispatcher,
        feishu_client=mock_feishu_client, poll_interval=0.01,
        overdue_grace_minutes=grace_minutes,
    )


async def test_overdue_task_skipped_when_exceeds_grace(mock_db, mock_dispatcher, mock_feishu_client):
    eng = _make_engine_with_grace(5, mock_db, mock_dispatcher, mock_feishu_client)
    mock_db.list_due = AsyncMock(return_value=[_make_overdue_task(10)])
    await eng._tick()
    mock_dispatcher.dispatch.assert_not_called()
    mock_db.update_after_run.assert_called_once()


async def test_overdue_task_fires_within_grace(mock_db, mock_dispatcher, mock_feishu_client):
    eng = _make_engine_with_grace(5, mock_db, mock_dispatcher, mock_feishu_client)
    mock_db.list_due = AsyncMock(return_value=[_make_overdue_task(2)])
    await eng._tick()
    mock_dispatcher.dispatch.assert_called_once()


async def test_grace_minus_one_fires_all_overdue(mock_db, mock_dispatcher, mock_feishu_client):
    eng = _make_engine_with_grace(-1, mock_db, mock_dispatcher, mock_feishu_client)
    mock_db.list_due = AsyncMock(return_value=[_make_overdue_task(120)])
    await eng._tick()
    mock_dispatcher.dispatch.assert_called_once()


async def test_grace_zero_skips_any_overdue(mock_db, mock_dispatcher, mock_feishu_client):
    eng = _make_engine_with_grace(0, mock_db, mock_dispatcher, mock_feishu_client)
    mock_db.list_due = AsyncMock(return_value=[_make_overdue_task(0.1)])
    await eng._tick()
    mock_dispatcher.dispatch.assert_not_called()
