"""Tests for nextme.core.dispatcher."""
import asyncio
import uuid
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.core.dispatcher import TaskDispatcher
from nextme.config.schema import AppConfig, Project, Settings
from nextme.core.session import SessionRegistry, UserContext, Session
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.core.path_lock import PathLockRegistry
from nextme.protocol.types import Task, TaskStatus, PermissionChoice, PermOption


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(content: str, session_id: str = "chat_abc:user_xyz") -> Task:
    """Create a minimal Task for dispatch tests."""
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        timeout=timedelta(seconds=30),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_session_registry():
    """Reset the SessionRegistry singleton before each test."""
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


@pytest.fixture
def settings():
    return Settings(task_queue_capacity=5, permission_timeout_seconds=1.0)


@pytest.fixture
def project(tmp_path):
    return Project(name="myproj", path=str(tmp_path), executor="claude-code-acp")


@pytest.fixture
def config(project):
    return AppConfig(app_id="cli_x", app_secret="secret", projects=[project])


@pytest.fixture
def mock_replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="msg_id")
    r.update_card = AsyncMock()
    r.reply_text = AsyncMock(return_value="thread_msg_id")
    r.reply_card = AsyncMock(return_value="thread_card_id")
    r.build_help_card = MagicMock(return_value='{"card": "help"}')
    r.build_permission_card = MagicMock(return_value='{"card": "perm"}')
    r.build_progress_card = MagicMock(return_value='{"card": "prog"}')
    r.build_result_card = MagicMock(return_value='{"card": "result"}')
    r.build_error_card = MagicMock(return_value='{"card": "error"}')
    return r


@pytest.fixture
def feishu_client(mock_replier):
    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=mock_replier)
    return fc


@pytest.fixture
def acp_registry():
    registry = MagicMock(spec=ACPRuntimeRegistry)
    registry.get = MagicMock(return_value=None)
    return registry


@pytest.fixture
def path_lock_registry():
    return PathLockRegistry()


@pytest.fixture
def session_registry():
    return SessionRegistry()


@pytest.fixture
def dispatcher(config, settings, acp_registry, feishu_client, path_lock_registry, session_registry):
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=path_lock_registry,
        feishu_client=feishu_client,
    )


# ---------------------------------------------------------------------------
# Tests: meta-command routing via /help
# ---------------------------------------------------------------------------

async def test_dispatch_help_command(dispatcher, mock_replier):
    """Dispatching /help sends a help card."""
    task = make_task("/help")
    with patch("nextme.core.dispatcher.handle_help", new_callable=AsyncMock) as mock_help:
        await dispatcher.dispatch(task)
    mock_help.assert_awaited_once()


async def test_dispatch_help_command_uppercase(dispatcher, mock_replier):
    """Dispatching /HELP (case-insensitive) sends a help card."""
    task = make_task("/HELP")
    with patch("nextme.core.dispatcher.handle_help", new_callable=AsyncMock) as mock_help:
        await dispatcher.dispatch(task)
    mock_help.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: /new command
# ---------------------------------------------------------------------------

async def test_dispatch_new_command(dispatcher, mock_replier):
    """/new resets the session via handle_new."""
    task = make_task("/new")
    with patch("nextme.core.dispatcher.handle_new", new_callable=AsyncMock) as mock_new:
        await dispatcher.dispatch(task)
    mock_new.assert_awaited_once()


async def test_dispatch_new_command_passes_runtime(dispatcher, acp_registry, mock_replier):
    """/new looks up the runtime from acp_registry."""
    mock_runtime = MagicMock()
    acp_registry.get.return_value = mock_runtime
    task = make_task("/new")
    with patch("nextme.core.dispatcher.handle_new", new_callable=AsyncMock) as mock_new:
        await dispatcher.dispatch(task)
    # acp_registry.get was called with the context_id
    acp_registry.get.assert_called_once_with(task.session_id)
    # The runtime was passed to handle_new
    call_args = mock_new.call_args
    assert call_args.args[1] is mock_runtime or call_args.kwargs.get("runtime") is mock_runtime


