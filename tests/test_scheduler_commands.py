"""Unit tests for nextme.scheduler.commands."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone
from nextme.scheduler.commands import parse_schedule_command, ParsedSchedule, handle_schedule
from nextme.scheduler.schema import ScheduledTask, ScheduleType


# -- parse_schedule_command tests --

def test_parse_at_datetime():
    result = parse_schedule_command("say hello at 2026-03-20T10:00:00+08:00")
    assert result is not None
    assert result.action == "create"
    assert result.prompt == "say hello"
    assert result.schedule_type == "once"
    assert result.next_run_at is not None

def test_parse_every_hours():
    result = parse_schedule_command("check news every 2h")
    assert result is not None
    assert result.schedule_type == "interval"
    assert result.schedule_value == "7200"

def test_parse_every_minutes():
    result = parse_schedule_command("ping me every 30m")
    assert result is not None
    assert result.schedule_value == "1800"

def test_parse_every_seconds():
    result = parse_schedule_command("ping every 10s")
    assert result.schedule_value == "10"

def test_parse_every_days():
    result = parse_schedule_command("daily report every 1d")
    assert result.schedule_value == "86400"

def test_parse_cron():
    result = parse_schedule_command("daily report cron 0 9 * * *")
    assert result is not None
    assert result.schedule_type == "cron"
    assert result.schedule_value == "0 9 * * *"
    assert result.prompt == "daily report"

def test_parse_list():
    result = parse_schedule_command("list")
    assert result is not None
    assert result.action == "list"

def test_parse_pause():
    result = parse_schedule_command("pause abc12345")
    assert result is not None
    assert result.action == "pause"
    assert result.target_id == "abc12345"

def test_parse_resume():
    result = parse_schedule_command("resume abc12345")
    assert result.action == "resume"
    assert result.target_id == "abc12345"

def test_parse_delete():
    result = parse_schedule_command("delete abc12345")
    assert result.action == "delete"
    assert result.target_id == "abc12345"

def test_parse_invalid_returns_none():
    assert parse_schedule_command("gibberish xyz") is None

def test_parse_at_invalid_datetime():
    assert parse_schedule_command("do thing at not-a-date") is None

def test_parse_bad_cron_returns_none():
    assert parse_schedule_command("task cron not-valid-cron") is None


# -- handle_schedule tests --

@pytest.fixture
def replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    return r


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.create = AsyncMock()
    db.list_by_chat = AsyncMock(return_value=[])
    db.get = AsyncMock(return_value=None)
    db.update_status = AsyncMock()
    db.delete = AsyncMock()
    return db


async def test_handle_create_once(replier, mock_db):
    await handle_schedule(
        raw_args="say hi at 2026-03-20T10:00:00+08:00",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    mock_db.create.assert_called_once()
    replier.send_text.assert_called_once()
    assert "已创建" in replier.send_text.call_args[0][1]


async def test_handle_create_interval(replier, mock_db):
    await handle_schedule(
        raw_args="ping every 1h",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    mock_db.create.assert_called_once()
    task = mock_db.create.call_args[0][0]
    assert task.schedule_type == ScheduleType.INTERVAL
    assert task.schedule_value == "3600"


async def test_handle_list_empty(replier, mock_db):
    await handle_schedule(
        raw_args="list",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    replier.send_text.assert_called_once()
    assert "没有" in replier.send_text.call_args[0][1]


async def test_handle_list_with_tasks(replier, mock_db):
    from datetime import timedelta
    task = ScheduledTask(
        id="abc12345-uuid",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="check news",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value="3600",
        next_run_at=datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc),
    )
    mock_db.list_by_chat = AsyncMock(return_value=[task])
    await handle_schedule(
        raw_args="list",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    text = replier.send_text.call_args[0][1]
    assert "abc12345" in text
    assert "1小时" in text       # interval human-readable
    assert "UTC" not in text    # local timezone, not raw UTC label


async def test_handle_pause(replier, mock_db):
    task = ScheduledTask(
        id="abc12345-full-uuid",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="p",
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-03-20T10:00:00+08:00",
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
    )
    mock_db.list_by_chat = AsyncMock(return_value=[task])
    await handle_schedule(
        raw_args="pause abc12345",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    mock_db.update_status.assert_called_once_with("abc12345-full-uuid", "paused")
    assert "已暂停" in replier.send_text.call_args[0][1]


async def test_handle_resume(replier, mock_db):
    task = ScheduledTask(
        id="abc12345-full-uuid",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="p",
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-03-20T10:00:00+08:00",
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
        status="paused",
    )
    mock_db.list_by_chat = AsyncMock(return_value=[task])
    await handle_schedule(
        raw_args="resume abc12345",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    mock_db.update_status.assert_called_once_with("abc12345-full-uuid", "active")
    assert "已恢复" in replier.send_text.call_args[0][1]


async def test_handle_delete(replier, mock_db):
    task = ScheduledTask(
        id="abc12345-full-uuid",
        chat_id="oc_x",
        creator_open_id="ou_y",
        prompt="p",
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-03-20T10:00:00+08:00",
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
    )
    mock_db.list_by_chat = AsyncMock(return_value=[task])
    await handle_schedule(
        raw_args="delete abc12345",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    mock_db.delete.assert_called_once_with("abc12345-full-uuid")
    assert "已删除" in replier.send_text.call_args[0][1]


async def test_handle_not_found(replier, mock_db):
    mock_db.list_by_chat = AsyncMock(return_value=[])
    await handle_schedule(
        raw_args="pause xyz99999",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    assert "未找到" in replier.send_text.call_args[0][1]


async def test_handle_invalid_args_shows_usage(replier, mock_db):
    await handle_schedule(
        raw_args="nonsense input xyz",
        chat_id="oc_x",
        creator_open_id="ou_y",
        replier=replier,
        db=mock_db,
    )
    replier.send_text.assert_called_once()
    text = replier.send_text.call_args[0][1]
    assert "schedule" in text.lower() or "用法" in text


# -- Chinese natural language parse tests --

def test_parse_chinese_every_hour():
    result = parse_schedule_command("每小时提醒我喝水")
    assert result is not None
    assert result.action == "create"
    assert result.prompt == "提醒我喝水"
    assert result.schedule_type == "interval"
    assert result.schedule_value == "3600"

def test_parse_chinese_every_n_hours():
    result = parse_schedule_command("每2小时检查新闻")
    assert result is not None
    assert result.schedule_value == "7200"
    assert result.prompt == "检查新闻"

def test_parse_chinese_every_n_minutes():
    result = parse_schedule_command("每30分钟ping")
    assert result is not None
    assert result.schedule_value == "1800"
    assert result.prompt == "ping"

def test_parse_chinese_every_day():
    result = parse_schedule_command("每天发日报")
    assert result is not None
    assert result.schedule_type == "interval"
    assert result.schedule_value == "86400"
    assert result.prompt == "发日报"

def test_parse_chinese_suffix_interval():
    result = parse_schedule_command("提醒我喝水每小时")
    assert result is not None
    assert result.prompt == "提醒我喝水"
    assert result.schedule_value == "3600"

def test_parse_chinese_daily_at():
    result = parse_schedule_command("每天9点发日报")
    assert result is not None
    assert result.schedule_type == "cron"
    assert result.schedule_value == "0 9 * * *"
    assert result.prompt == "发日报"

def test_parse_chinese_daily_at_prefix_with_early_morning():
    # "早上" is part of the hour spec — handle gracefully
    # This might not parse due to "早上" but ensure no crash
    # (could be None if pattern doesn't match)
    _ = parse_schedule_command("每天早上8点提醒我")

def test_parse_chinese_future_once():
    result = parse_schedule_command("30分钟后提醒我开会")
    assert result is not None
    assert result.schedule_type == "once"
    assert result.prompt == "提醒我开会"
    # next_run_at should be ~30 minutes from now
    now = datetime.now(timezone.utc)
    delta = (result.next_run_at - now).total_seconds()
    assert 1700 < delta < 1900  # 30 min ± small tolerance

def test_parse_chinese_future_hours():
    result = parse_schedule_command("2小时后检查结果")
    assert result is not None
    assert result.schedule_type == "once"
    assert result.prompt == "检查结果"

def test_parse_chinese_suffix_daily_at():
    result = parse_schedule_command("发日报每天9点")
    assert result is not None
    assert result.schedule_type == "cron"
    assert result.schedule_value == "0 9 * * *"
    assert result.prompt == "发日报"

def test_parse_chinese_future_suffix():
    result = parse_schedule_command("提醒我开会2小时后")
    assert result is not None
    assert result.schedule_type == "once"
    assert result.prompt == "提醒我开会"

def test_parse_chinese_daily_at_invalid_hour_returns_none():
    assert parse_schedule_command("每天25点发日报") is None
