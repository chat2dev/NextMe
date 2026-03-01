"""Full-stack integration tests wiring together real components.

Only external boundaries (ACP subprocess, Feishu HTTP) are mocked.
Everything else — SessionRegistry, PathLockRegistry, TaskDispatcher,
SessionWorker — runs as real code.

Tests
-----
1.  test_normal_message_full_flow                    — normal message → agent executes → result card
2.  test_help_command_no_runtime                     — /help → help card, runtime NOT called
3.  test_new_command_resets_session                  — /new after a normal task → no crash
4.  test_second_task_queued                          — 2 tasks queued → runtime called twice
5.  test_acl_gate_blocks_unauthorized_user           — ACL None → denied card, runtime not called
6.  test_acl_gate_whoami_bypasses_auth               — /whoami with ACL None → no denied card
7.  test_no_project_sends_error                      — default_project=None → error text sent
8.  test_group_chat_unauthorized_gets_dm             — group chat: text prompt in thread + DM, no card in thread
9.  test_acl_apply_by_different_operator_is_ignored  — cross-user apply button click → no application created
10. test_review_card_disabled_after_approve          — approve → update_card called on review notification
11. test_review_card_disabled_after_reject           — reject → update_card called on review notification
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextme.acl.db import AclDb
from nextme.acl.manager import AclManager
from nextme.acl.schema import Role
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import AppConfig, Project, Settings
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.path_lock import PathLockRegistry
from nextme.core.session import SessionRegistry
from nextme.protocol.types import Task


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def acl_db(tmp_path):
    """Real in-process AclDb backed by a temporary SQLite file."""
    db = AclDb(db_path=tmp_path / "acl_e2e.db")
    await db.open()
    yield db
    await db.close()


@pytest.fixture(autouse=True)
def reset_session_registry():
    """Reset the SessionRegistry singleton before and after each test."""
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


@pytest.fixture
def tmp_project(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return Project(name="myproject", path=str(d), executor="claude-code-acp")


@pytest.fixture
def settings():
    return Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        streaming_enabled=False,  # disable streaming to use simpler fallback path
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(content: str, session_id: str = "oc_chat:ou_user") -> Task:
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_test_msg",
        chat_type="p2p",
        timeout=timedelta(seconds=10),
    )


def make_replier() -> MagicMock:
    """Create a fully-mocked Replier that satisfies all worker/dispatcher calls."""
    r = MagicMock()
    # Async send methods
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="card_msg_id")
    r.send_to_user = AsyncMock()
    r.send_reaction = AsyncMock()  # dispatcher calls this on every normal task
    r.reply_text = AsyncMock(return_value="thread_msg_id")
    r.reply_card = AsyncMock(return_value="progress_msg_id")
    r.reply_card_by_id = AsyncMock(return_value="card_msg_id")
    r.update_card = AsyncMock()
    r.create_card = AsyncMock(return_value="")   # return "" → streaming disabled path
    r.get_card_id = AsyncMock(return_value="")
    r.stream_set_content = AsyncMock()
    r.update_card_entity = AsyncMock()
    r.send_card_by_id = AsyncMock(return_value="msg_id")
    # Sync build methods
    r.build_progress_card = MagicMock(return_value='{"card":"progress"}')
    r.build_result_card = MagicMock(return_value='{"card":"result"}')
    r.build_error_card = MagicMock(return_value='{"card":"error"}')
    r.build_streaming_progress_card = MagicMock(return_value='{"card":"sp"}')
    r.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')
    r.build_help_card = MagicMock(return_value='{"card":"help"}')
    r.build_whoami_card = MagicMock(return_value='{"card":"whoami"}')
    r.build_permission_card = MagicMock(return_value='{"card":"perm"}')
    r.build_acl_review_notification_card = MagicMock(return_value='{"card":"notify"}')
    r.build_acl_review_done_card = MagicMock(return_value='{"card":"done"}')
    return r


def make_feishu_client(replier: MagicMock) -> MagicMock:
    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=replier)
    return fc


def make_mock_runtime(execute_return: str = "Agent answer") -> MagicMock:
    """Create a mock runtime whose execute() returns immediately."""
    runtime = AsyncMock()
    runtime.actual_id = "test-acp-session-id"
    runtime.is_running = True
    runtime.ensure_ready = AsyncMock()
    runtime.execute = AsyncMock(return_value=execute_return)
    runtime.restore_session = AsyncMock()
    runtime.stop = AsyncMock()
    return runtime


async def drain_session_queue(session, *, timeout: float = 5.0) -> None:
    """Wait for the session task queue to drain."""
    await asyncio.wait_for(session.task_queue.join(), timeout=timeout)


async def cancel_workers(dispatcher: TaskDispatcher) -> None:
    """Cancel all active worker tasks owned by the dispatcher."""
    for worker_task in list(dispatcher._worker_tasks.values()):
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


def _get_session(dispatcher: TaskDispatcher, session_id: str):
    """Return the active session for a context_id, or None."""
    user_ctx = dispatcher._session_registry.get(session_id)
    if user_ctx is None:
        return None
    return user_ctx.get_active_session()


def make_dispatcher(
    config,
    settings,
    replier,
    acp_registry=None,
    acl_manager=None,
) -> TaskDispatcher:
    feishu_client = make_feishu_client(replier)
    if acp_registry is None:
        acp_registry = ACPRuntimeRegistry()
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        acl_manager=acl_manager,
    )


# ---------------------------------------------------------------------------
# Test 1: Normal message full flow
# ---------------------------------------------------------------------------


async def test_normal_message_full_flow(tmp_project, settings):
    """dispatch("hello") → worker calls runtime.execute() → result card sent."""
    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="Hello from Claude!")

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = make_task("hello")
    await dispatcher.dispatch(task)

    # Retrieve the created session and drain the queue.
    session = _get_session(dispatcher, task.session_id)
    assert session is not None, "Session should have been created by dispatcher"

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Session task queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Assertions: runtime was called and result card was built.
    mock_runtime.execute.assert_called_once()
    replier.build_result_card.assert_called()


# ---------------------------------------------------------------------------
# Test 2: /help command does NOT call runtime
# ---------------------------------------------------------------------------


async def test_help_command_no_runtime(tmp_project, settings):
    """/help is a meta-command; it returns a help card without touching the runtime."""
    replier = make_replier()
    mock_runtime = make_mock_runtime()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = make_task("/help")
    await dispatcher.dispatch(task)

    # /help is handled immediately without enqueuing into a worker.
    # The help card should have been sent (via send_card or through build_help_card).
    assert replier.build_help_card.called or replier.send_card.called, (
        "Expected build_help_card or send_card to be called for /help"
    )
    mock_runtime.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: /new command resets session
# ---------------------------------------------------------------------------


async def test_new_command_resets_session(tmp_project, settings):
    """Dispatch a normal task, drain it, then dispatch /new — no crash."""
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    # /new uses acp_registry.get() (not get_or_create) to get the existing runtime
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)
    # acp_registry.get() returns None by default (no session stored), which is fine for /new

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    # Step 1: dispatch a normal task to create the session.
    task1 = make_task("do some work")
    await dispatcher.dispatch(task1)

    session = _get_session(dispatcher, task1.session_id)
    assert session is not None

    # Drain the first task so the worker is idle.
    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("First task queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Step 2: dispatch /new — should succeed without raising.
    task_new = make_task("/new")
    await dispatcher.dispatch(task_new)  # must not raise

    # Session still exists after /new (only the actual_id is cleared).
    session_after = _get_session(dispatcher, task1.session_id)
    assert session_after is not None, "/new should not destroy the UserContext"


# ---------------------------------------------------------------------------
# Test 4: Two tasks — both processed by the worker
# ---------------------------------------------------------------------------


async def test_second_task_queued(tmp_project, settings):
    """Dispatch two tasks to the same session; both get executed by the worker."""
    replier = make_replier()

    # First execute blocks briefly so the second task is guaranteed to queue.
    execute_count = 0

    async def slow_execute(task, on_progress, on_permission):
        nonlocal execute_count
        execute_count += 1
        await asyncio.sleep(0.05)
        return f"result-{execute_count}"

    mock_runtime = AsyncMock()
    mock_runtime.actual_id = "test-session-id"
    mock_runtime.is_running = True
    mock_runtime.ensure_ready = AsyncMock()
    mock_runtime.execute = slow_execute
    mock_runtime.stop = AsyncMock()
    mock_runtime.restore_session = AsyncMock()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task1 = make_task("task one")
    task2 = make_task("task two")

    await dispatcher.dispatch(task1)
    await dispatcher.dispatch(task2)

    session = _get_session(dispatcher, task1.session_id)
    assert session is not None

    try:
        await drain_session_queue(session, timeout=10.0)
    except asyncio.TimeoutError:
        pytest.fail("Both tasks were not processed within timeout")
    finally:
        await cancel_workers(dispatcher)

    assert execute_count == 2, f"Expected 2 executions, got {execute_count}"


# ---------------------------------------------------------------------------
# Test 5: ACL gate blocks unauthorized user
# ---------------------------------------------------------------------------


async def test_acl_gate_blocks_unauthorized_user(tmp_project, settings):
    """Unauthorized user (get_role returns None) gets a denied card, runtime not called."""
    replier = make_replier()
    mock_runtime = make_mock_runtime()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    # ACL manager that denies everyone.
    acl_manager = MagicMock()
    acl_manager.get_role = AsyncMock(return_value=None)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(
        config, settings, replier, acp_registry=acp_registry, acl_manager=acl_manager
    )

    task = make_task("do something")
    await dispatcher.dispatch(task)

    replier.build_access_denied_card.assert_called()
    mock_runtime.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: /whoami bypasses ACL gate
# ---------------------------------------------------------------------------


async def test_acl_gate_whoami_bypasses_auth(tmp_project, settings):
    """/whoami is an open command and must NOT trigger the access-denied card."""
    replier = make_replier()
    mock_runtime = make_mock_runtime()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    # ACL manager that denies everyone (including the caller).
    acl_manager = MagicMock()
    acl_manager.get_role = AsyncMock(return_value=None)
    # whoami calls get_role for displaying the role, tolerate that.
    acl_manager.get_admin_ids = MagicMock(return_value=[])

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(
        config, settings, replier, acp_registry=acp_registry, acl_manager=acl_manager
    )

    task = make_task("/whoami")
    await dispatcher.dispatch(task)

    replier.build_access_denied_card.assert_not_called()
    mock_runtime.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: No project configured → error text sent
# ---------------------------------------------------------------------------


async def test_no_project_sends_error(settings):
    """When no projects are configured and no binding exists, the dispatcher sends an error."""
    replier = make_replier()

    # Config with no projects.
    config = AppConfig(projects=[])  # default_project is None
    dispatcher = make_dispatcher(config, settings, replier)

    task = make_task("hello")
    await dispatcher.dispatch(task)

    # Dispatcher should have sent an error text message to the chat.
    replier.send_text.assert_called()
    error_call_args = replier.send_text.call_args
    # The error message should mention configuration.
    assert error_call_args is not None, "send_text should have been called with an error"


# ---------------------------------------------------------------------------
# Test 8: Group chat – unauthorized user gets text prompt in thread + DM
# ---------------------------------------------------------------------------


async def test_group_chat_unauthorized_gets_dm(tmp_project, settings):
    """Group chat: unauthorized user receives a plain text thread-reply and the
    apply card is sent as a private DM — no interactive card is posted to the
    group thread so other members cannot click apply on the user's behalf.
    """
    replier = make_replier()

    acl_manager = MagicMock()
    acl_manager.get_role = AsyncMock(return_value=None)  # everyone denied

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acl_manager=acl_manager)

    task = Task(
        id=str(uuid.uuid4()),
        content="do something",
        session_id="oc_group_abc:ou_stranger",
        reply_fn=AsyncMock(),
        message_id="om_group_msg",
        chat_type="group",
        timeout=timedelta(seconds=10),
    )
    await dispatcher.dispatch(task)

    # 1. Thread gets a plain-text prompt (no interactive card with buttons)
    replier.reply_text.assert_called_once()
    text_args = replier.reply_text.call_args
    assert text_args.kwargs.get("in_thread") is True, "prompt must be posted in thread"
    assert "私信" in text_args.args[1], "prompt must mention DM"

    # 2. Apply card sent as DM directly to the unauthorized user
    replier.send_to_user.assert_called_once()
    assert replier.send_to_user.call_args.args[0] == "ou_stranger"

    # 3. No card was posted to the group thread
    replier.reply_card.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: handle_acl_card_action – cross-user apply is rejected end-to-end
# ---------------------------------------------------------------------------


async def test_acl_apply_by_different_operator_is_ignored(tmp_project, settings):
    """handle_acl_card_action: when operator_id differs from open_id (i.e. someone
    clicked another user's apply button), no application is created and no DM is sent.
    """
    replier = make_replier()

    acl_manager = MagicMock()
    acl_manager.get_role = AsyncMock(return_value=None)
    acl_manager.create_application = AsyncMock(return_value=(1, None))

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acl_manager=acl_manager)

    action_data = {
        "action": "acl_apply",
        "open_id": "ou_applicant",      # the card's intended recipient
        "operator_id": "ou_clicker",    # a different group member who clicked
        "role": "collaborator",
    }
    await dispatcher.handle_acl_card_action(action_data)

    # No application must be created for the card's open_id
    acl_manager.create_application.assert_not_called()
    # No notification DM sent
    replier.send_to_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: Review notification card is disabled (update_card) after approve
# ---------------------------------------------------------------------------


async def test_review_card_disabled_after_approve(tmp_project, settings, acl_db):
    """Full apply → approve flow: the review notification card is updated
    (buttons removed) via update_card after the admin approves.

    Flow:
    1. Applicant triggers acl_apply → send_to_user sends notification to admin,
       returning a message_id that is stored in _review_notification_msgs.
    2. Admin approves via acl_review → update_card is called on that message_id.
    """
    replier = make_replier()
    # send_to_user returns a notification message_id the first time (reviewer notification)
    review_notification_msg_id = "om_review_notify_001"
    replier.send_to_user = AsyncMock(return_value=review_notification_msg_id)

    acl_manager = AclManager(db=acl_db, admin_users=["ou_admin"])
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acl_manager=acl_manager)

    # Step 1: applicant submits an access request
    await dispatcher.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "operator_id": "ou_applicant",
        "role": "collaborator",
    })

    # The dispatcher should have stored the review notification message_id
    assert len(dispatcher._review_notification_msgs) == 1, (
        "Expected one pending review notification after acl_apply"
    )

    # Step 2: admin approves the application
    app_id = next(iter(dispatcher._review_notification_msgs))
    await dispatcher.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_admin",
    })

    # update_card must have been called with the notification message_id
    replier.update_card.assert_called()
    updated_ids = [c.args[0] for c in replier.update_card.call_args_list]
    assert review_notification_msg_id in updated_ids, (
        f"Expected update_card to be called with {review_notification_msg_id!r}, got {updated_ids}"
    )

    # The pending entry should have been consumed
    assert app_id not in dispatcher._review_notification_msgs, (
        "Expected _review_notification_msgs entry to be removed after approval"
    )


# ---------------------------------------------------------------------------
# Test 11: Review notification card is disabled (update_card) after reject
# ---------------------------------------------------------------------------


async def test_review_card_disabled_after_reject(tmp_project, settings, acl_db):
    """Full apply → reject flow: the review notification card is updated
    (buttons removed) via update_card after the admin rejects.
    """
    replier = make_replier()
    review_notification_msg_id = "om_review_notify_002"
    replier.send_to_user = AsyncMock(return_value=review_notification_msg_id)

    acl_manager = AclManager(db=acl_db, admin_users=["ou_admin"])
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acl_manager=acl_manager)

    # Step 1: applicant submits an access request
    await dispatcher.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant2",
        "operator_id": "ou_applicant2",
        "role": "collaborator",
    })

    assert len(dispatcher._review_notification_msgs) == 1

    # Step 2: admin rejects
    app_id = next(iter(dispatcher._review_notification_msgs))
    await dispatcher.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })

    # update_card must have been called with the notification message_id
    replier.update_card.assert_called()
    updated_ids = [c.args[0] for c in replier.update_card.call_args_list]
    assert review_notification_msg_id in updated_ids, (
        f"Expected update_card to be called with {review_notification_msg_id!r}, got {updated_ids}"
    )

    # The pending entry should have been consumed
    assert app_id not in dispatcher._review_notification_msgs