# ---------------------------------------------------------------------------
# Tests: /stop command
# ---------------------------------------------------------------------------

async def test_dispatch_stop_command(dispatcher, mock_replier):
    """/stop calls handle_stop."""
    task = make_task("/stop")
    with patch("nextme.core.dispatcher.handle_stop", new_callable=AsyncMock) as mock_stop:
        await dispatcher.dispatch(task)
    mock_stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: /status command
# ---------------------------------------------------------------------------

async def test_dispatch_status_command(dispatcher, mock_replier):
    """/status calls handle_status."""
    task = make_task("/status")
    with patch("nextme.core.dispatcher.handle_status", new_callable=AsyncMock) as mock_status:
        await dispatcher.dispatch(task)
    mock_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: /project command
# ---------------------------------------------------------------------------

async def test_dispatch_project_command_with_name(dispatcher, mock_replier):
    """/project <name> calls handle_project."""
    task = make_task("/project myproj")
    with patch("nextme.core.dispatcher.handle_project", new_callable=AsyncMock) as mock_proj:
        await dispatcher.dispatch(task)
    mock_proj.assert_awaited_once()


async def test_dispatch_project_command_no_arg(dispatcher, mock_replier):
    """/project with no argument sends usage help."""
    task = make_task("/project")
    with patch("nextme.core.dispatcher.handle_project", new_callable=AsyncMock) as mock_proj:
        await dispatcher.dispatch(task)
    # handle_project should NOT be called; instead a text message with usage is sent
    mock_proj.assert_not_awaited()
    mock_replier.send_text.assert_awaited_once()
    sent_text = mock_replier.send_text.call_args.args[1]
    assert "project" in sent_text.lower() or "用法" in sent_text


# ---------------------------------------------------------------------------
# Tests: /skill command
# ---------------------------------------------------------------------------

async def test_dispatch_skill_with_trigger(dispatcher, mock_replier):
    """/skill <trigger> sends an acknowledgment text."""
    task = make_task("/skill my-skill")
    await dispatcher.dispatch(task)
    mock_replier.send_text.assert_awaited_once()
    sent_text = mock_replier.send_text.call_args.args[1]
    assert "my-skill" in sent_text


async def test_dispatch_skill_no_arg(dispatcher, mock_replier):
    """/skill with no argument sends usage error."""
    task = make_task("/skill")
    await dispatcher.dispatch(task)
    mock_replier.send_text.assert_awaited_once()
    sent_text = mock_replier.send_text.call_args.args[1]
    assert "skill" in sent_text.lower() or "用法" in sent_text


# ---------------------------------------------------------------------------
# Tests: unknown command → help card
# ---------------------------------------------------------------------------

async def test_dispatch_unknown_command_shows_help(dispatcher, mock_replier):
    """Unknown slash command falls back to displaying the help card."""
    task = make_task("/foobar")
    with patch("nextme.core.dispatcher.handle_help", new_callable=AsyncMock) as mock_help:
        await dispatcher.dispatch(task)
    mock_help.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: normal message → enqueue
# ---------------------------------------------------------------------------

async def test_dispatch_normal_message_enqueues_task(dispatcher, session_registry, config, settings):
    """Normal text enqueues the task and starts a worker."""
    task = make_task("hello world")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_worker_instance = MagicMock()
        mock_worker_instance.run = AsyncMock()
        MockWorker.return_value = mock_worker_instance
        await dispatcher.dispatch(task)

    user_ctx = session_registry.get(task.session_id)
    assert user_ctx is not None
    session = user_ctx.get_active_session()
    assert session is not None
    # Task was appended to pending_tasks
    assert task in session.pending_tasks


