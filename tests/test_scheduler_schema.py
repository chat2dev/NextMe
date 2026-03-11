from nextme.scheduler.schema import ScheduledTask, ScheduleType, TaskRunLog
from datetime import datetime, timezone


def test_scheduled_task_defaults():
    t = ScheduledTask(
        id="abc",
        chat_id="oc_xxx",
        creator_open_id="ou_yyy",
        prompt="say hello",
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-03-20T10:00:00+08:00",
        next_run_at=datetime(2026, 3, 20, 2, 0, 0, tzinfo=timezone.utc),
    )
    assert t.status == "active"
    assert t.run_count == 0
    assert t.session_id == "oc_xxx:ou_yyy"


def test_interval_task():
    t = ScheduledTask(
        id="def",
        chat_id="oc_xxx",
        creator_open_id="ou_yyy",
        prompt="check news",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value="3600",
        next_run_at=datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert t.schedule_type == ScheduleType.INTERVAL


def test_run_log():
    log = TaskRunLog(task_id="abc", run_at=datetime.now(timezone.utc), success=True)
    assert log.error_message is None


def test_created_at_auto_populated():
    t = ScheduledTask(
        id="ghi",
        chat_id="oc_aaa",
        creator_open_id="ou_bbb",
        prompt="ping",
        schedule_type=ScheduleType.ONCE,
        schedule_value="2026-04-01T00:00:00+00:00",
        next_run_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert t.created_at is not None
    assert t.created_at.tzinfo is not None


def test_session_id_varies_with_inputs():
    def make(chat_id: str, creator_open_id: str) -> ScheduledTask:
        return ScheduledTask(
            id="x",
            chat_id=chat_id,
            creator_open_id=creator_open_id,
            prompt="p",
            schedule_type=ScheduleType.INTERVAL,
            schedule_value="60",
            next_run_at=datetime(2026, 3, 11, 0, 0, 0, tzinfo=timezone.utc),
        )

    t1 = make("oc_111", "ou_aaa")
    t2 = make("oc_222", "ou_bbb")
    assert t1.session_id == "oc_111:ou_aaa"
    assert t2.session_id == "oc_222:ou_bbb"
    assert t1.session_id != t2.session_id
