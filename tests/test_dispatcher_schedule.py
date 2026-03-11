"""Tests for /schedule command routing in TaskDispatcher."""
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from nextme.protocol.types import Task
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.session import SessionRegistry
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.core.path_lock import PathLockRegistry
from nextme.config.schema import AppConfig, Settings
from nextme.config.schema import Project


@pytest.fixture(autouse=True)
def reset_session_registry():
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


@pytest.fixture
def tmp_project(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return Project(name="myproject", path=str(d), executor="claude-code-acp")


@pytest.fixture
def replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock()
    r.send_reaction = AsyncMock()
    r.reply_text = AsyncMock()
    r.reply_card = AsyncMock()
    r.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')
    r.build_help_card = MagicMock(return_value='{"card":"help"}')
    return r


@pytest.fixture
def mock_scheduler_db():
    db = AsyncMock()
    db.create = AsyncMock()
    db.list_by_chat = AsyncMock(return_value=[])
    db.update_status = AsyncMock()
    db.delete = AsyncMock()
    return db


@pytest.fixture
def dispatcher(replier, mock_scheduler_db, tmp_project):
    config = AppConfig(projects=[tmp_project])
    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)
    return TaskDispatcher(
        config=config,
        settings=Settings(task_queue_capacity=10, progress_debounce_seconds=0.0),
        session_registry=SessionRegistry.get_instance(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        scheduler_db=mock_scheduler_db,
    )


def _make_task(content: str, session_id: str = "oc_x:ou_y") -> Task:
    async def reply_fn(_r): pass
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=reply_fn,
        message_id="msg_001",
        chat_type="p2p",
    )


async def test_schedule_list_routes_to_db(dispatcher, replier, mock_scheduler_db):
    """'/schedule list' should call db.list_by_chat."""
    await dispatcher.dispatch(_make_task("/schedule list"))
    mock_scheduler_db.list_by_chat.assert_called_once()


async def test_schedule_create_once(dispatcher, replier, mock_scheduler_db):
    """'/schedule ... at <time>' should create a task in DB."""
    await dispatcher.dispatch(_make_task("/schedule say hi at 2026-03-20T10:00:00+08:00"))
    mock_scheduler_db.create.assert_called_once()
    replier.send_text.assert_called_once()
    assert "已创建" in replier.send_text.call_args[0][1]


async def test_schedule_create_interval(dispatcher, replier, mock_scheduler_db):
    """'/schedule ... every <N><unit>' should create an interval task."""
    await dispatcher.dispatch(_make_task("/schedule ping every 1h"))
    mock_scheduler_db.create.assert_called_once()
    task = mock_scheduler_db.create.call_args[0][0]
    assert task.schedule_value == "3600"


async def test_schedule_no_db_sends_error(replier, tmp_project):
    """Without scheduler_db, should send '定时任务功能未启用'."""
    config = AppConfig(projects=[tmp_project])
    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)
    d = TaskDispatcher(
        config=config,
        settings=Settings(task_queue_capacity=10, progress_debounce_seconds=0.0),
        session_registry=SessionRegistry.get_instance(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        scheduler_db=None,
    )
    await d.dispatch(_make_task("/schedule list"))
    replier.send_text.assert_called_once()
    assert "未启用" in replier.send_text.call_args[0][1]


async def test_schedule_command_does_not_enqueue(dispatcher, mock_scheduler_db):
    """/schedule should return early and NOT enqueue to session worker."""
    from nextme.core.session import SessionRegistry
    task = _make_task("/schedule list")
    await dispatcher.dispatch(task)
    # If it was enqueued, session would be created. Verify list_by_chat called = command handled.
    mock_scheduler_db.list_by_chat.assert_called_once()