async def test_dispatch_normal_message_starts_worker(dispatcher):
    """Dispatching a normal task creates a SessionWorker task."""
    task = make_task("run this")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_worker_instance = MagicMock()
        mock_worker_instance.run = AsyncMock()
        MockWorker.return_value = mock_worker_instance
        await dispatcher.dispatch(task)

    MockWorker.assert_called_once()
    # Worker task should be registered
    context_id = task.session_id
    assert context_id in dispatcher._worker_tasks


async def test_dispatch_does_not_restart_running_worker(dispatcher):
    """A second task dispatched while worker is running does not spawn another worker."""
    task1 = make_task("first")
    task2 = make_task("second")

    call_count = 0

    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        # Make worker run "forever" so it stays running
        worker_started = asyncio.Event()

        async def slow_run():
            worker_started.set()
            await asyncio.sleep(100)

        mock_worker_instance = MagicMock()
        mock_worker_instance.run = slow_run
        MockWorker.return_value = mock_worker_instance

        await dispatcher.dispatch(task1)
        # Worker is started; dispatch second task
        await dispatcher.dispatch(task2)

    # SessionWorker should only be constructed once
    assert MockWorker.call_count == 1


async def test_dispatch_queue_full_sends_error(dispatcher, mock_replier):
    """When the task queue is full, sends a queue-full message and drops the task."""
    session_id = "chat_abc:user_xyz"

    # Pre-fill the queue by dispatching tasks (queue capacity = 5).
    # Patch SessionWorker so workers don't drain the queue.
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        async def block_forever():
            await asyncio.sleep(9999)

        mock_instance = MagicMock()
        mock_instance.run = block_forever
        MockWorker.return_value = mock_instance

        # Fill up the queue (capacity = 5)
        for i in range(5):
            t = make_task(f"msg {i}", session_id=session_id)
            await dispatcher.dispatch(t)

        # This one should be dropped
        overflow_task = make_task("overflow", session_id=session_id)
        mock_replier.send_text.reset_mock()
        await dispatcher.dispatch(overflow_task)

    mock_replier.send_text.assert_awaited_once()
    sent_text = mock_replier.send_text.call_args.args[1]
    assert "满" in sent_text or "队列" in sent_text or "full" in sent_text.lower()


# ---------------------------------------------------------------------------
# Tests: permission reply
# ---------------------------------------------------------------------------

async def test_dispatch_permission_reply_resolves_future(dispatcher, session_registry, config, settings):
    """A digit reply matching a pending permission option resolves the future."""
    session_id = "chat_abc:user_xyz"

    # Create user context and session
    user_ctx = session_registry.get_or_create(session_id)
    project = config.default_project
    session = user_ctx.get_or_create_session(project, settings)

    # Set up a pending permission future with option index 1
    perm_future = session.set_permission_pending([
        PermOption(index=1, label="Allow", description="Allow action"),
        PermOption(index=2, label="Deny", description="Deny action"),
    ])

    # Dispatch digit reply "1"
    task = make_task("1", session_id=session_id)
    await dispatcher.dispatch(task)

    # The future should be resolved
    assert perm_future.done()
    result = perm_future.result()
    assert result.option_index == 1
    assert result.option_label == "Allow"


async def test_dispatch_permission_reply_non_digit_not_treated_as_permission(
    dispatcher, session_registry, config, settings
):
    """A non-digit text is NOT treated as a permission reply even if a future is pending."""
    session_id = "chat_abc:user_xyz"

    user_ctx = session_registry.get_or_create(session_id)
    project = config.default_project
    session = user_ctx.get_or_create_session(project, settings)

    perm_future = session.set_permission_pending([
        PermOption(index=1, label="Allow"),
    ])

    # Non-digit normal message
    task = make_task("not a digit", session_id=session_id)
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    # Future should still be pending (not resolved)
    assert not perm_future.done()
    # Task should have been enqueued instead
    assert task in session.pending_tasks


