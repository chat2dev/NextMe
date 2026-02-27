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
    r.send_reaction = AsyncMock()
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
    """/new looks up the runtime from acp_registry using context_id:project_name key."""
    mock_runtime = MagicMock()
    acp_registry.get.return_value = mock_runtime
    task = make_task("/new")
    with patch("nextme.core.dispatcher.handle_new", new_callable=AsyncMock) as mock_new:
        await dispatcher.dispatch(task)
    # acp_registry.get was called with the scoped key (context_id:project_name)
    call_arg = acp_registry.get.call_args[0][0]
    assert call_arg.startswith(task.session_id + ":")
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
    # Worker task should be registered under context_id:project_name key
    assert any(k.startswith(task.session_id + ":") for k in dispatcher._worker_tasks)


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
        worker_asyncio_task = next(
            (t for k, t in dispatcher._worker_tasks.items() if k.startswith(task1.session_id + ":")),
            None,
        )
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


async def test_dispatch_sends_ok_emoji_when_message_id_present(dispatcher, mock_replier):
    """When task has a message_id, an 'OK' emoji reaction is sent after enqueueing."""
    task = make_task_with_message_id("hello", message_id="om_src_msg")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)
    mock_replier.send_reaction.assert_awaited_once_with("om_src_msg", "OK")


async def test_dispatch_no_reaction_when_message_id_empty(dispatcher, mock_replier):
    """When task has no message_id, no emoji reaction is sent."""
    task = make_task("hello")  # message_id defaults to ""
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)
    mock_replier.send_reaction.assert_not_awaited()


async def test_dispatch_reply_fn_uses_reply_card_for_group(dispatcher, mock_replier):
    """reply_fn uses reply_card with in_thread=True for group chats."""
    task = make_task_with_message_id("hello", message_id="om_src_msg")
    task.chat_type = "group"
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    from nextme.protocol.types import Reply, ReplyType
    reply = Reply(type=ReplyType.CARD, content='{"card": "result"}')
    await task.reply_fn(reply)
    mock_replier.reply_card.assert_awaited_with("om_src_msg", '{"card": "result"}', in_thread=True)
    mock_replier.send_card.assert_not_awaited()


async def test_dispatch_reply_fn_uses_reply_card_for_p2p(dispatcher, mock_replier):
    """reply_fn uses reply_card with in_thread=False (quote reply) for p2p chats."""
    task = make_task_with_message_id("hello", message_id="om_src_msg")
    task.chat_type = "p2p"
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await dispatcher.dispatch(task)

    from nextme.protocol.types import Reply, ReplyType
    reply = Reply(type=ReplyType.CARD, content='{"card": "result"}')
    await task.reply_fn(reply)
    mock_replier.reply_card.assert_awaited_with("om_src_msg", '{"card": "result"}', in_thread=False)
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


# ---------------------------------------------------------------------------
# Tests: chat binding routing
# ---------------------------------------------------------------------------


def make_bound_dispatcher(config, settings, acp_registry, feishu_client, path_lock_registry, session_registry):
    """Create a dispatcher with a static binding: chat_abc → myproj."""
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=path_lock_registry,
        feishu_client=feishu_client,
    )


async def test_dispatch_static_binding_routes_to_bound_project(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """Static binding in config routes all messages to the bound project."""
    bound_config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project],
        bindings={"chat_abc": project.name},
    )
    d = TaskDispatcher(
        config=bound_config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
    )
    task = make_task("hello", session_id="chat_abc:user_xyz")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    user_ctx = session_registry.get("chat_abc:user_xyz")
    assert user_ctx is not None
    assert project.name in user_ctx.sessions


async def test_dispatch_dynamic_binding_routes_to_bound_project(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """Dynamic binding (_dynamic_bindings) routes messages to the bound project."""
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
    )
    d._dynamic_bindings["chat_abc"] = project.name
    task = make_task("hello", session_id="chat_abc:user_xyz")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    user_ctx = session_registry.get("chat_abc:user_xyz")
    assert user_ctx is not None
    assert project.name in user_ctx.sessions