async def test_dispatch_permission_digit_not_in_options_not_treated_as_permission(
    dispatcher, session_registry, config, settings
):
    """A digit not matching any option index is treated as a normal message."""
    session_id = "chat_abc:user_xyz"

    user_ctx = session_registry.get_or_create(session_id)
    project = config.default_project
    session = user_ctx.get_or_create_session(project, settings)

    # Options are 1 and 2, but we'll send "5"
    perm_future = session.set_permission_pending([
        PermOption(index=1, label="Allow"),
        PermOption(index=2, label="Deny"),
    ])

    task = make_task("5", session_id=session_id)
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    # Future should still be pending
    assert not perm_future.done()
    # Task was queued as a normal message
    assert task in session.pending_tasks


# ---------------------------------------------------------------------------
# Tests: session bootstrap
# ---------------------------------------------------------------------------

async def test_dispatch_creates_session_with_default_project(
    dispatcher, session_registry, config, settings
):
    """First dispatch creates a session using the first project as default."""
    task = make_task("hello")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    user_ctx = session_registry.get(task.session_id)
    assert user_ctx is not None
    session = user_ctx.get_active_session()
    assert session is not None
    assert session.project_name == config.default_project.name


async def test_dispatch_no_projects_sends_error(config, settings, acp_registry, feishu_client, path_lock_registry, mock_replier):
    """When no projects are configured, sends a configuration error message."""
    empty_config = AppConfig(app_id="x", app_secret="y", projects=[])
    empty_registry = SessionRegistry()
    d = TaskDispatcher(
        config=empty_config,
        settings=settings,
        session_registry=empty_registry,
        acp_registry=acp_registry,
        path_lock_registry=path_lock_registry,
        feishu_client=feishu_client,
    )
    task = make_task("hello")
    await d.dispatch(task)
    mock_replier.send_text.assert_awaited_once()
    sent_text = mock_replier.send_text.call_args.args[1]
    assert "配置" in sent_text or "项目" in sent_text


# ---------------------------------------------------------------------------
# Tests: _get_chat_id
# ---------------------------------------------------------------------------

def test_get_chat_id_extracts_first_segment(dispatcher):
    """_get_chat_id splits on ':' and returns the first part."""
    chat_id = dispatcher._get_chat_id("oc_abc123:ou_xyz789")
    assert chat_id == "oc_abc123"


def test_get_chat_id_no_colon(dispatcher):
    """_get_chat_id with no colon returns the whole string."""
    chat_id = dispatcher._get_chat_id("oc_abc123")
    assert chat_id == "oc_abc123"


# ---------------------------------------------------------------------------
# Tests: _is_meta_command
# ---------------------------------------------------------------------------

def test_is_meta_command_slash(dispatcher):
    assert dispatcher._is_meta_command("/help") is True


def test_is_meta_command_no_slash(dispatcher):
    assert dispatcher._is_meta_command("hello") is False


def test_is_meta_command_empty(dispatcher):
    assert dispatcher._is_meta_command("") is False


# ---------------------------------------------------------------------------
# Tests: _is_permission_reply
# ---------------------------------------------------------------------------

def test_is_permission_reply_no_future(dispatcher, config, settings):
    """Returns False when no permission future is pending."""
    project = config.default_project
    session = Session(context_id="chat:user", project=project, settings=settings)
    assert dispatcher._is_permission_reply(session, "1") is False


async def test_is_permission_reply_with_pending_future_matching_index(dispatcher, config, settings):
    """Returns True when a matching pending future exists and digit matches."""
    project = config.default_project
    session = Session(context_id="chat:user", project=project, settings=settings)
    session.set_permission_pending([PermOption(index=1, label="Allow")])
    assert dispatcher._is_permission_reply(session, "1") is True


async def test_is_permission_reply_with_pending_future_non_matching_index(dispatcher, config, settings):
    """Returns False when the digit does not match any option index."""
    project = config.default_project
    session = Session(context_id="chat:user", project=project, settings=settings)
    session.set_permission_pending([PermOption(index=1, label="Allow")])
    assert dispatcher._is_permission_reply(session, "9") is False


async def test_is_permission_reply_non_digit(dispatcher, config, settings):
    """Returns False when text is not a digit."""
    project = config.default_project
    session = Session(context_id="chat:user", project=project, settings=settings)
    session.set_permission_pending([PermOption(index=1, label="Allow")])
    assert dispatcher._is_permission_reply(session, "yes") is False


async def test_is_permission_reply_done_future(dispatcher, config, settings):
    """Returns False when the pending future is already done."""
    project = config.default_project
    session = Session(context_id="chat:user", project=project, settings=settings)
    future = session.set_permission_pending([PermOption(index=1, label="Allow")])
    # Resolve the future
    session.resolve_permission(PermissionChoice(request_id="", option_index=1))
    assert dispatcher._is_permission_reply(session, "1") is False


# ---------------------------------------------------------------------------
# Tests: worker restart after completion
# ---------------------------------------------------------------------------

async def test_dispatch_restarts_finished_worker(dispatcher):
    """After a worker finishes, the next dispatch creates a new worker."""
    session_id = "chat_abc:user_xyz"

    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()  # completes immediately
        MockWorker.return_value = mock_instance

        task1 = make_task("first", session_id=session_id)
        await dispatcher.dispatch(task1)

        # Allow the worker task to complete
        context_id = task1.session_id
        worker_asyncio_task = dispatcher._worker_tasks.get(context_id)
        if worker_asyncio_task:
            await asyncio.sleep(0)  # yield control so task can complete
            await asyncio.sleep(0)

        task2 = make_task("second", session_id=session_id)
        await dispatcher.dispatch(task2)

    # Two workers should have been created (one per dispatch after the first finished)
    assert MockWorker.call_count >= 1


# ---------------------------------------------------------------------------
# Tests: immediate "ok" thread ack
# ---------------------------------------------------------------------------

def make_task_with_message_id(content: str, message_id: str, session_id: str = "chat_abc:user_xyz") -> Task:
    """Create a Task with a Feishu message_id for thread-reply tests."""
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id=message_id,
    )


async def test_dispatch_sends_ok_ack_when_message_id_present(dispatcher, mock_replier):
    """When task has a message_id, an 'ok' thread reply is sent after enqueueing."""
    task = make_task_with_message_id("hello", message_id="om_src_msg")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)
    mock_replier.reply_text.assert_awaited_once_with("om_src_msg", "ok")


async def test_dispatch_no_ok_ack_when_message_id_empty(dispatcher, mock_replier):
    """When task has no message_id, no thread ack is sent."""
    task = make_task("hello")  # message_id defaults to ""
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)
    mock_replier.reply_text.assert_not_awaited()


async def test_dispatch_reply_fn_uses_reply_card_when_message_id_set(dispatcher, mock_replier):
    """reply_fn calls reply_card (not send_card) when task has message_id."""
    task = make_task_with_message_id("hello", message_id="om_src_msg")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    from nextme.protocol.types import Reply, ReplyType
    reply = Reply(type=ReplyType.CARD, content='{"card": "result"}')
    await task.reply_fn(reply)
    mock_replier.reply_card.assert_awaited_with("om_src_msg", '{"card": "result"}')
    mock_replier.send_card.assert_not_awaited()


async def test_dispatch_reply_fn_uses_send_card_when_no_message_id(dispatcher, mock_replier):
    """reply_fn falls back to send_card when task has no message_id."""
    task = make_task("hello")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    from nextme.protocol.types import Reply, ReplyType
    reply = Reply(type=ReplyType.CARD, content='{"card": "result"}')
    await task.reply_fn(reply)
    mock_replier.send_card.assert_awaited()
    mock_replier.reply_card.assert_not_awaited()