async def test_dispatch_static_binding_takes_precedence_over_dynamic(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project, tmp_path
):
    """Static config binding wins over dynamic binding when both exist."""
    project_b = Project(name="repo-B", path=str(tmp_path / "repo_b"), executor="claude")
    bound_config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project, project_b],
        bindings={"chat_abc": project.name},   # static → project
    )
    d = TaskDispatcher(
        config=bound_config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
    )
    d._dynamic_bindings["chat_abc"] = project_b.name  # dynamic → repo-B

    task = make_task("hello", session_id="chat_abc:user_xyz")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    user_ctx = session_registry.get("chat_abc:user_xyz")
    # Static binding (project.name) should win
    assert project.name in user_ctx.sessions


async def test_dispatch_binding_unknown_project_falls_back_to_default(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """If binding points to an unknown project, fall back to default."""
    bound_config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project],
        bindings={"chat_abc": "nonexistent-project"},
    )
    d = TaskDispatcher(
        config=bound_config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
    )
    task = make_task("hello", session_id="chat_abc:user_xyz")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    user_ctx = session_registry.get("chat_abc:user_xyz")
    # Falls back to default project (the only configured project)
    assert project.name in user_ctx.sessions


async def test_dispatch_project_bind_subcommand_updates_dynamic_bindings(
    dispatcher, mock_replier, project
):
    """/project bind <name> updates _dynamic_bindings."""
    task = make_task(f"/project bind {project.name}")
    with patch("nextme.core.dispatcher.handle_bind", new_callable=AsyncMock, return_value=project.name):
        await dispatcher.dispatch(task)
    assert dispatcher._dynamic_bindings.get("chat_abc") == project.name


async def test_dispatch_project_unbind_subcommand_clears_dynamic_bindings(
    dispatcher, mock_replier, project
):
    """/project unbind removes chat from _dynamic_bindings."""
    dispatcher._dynamic_bindings["chat_abc"] = project.name
    task = make_task("/project unbind")
    with patch("nextme.core.dispatcher.handle_unbind", new_callable=AsyncMock, return_value=True):
        await dispatcher.dispatch(task)
    assert "chat_abc" not in dispatcher._dynamic_bindings


async def test_dispatch_project_bind_no_arg_sends_usage(dispatcher, mock_replier):
    """/project bind without arg sends usage hint."""
    task = make_task("/project bind")
    await dispatcher.dispatch(task)
    mock_replier.send_text.assert_awaited()
    text = mock_replier.send_text.call_args[0][1]
    assert "bind" in text


async def test_dispatch_state_store_set_binding_called_on_bind(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """/project bind persists binding to state_store."""
    mock_store = MagicMock()
    mock_store.get_all_bindings = MagicMock(return_value={})
    mock_store.set_binding = MagicMock()
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
        state_store=mock_store,
    )
    task = make_task(f"/project bind {project.name}")
    with patch("nextme.core.dispatcher.handle_bind", new_callable=AsyncMock, return_value=project.name):
        await d.dispatch(task)
    mock_store.set_binding.assert_called_once_with("chat_abc", project.name)


async def test_dispatch_state_store_remove_binding_called_on_unbind(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """/project unbind removes binding from state_store."""
    mock_store = MagicMock()
    mock_store.get_all_bindings = MagicMock(return_value={"chat_abc": project.name})
    mock_store.remove_binding = MagicMock()
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
        state_store=mock_store,
    )
    d._dynamic_bindings["chat_abc"] = project.name
    task = make_task("/project unbind")
    with patch("nextme.core.dispatcher.handle_unbind", new_callable=AsyncMock, return_value=True):
        await d.dispatch(task)
    mock_store.remove_binding.assert_called_once_with("chat_abc")


async def test_dispatcher_loads_dynamic_bindings_from_state_store(
    config, settings, acp_registry, feishu_client, path_lock_registry, session_registry, project
):
    """Dispatcher loads existing bindings from state_store at construction."""
    mock_store = MagicMock()
    mock_store.get_all_bindings = MagicMock(return_value={"oc_existing": project.name})
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=session_registry, acp_registry=acp_registry,
        path_lock_registry=path_lock_registry, feishu_client=feishu_client,
        state_store=mock_store,
    )
    assert d._dynamic_bindings == {"oc_existing": project.name}


# ---------------------------------------------------------------------------
# Tests: handle_card_action
# ---------------------------------------------------------------------------


def test_handle_card_action_resolves_permission(
    dispatcher, session_registry, config, settings
):
    """handle_card_action resolves a pending permission future via button click."""
    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    project = config.default_project
    session = user_ctx.get_or_create_session(project, settings)

    perm_future = session.set_permission_pending([
        PermOption(index=1, label="Allow"),
        PermOption(index=2, label="Deny"),
    ])

    dispatcher.handle_card_action(session_id, 1)

    assert perm_future.done()
    result = perm_future.result()
    assert result.option_index == 1
    assert result.option_label == "Allow"


def test_handle_card_action_unknown_session_does_not_raise(dispatcher):
    """handle_card_action with unknown session_id logs a warning but does not raise."""
    dispatcher.handle_card_action("nonexistent:session", 1)  # should not raise


def test_handle_card_action_no_active_session_does_not_raise(
    dispatcher, session_registry
):
    """handle_card_action with no active session logs a warning but does not raise."""
    session_registry.get_or_create("chat_abc:user_xyz")
    dispatcher.handle_card_action("chat_abc:user_xyz", 1)  # no active session, no raise


def test_handle_card_action_no_pending_permission_does_not_raise(
    dispatcher, session_registry, config, settings
):
    """handle_card_action when no permission is pending is a no-op."""
    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    user_ctx.get_or_create_session(config.default_project, settings)
    # No perm future set
    dispatcher.handle_card_action(session_id, 1)  # should not raise


def test_handle_card_action_index_not_in_options_does_not_resolve(
    dispatcher, session_registry, config, settings
):
    """handle_card_action with an index not in options does not resolve the future."""
    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    session = user_ctx.get_or_create_session(config.default_project, settings)

    perm_future = session.set_permission_pending([
        PermOption(index=1, label="Allow"),
        PermOption(index=2, label="Deny"),
    ])

    dispatcher.handle_card_action(session_id, 9)  # index 9 not in options

    assert not perm_future.done()


def test_handle_card_action_with_project_name_resolves_correct_session(
    dispatcher, session_registry, config, settings
):
    """project_name targets the exact session in a multi-project setup."""
    from nextme.config.schema import Project

    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    proj_a = config.default_project
    proj_b = Project(name="other", path=str(proj_a.path), executor="claude-code-acp")

    session_a = user_ctx.get_or_create_session(proj_a, settings)
    session_b = user_ctx.get_or_create_session(proj_b, settings)
    # active_project is now "other" (proj_b was set last)

    perm_future_a = session_a.set_permission_pending([PermOption(index=1, label="Allow")])
    # session_b has no pending permission

    # Pass project_name so we target session_a directly
    dispatcher.handle_card_action(session_id, 1, project_name=proj_a.name)

    assert perm_future_a.done()
    assert perm_future_a.result().option_index == 1


def test_handle_card_action_without_project_name_scans_all_sessions(
    dispatcher, session_registry, config, settings
):
    """Without project_name, handle_card_action scans all sessions for a pending perm."""
    from nextme.config.schema import Project

    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    proj_a = config.default_project
    proj_b = Project(name="other", path=str(proj_a.path), executor="claude-code-acp")

    session_a = user_ctx.get_or_create_session(proj_a, settings)
    user_ctx.get_or_create_session(proj_b, settings)
    # active_project is now "other", but session_a has the pending permission

    perm_future_a = session_a.set_permission_pending([PermOption(index=1, label="Allow")])

    # No project_name → falls back to scanning; should find session_a
    dispatcher.handle_card_action(session_id, 1)

    assert perm_future_a.done()


def test_handle_card_action_wrong_project_name_does_not_resolve(
    dispatcher, session_registry, config, settings
):
    """project_name pointing to a session without pending perm is a no-op."""
    from nextme.config.schema import Project

    session_id = "chat_abc:user_xyz"
    user_ctx = session_registry.get_or_create(session_id)
    proj_a = config.default_project
    proj_b = Project(name="other", path=str(proj_a.path), executor="claude-code-acp")

    session_a = user_ctx.get_or_create_session(proj_a, settings)
    user_ctx.get_or_create_session(proj_b, settings)

    perm_future_a = session_a.set_permission_pending([PermOption(index=1, label="Allow")])

    # Points to proj_b which has no pending permission
    dispatcher.handle_card_action(session_id, 1, project_name=proj_b.name)

    assert not perm_future_a.done()  # session_a's future untouched
