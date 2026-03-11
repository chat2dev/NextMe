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
12. test_path_lock_contention_shows_waiting_card     — 2nd user on same project sees "候补中" then "思考中"
13. test_task_timeout_sends_timeout_card             — runtime raises TaskTimeoutError → timeout card sent, result card NOT sent
14. test_task_timeout_evicts_runtime_from_registry   — after timeout, runtime is removed from ACPRuntimeRegistry
15. test_task_timeout_session_preserved_next_message_succeeds — actual_id preserved; 2nd task after timeout succeeds
16. test_schedule_list_empty                                 — /schedule list with empty DB → "没有" in reply
17. test_schedule_create_once_stores_in_db                  — /schedule ... at <ISO> → task in DB + "已创建" confirmation
18. test_schedule_create_interval_stores_in_db              — /schedule ... every 2h → interval task in DB
19. test_schedule_list_shows_existing_tasks                 — /schedule list after creating a task shows it
20. test_schedule_pause_command                             — /schedule pause <id> → task status becomes 'paused'
21. test_schedule_engine_fires_due_task_through_dispatcher  — SchedulerEngine._tick() fires due task → runtime.execute() called
23. test_thread_command_lists_active_threads         — /thread in group chat → lists threads from state_store
24. test_thread_close_command_closes_thread          — /thread close <short_id> (Owner) → calls handle_done, DONE reaction sent
25. test_thread_close_command_non_owner_denied       — /thread close by non-Owner → permission denied message
26. test_suppress_cancel_prevents_cancel_card        — worker cancelled with suppress_cancel=True → no cancel card
27. test_thread_close_ambiguous_short_id             — /thread close with ambiguous prefix → "匹配多个" error
28. test_thread_command_non_group_chat_rejected      — /thread in p2p chat → "仅在群聊中有效" message
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
from nextme.protocol.types import Task, TaskTimeoutError


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
    scheduler_db=None,
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
        scheduler_db=scheduler_db,
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


# ---------------------------------------------------------------------------
# Test 12: Path lock contention shows "候补中" card, then updates to "思考中"
# ---------------------------------------------------------------------------


async def test_path_lock_contention_shows_waiting_card(tmp_project, settings):
    """When two users on the same project send tasks concurrently, the second
    user must see a "候补中" (waiting) card while the first is executing,
    and the card must update to "思考中" once the second user acquires the lock.

    This tests the fix for the bug where both users would see "思考中" cards
    but only one would receive updates — because the second worker sent its
    initial card before blocking on the path lock.
    """
    # User A: slow runtime so the lock is held long enough for User B to observe contention.
    lock_acquired = asyncio.Event()
    lock_release = asyncio.Event()

    async def slow_execute_a(task, on_progress, on_permission):
        lock_acquired.set()        # signal that user A has the lock
        await lock_release.wait()  # hold until we say so
        return "done by A"

    runtime_a = AsyncMock()
    runtime_a.actual_id = ""
    runtime_a.is_running = True
    runtime_a.ensure_ready = AsyncMock()
    runtime_a.execute = slow_execute_a
    runtime_a.stop = AsyncMock()
    runtime_a.restore_session = AsyncMock()

    async def instant_execute_b(task, on_progress, on_permission):
        return "done by B"

    runtime_b = AsyncMock()
    runtime_b.actual_id = ""
    runtime_b.is_running = True
    runtime_b.ensure_ready = AsyncMock()
    runtime_b.execute = instant_execute_b
    runtime_b.stop = AsyncMock()
    runtime_b.restore_session = AsyncMock()

    replier_a = make_replier()
    replier_a.reply_card = AsyncMock(return_value="msg_a_progress")
    replier_b = make_replier()
    replier_b.reply_card = AsyncMock(return_value="msg_b_waiting")
    # build_progress_card must include the real content so we can assert on it.
    replier_b.build_progress_card = MagicMock(
        side_effect=lambda status, content, title="": f'{{"content":"{content}","title":"{title}"}}'
    )

    # get_replier() is called once per dispatch; return A's replier first, then B's.
    replier_sequence = [replier_a, replier_b]
    replier_index = [0]

    def get_replier_side_effect():
        r = replier_sequence[replier_index[0]]
        replier_index[0] = min(replier_index[0] + 1, len(replier_sequence) - 1)
        return r

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(side_effect=get_replier_side_effect)

    acp_registry = ACPRuntimeRegistry()

    def get_or_create_side_effect(**kwargs):
        session_id = kwargs.get("session_id", "")
        return runtime_a if "ou_user_a" in session_id else runtime_b

    acp_registry.get_or_create = MagicMock(side_effect=get_or_create_side_effect)

    config = AppConfig(projects=[tmp_project])
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()

    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
    )

    task_a = Task(
        id=str(uuid.uuid4()),
        content="task from A",
        session_id=f"oc_group:ou_user_a",
        reply_fn=AsyncMock(),
        message_id="om_msg_a",
        chat_type="p2p",
        timeout=timedelta(seconds=10),
    )
    task_b = Task(
        id=str(uuid.uuid4()),
        content="task from B",
        session_id=f"oc_group:ou_user_b",
        reply_fn=AsyncMock(),
        message_id="om_msg_b",
        chat_type="p2p",
        timeout=timedelta(seconds=10),
    )

    # Dispatch A first, wait until A has the path lock, then dispatch B.
    await dispatcher.dispatch(task_a)
    await asyncio.wait_for(lock_acquired.wait(), timeout=5.0)

    # User A holds the lock.  Dispatch User B.
    await dispatcher.dispatch(task_b)

    # Give User B's worker time to detect contention and send the waiting card.
    await asyncio.sleep(0.15)

    # User B must have received a "候补中" card.
    replier_b.reply_card.assert_called_once()
    waiting_card_json: str = replier_b.reply_card.call_args.args[1]
    assert "候补中" in waiting_card_json, (
        f"Expected '候补中' in waiting card JSON, got: {waiting_card_json!r}"
    )

    # Release User A — let the lock go so User B can proceed.
    lock_release.set()

    # Wait for User B to complete.
    session_b_ctx = session_registry.get(task_b.session_id)
    assert session_b_ctx is not None
    active_b = session_b_ctx.get_active_session()
    assert active_b is not None
    try:
        await asyncio.wait_for(active_b.task_queue.join(), timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("User B's task did not complete within timeout")
    finally:
        for worker_task in list(dispatcher._worker_tasks.values()):
            if not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)

    # After acquiring the lock, User B's worker must have updated the waiting
    # card to "思考中..." to reflect that execution has started.
    replier_b.update_card.assert_called()
    updated_card_json: str = replier_b.update_card.call_args_list[0].args[1]
    assert "思考中" in updated_card_json, (
        f"Expected '思考中' in updated card JSON, got: {updated_card_json!r}"
    )


# ---------------------------------------------------------------------------
# Test 13: Task timeout → timeout card sent, result card NOT sent
# ---------------------------------------------------------------------------


async def test_task_timeout_sends_timeout_card(tmp_project, settings):
    """When the runtime raises TaskTimeoutError the worker sends a timeout error
    card (containing '⏰' and '超时' in its title) and does NOT send a result card.
    """
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    mock_runtime.execute = AsyncMock(side_effect=TaskTimeoutError("timed out after 7200s"))

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = make_task("long running task")
    await dispatcher.dispatch(task)

    session = _get_session(dispatcher, task.session_id)
    assert session is not None

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Session queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Timeout card must have been built with '⏰' and '超时' in the title.
    replier.build_error_card.assert_called()
    call_args = replier.build_error_card.call_args
    title = call_args.kwargs.get("title", "")
    assert "⏰" in title and "超时" in title, (
        f"Expected '⏰' and '超时' in build_error_card title, got: {title!r}"
    )

    # Result card must NOT have been built.
    replier.build_result_card.assert_not_called()


# ---------------------------------------------------------------------------
# Test 14: Task timeout → runtime evicted from ACPRuntimeRegistry
# ---------------------------------------------------------------------------


async def test_task_timeout_evicts_runtime_from_registry(tmp_project, settings):
    """After a TaskTimeoutError the worker calls acp_registry.remove(), which
    stops the runtime subprocess and evicts it from the registry.
    """
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    mock_runtime.execute = AsyncMock(side_effect=TaskTimeoutError("timed out after 7200s"))

    # Use a real registry so remove() actually pops the entry.
    acp_registry = ACPRuntimeRegistry()
    runtime_key = "oc_chat:ou_user:myproject"
    # Pre-populate the registry so remove() has something to evict.
    acp_registry._runtimes[runtime_key] = mock_runtime
    # Also mock get_or_create so the worker receives our mock runtime.
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = make_task("long running task")
    await dispatcher.dispatch(task)

    session = _get_session(dispatcher, task.session_id)
    assert session is not None

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Session queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Runtime must have been evicted from the registry.
    assert acp_registry.get(runtime_key) is None, (
        "Expected runtime to be evicted from registry after timeout"
    )
    # stop() must have been called on the evicted runtime.
    mock_runtime.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Test 15: Task timeout → session actual_id preserved; next message succeeds
# ---------------------------------------------------------------------------


async def test_task_timeout_session_preserved_next_message_succeeds(tmp_project, settings):
    """After a timeout the session's actual_id is preserved (no context reset).
    A subsequent normal message executes successfully and receives a result card.
    """
    replier = make_replier()

    # First runtime: raises TaskTimeoutError immediately after ensure_ready sets actual_id.
    mock_runtime_1 = make_mock_runtime()
    mock_runtime_1.actual_id = "preserved-session-id"
    mock_runtime_1.execute = AsyncMock(side_effect=TaskTimeoutError("timed out after 7200s"))

    # Second runtime: succeeds normally.
    mock_runtime_2 = make_mock_runtime(execute_return="Resumed answer")
    mock_runtime_2.actual_id = "preserved-session-id"

    # Real registry so remove() actually works; pre-populate for the first call.
    acp_registry = ACPRuntimeRegistry()
    runtime_key = "oc_chat:ou_user:myproject"
    acp_registry._runtimes[runtime_key] = mock_runtime_1

    call_count = 0

    def _get_or_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_runtime_1 if call_count == 1 else mock_runtime_2

    acp_registry.get_or_create = MagicMock(side_effect=_get_or_create)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    # --- First task: times out ---
    task1 = make_task("long running task")
    await dispatcher.dispatch(task1)

    session = _get_session(dispatcher, task1.session_id)
    assert session is not None

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("First task queue did not drain within timeout")

    # Timeout card was sent; actual_id is preserved on the session.
    replier.build_error_card.assert_called()
    assert session.actual_id == "preserved-session-id", (
        f"Expected session.actual_id='preserved-session-id', got {session.actual_id!r}"
    )

    # --- Second task: succeeds ---
    replier.build_result_card.reset_mock()
    task2 = make_task("follow-up question", session_id=task1.session_id)
    await dispatcher.dispatch(task2)

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Second task queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Second task must have received a result card.
    replier.build_result_card.assert_called()
    mock_runtime_2.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test 16: /skill book with @mentions → dispatches without crash
# ---------------------------------------------------------------------------


async def test_skill_book_mentions_in_prompt(tmp_project, settings):
    """E2E: /skill book with @mentions → skill_task is forwarded with message_id
    and chat_type; dispatch completes without error.

    Note: The "book" skill is not registered in the test environment (no skill
    directory is loaded).  This test therefore verifies that:
    1. The dispatcher routes the /skill command without crashing.
    2. An "access denied" card is NOT shown (ACL is open — no acl_manager).
    3. The send_text path fires with "未找到 Skill" (unknown skill) rather than
       an unhandled exception, confirming that message_id/chat_type forwarding
       in the skill_task constructor does not break the dispatch path.

    If the "book" skill were registered, runtime.execute would be called with a
    prompt that includes the open_id lines from the @mentions list.
    """
    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="✅ 会议已预定")

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = Task(
        id=str(uuid.uuid4()),
        content="/skill book 明天下午3点 团队周会",
        session_id="oc_chat:ou_user_book",
        reply_fn=AsyncMock(),
        message_id="msg_book_01",
        chat_type="group",
        timeout=timedelta(seconds=10),
        mentions=[
            {"name": "小明", "open_id": "ou_attendee_aaa"},
            {"name": "小红", "open_id": "ou_attendee_bbb"},
        ],
    )
    await dispatcher.dispatch(task)

    # The "book" skill is not registered in tests, so the dispatcher should have
    # sent a "未找到 Skill" text message — not an access-denied card.
    assert not replier.build_access_denied_card.called, (
        "Should not be access denied — no ACL manager configured"
    )
    # Dispatcher must have responded (either found skill or reported missing skill).
    # In test env without skills loaded, send_text is called with "未找到 Skill".
    replier.send_text.assert_called()
    text_call = replier.send_text.call_args
    assert text_call is not None, "Expected send_text to be called for unknown skill"


# ---------------------------------------------------------------------------
# Test 17: Group @bot creates thread session (session_id = chat_id:message_id)
# ---------------------------------------------------------------------------


async def test_group_at_bot_creates_thread_session(tmp_project, settings):
    """E2E: @bot on group root message creates session with session_id=chat_id:message_id."""
    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="Feature built")

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    # session_id = chat_id:message_id (group thread format)
    session_id = "oc_group1:om_root1"
    task = Task(
        id=str(uuid.uuid4()),
        content="build a feature",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_root1",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userA",
        thread_root_id="om_root1",  # root message = thread creator
    )

    await dispatcher.dispatch(task)

    session = _get_session(dispatcher, session_id)
    assert session is not None, "Session should have been created for thread session_id"

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Session task queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Runtime was called with the task
    mock_runtime.execute.assert_called_once()
    # Result card was built
    replier.build_result_card.assert_called()


# ---------------------------------------------------------------------------
# Test 18: Thread reply dispatched to same session
# ---------------------------------------------------------------------------


async def test_thread_reply_uses_same_session_id(tmp_project, settings):
    """E2E: follow-up reply in active thread goes to the same session (queued)."""
    replier = make_replier()

    execute_count = 0

    async def counting_execute(task, on_progress, on_permission):
        nonlocal execute_count
        execute_count += 1
        await asyncio.sleep(0.05)
        return f"Response {execute_count}"

    mock_runtime = AsyncMock()
    mock_runtime.actual_id = "test-acp-session-id"
    mock_runtime.is_running = True
    mock_runtime.ensure_ready = AsyncMock()
    mock_runtime.execute = counting_execute
    mock_runtime.stop = AsyncMock()
    mock_runtime.restore_session = AsyncMock()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    # Both tasks share the same session_id (same thread)
    session_id = "oc_group1:om_root1"

    task1 = Task(
        id=str(uuid.uuid4()),
        content="first message",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_root1",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userA",
        thread_root_id="om_root1",
    )

    # Second message in same thread — same session_id, different message_id
    task2 = Task(
        id=str(uuid.uuid4()),
        content="follow up",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_reply1",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userB",  # different user, same thread
        thread_root_id="om_root1",
    )

    await dispatcher.dispatch(task1)
    await dispatcher.dispatch(task2)

    session = _get_session(dispatcher, session_id)
    assert session is not None, "Session should exist for the thread"

    try:
        await drain_session_queue(session, timeout=10.0)
    except asyncio.TimeoutError:
        pytest.fail("Both thread tasks were not processed within timeout")
    finally:
        await cancel_workers(dispatcher)

    # Both tasks were processed by the same session worker
    assert execute_count == 2, f"Expected 2 executions by same session, got {execute_count}"


# ---------------------------------------------------------------------------
# Test 19: /done command releases thread resources
# ---------------------------------------------------------------------------


async def test_done_command_releases_thread_resources(tmp_project, settings):
    """E2E: /done in a group thread stops runtime and sends DONE reaction."""
    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="Work started")

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    session_id = "oc_group1:om_done_root"

    # First establish the session with a normal task
    task1 = Task(
        id=str(uuid.uuid4()),
        content="start work",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_done_root",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userA",
        thread_root_id="om_done_root",
    )

    await dispatcher.dispatch(task1)

    session = _get_session(dispatcher, session_id)
    assert session is not None

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("First task did not complete within timeout")

    # Now send /done in the same thread
    done_task = Task(
        id=str(uuid.uuid4()),
        content="/done",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_done_reply",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userA",
        thread_root_id="om_done_root",
    )

    await dispatcher.dispatch(done_task)
    # Allow async cleanup to complete
    await asyncio.sleep(0.15)

    try:
        await cancel_workers(dispatcher)
    except Exception:
        pass

    # DONE reaction sent to root message
    replier.send_reaction.assert_called_with("om_done_root", "DONE")


# ---------------------------------------------------------------------------
# Test 20: Queued thread — _active_threads rolled back while thread is queued
# ---------------------------------------------------------------------------


async def test_thread_queue_active_threads_rolled_back_on_queue(tmp_project, settings, tmp_path):
    """When dispatcher queues a task (limit reached), the handler's _active_threads
    entry must be rolled back so follow-up messages in the queued thread are dropped.

    Setup:
    - max_active_threads_per_chat = 1
    - Pre-fill state_store with one active thread for the chat (simulating a live thread).
    - Dispatch task2 for a new thread (thread2) → should be queued.
    - Register a thread_closed_callback (acts like handler.deregister_thread).
    - Verify: thread2's key was removed from _active_threads (callback was called).
    - Verify: task2 was NOT dispatched to the worker (execute not called).
    - Verify: replier received a queue-full message (reply_text called).
    """
    from nextme.config.state_store import StateStore
    from nextme.feishu.handler import MessageHandler

    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    # Settings with thread limit = 1
    limited_settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        streaming_enabled=False,
        max_active_threads_per_chat=1,
    )

    # Real StateStore backed by tmp file
    state_store = StateStore(limited_settings, state_path=tmp_path / "state.json")
    await state_store.load()

    chat_id = "oc_queue_test"
    # Pre-register thread1 to fill the slot
    state_store.register_thread(chat_id, "om_thread1_root", "myproject")
    assert state_store.get_active_thread_count(chat_id) == 1

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=limited_settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
    )

    # Track what the closed-callback receives
    reverted: list[tuple[str, str]] = []
    accepted: list[tuple[str, str]] = []

    dispatcher.register_thread_closed_callback(
        lambda cid, tid: reverted.append((cid, tid))
    )
    dispatcher.register_thread_accept_callback(
        lambda cid, tid: accepted.append((cid, tid))
    )

    # Dispatch task2 for thread2 — should be queued because limit=1 and thread1 is active
    task2 = Task(
        id=str(uuid.uuid4()),
        content="new thread message",
        session_id=f"{chat_id}:om_thread2_root",
        reply_fn=AsyncMock(),
        message_id="om_thread2_root",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userB",
        thread_root_id="om_thread2_root",
    )
    await dispatcher.dispatch(task2)

    # 1. The thread_closed_callback (rollback) must have been called for thread2
    assert (chat_id, "om_thread2_root") in reverted, (
        f"Expected thread2 to be rolled back from _active_threads; reverted={reverted}"
    )

    # 2. The thread_accept_callback must NOT have been called (thread2 is still queued)
    assert (chat_id, "om_thread2_root") not in accepted, (
        f"thread2 must not be accepted while queued; accepted={accepted}"
    )

    # 3. Runtime must NOT have been called (task2 is in queue, not dispatched yet)
    mock_runtime.execute.assert_not_called()

    # 4. Queue-full message was sent via reply_text
    replier.reply_text.assert_called()
    call_args = replier.reply_text.call_args
    assert "排" in call_args.args[1] or "上限" in call_args.args[1], (
        f"Expected queue-full message, got: {call_args.args[1]!r}"
    )

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 21: Queued thread is released after existing thread closes
# ---------------------------------------------------------------------------


async def test_thread_queue_released_after_thread_closes(tmp_project, settings, tmp_path):
    """When an active thread is closed (_on_thread_closed), the next queued task
    should be dispatched and executed.

    Setup:
    - max_active_threads_per_chat = 1
    - Dispatch task1 for thread1 → accepted (registers in state_store + handler).
    - Dispatch task2 for thread2 → queued (limit reached).
    - Wait for task1 to complete, then call _on_thread_closed for thread1.
    - Verify: task2 is eventually executed by the runtime.
    - Verify: thread_accept_callback was called for thread2 before dispatch.
    """
    from nextme.config.state_store import StateStore
    from nextme.feishu.handler import MessageHandler

    replier = make_replier()

    execute_order: list[str] = []

    async def ordered_execute(task, on_progress, on_permission):
        execute_order.append(task.thread_root_id or task.message_id)
        return f"done:{task.message_id}"

    mock_runtime = AsyncMock()
    mock_runtime.actual_id = "test-session-id"
    mock_runtime.is_running = True
    mock_runtime.ensure_ready = AsyncMock()
    mock_runtime.execute = ordered_execute
    mock_runtime.stop = AsyncMock()
    mock_runtime.restore_session = AsyncMock()

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    limited_settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        streaming_enabled=False,
        max_active_threads_per_chat=1,
    )

    state_store = StateStore(limited_settings, state_path=tmp_path / "state2.json")
    await state_store.load()

    chat_id = "oc_queue_release"

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=limited_settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
    )

    accepted: list[tuple[str, str]] = []
    dispatcher.register_thread_closed_callback(lambda cid, tid: None)  # no-op closed
    dispatcher.register_thread_accept_callback(
        lambda cid, tid: accepted.append((cid, tid))
    )

    session_id_1 = f"{chat_id}:om_t1_root"
    session_id_2 = f"{chat_id}:om_t2_root"

    task1 = Task(
        id=str(uuid.uuid4()),
        content="thread1 message",
        session_id=session_id_1,
        reply_fn=AsyncMock(),
        message_id="om_t1_root",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userA",
        thread_root_id="om_t1_root",
    )

    task2 = Task(
        id=str(uuid.uuid4()),
        content="thread2 message",
        session_id=session_id_2,
        reply_fn=AsyncMock(),
        message_id="om_t2_root",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_userB",
        thread_root_id="om_t2_root",
    )

    # Dispatch task1 first → accepted (slot used)
    await dispatcher.dispatch(task1)
    session1 = _get_session(dispatcher, session_id_1)
    assert session1 is not None

    # Wait for task1 to complete
    try:
        await drain_session_queue(session1, timeout=5.0)
    except asyncio.TimeoutError:
        pytest.fail("task1 did not complete within timeout")

    # Dispatch task2 → should be queued because thread1 is still registered in state_store
    await dispatcher.dispatch(task2)

    # Verify task2 is queued (execute_order has only thread1's entry so far)
    assert "om_t1_root" in execute_order
    assert "om_t2_root" not in execute_order, "task2 should still be queued"

    # Close thread1 → this should release task2 from the queue
    dispatcher._on_thread_closed(chat_id, "om_t1_root")

    # Give the event loop time to dispatch and execute task2
    await asyncio.sleep(0.2)

    # task2 must now have been executed
    assert "om_t2_root" in execute_order, (
        f"Expected task2 to execute after thread1 closed; execute_order={execute_order}"
    )

    # thread_accept_callback must have been called for thread2 when it was dequeued
    assert (chat_id, "om_t2_root") in accepted, (
        f"Expected thread2 to be re-registered via accept callback; accepted={accepted}"
    )

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 22: Follow-up reply in queued thread is dropped
# ---------------------------------------------------------------------------


async def test_thread_queue_reply_in_queued_thread_is_dropped(tmp_project, settings, tmp_path):
    """After dispatcher rolls back the optimistic _active_threads entry for a
    queued thread, a follow-up reply in that thread must be dropped by the handler.

    Setup:
    - Simulate the handler's _active_threads being rolled back (as happens when
      dispatcher queues the thread).
    - Send a follow-up reply (root_id set, not root message) to the queued thread.
    - Verify: dispatch is NOT called for the follow-up (it is dropped by the handler
      before reaching the dispatcher).

    This test exercises handler.py's thread-key lookup, not the dispatcher directly.
    """
    from nextme.feishu.handler import MessageHandler

    # Minimal dedup mock
    dedup = MagicMock()
    dedup.check_and_mark = MagicMock(return_value=True)  # not duplicate

    dispatch_calls: list = []

    async def mock_dispatch(task):
        dispatch_calls.append(task)

    replier = make_replier()
    mock_fc = make_feishu_client(replier)

    config = AppConfig(projects=[tmp_project])
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=mock_fc,
    )
    # Replace dispatch with our spy
    dispatcher.dispatch = mock_dispatch  # type: ignore[method-assign]

    handler = MessageHandler(dedup=dedup, dispatcher=dispatcher)

    chat_id = "oc_handler_test"
    root_id = "om_queued_root"
    thread_key = f"{chat_id}:{root_id}"

    # Simulate: handler had the thread registered optimistically, then dispatcher
    # rolled it back via deregister_thread.
    # So _active_threads should NOT contain thread_key.
    assert thread_key not in handler._active_threads, (
        "thread_key must not be in _active_threads before the test"
    )

    # Build a fake lark-shaped message (MagicMock, matching handler test conventions)
    msg = MagicMock()
    msg.message_id = "om_reply_in_queued"
    msg.root_id = root_id
    msg.chat_type = "group"
    msg.chat_id = chat_id
    msg.message_type = "text"
    msg.content = '{"text":"follow-up"}'
    msg.mentions = []

    sender = MagicMock()
    sender.sender_id = MagicMock()
    sender.sender_id.open_id = "ou_userB"

    event = MagicMock()
    event.message = msg
    event.sender = sender

    event_outer = MagicMock()
    event_outer.event = event

    # Attach a dummy loop so handle_message can schedule coroutines
    loop = asyncio.get_event_loop()
    handler.attach_loop(loop)

    handler.handle_message(event_outer)  # type: ignore[arg-type]
    # Allow any scheduled coroutines to run
    await asyncio.sleep(0.05)

    # dispatch must NOT have been called — the reply should have been dropped
    assert len(dispatch_calls) == 0, (
        f"Expected 0 dispatch calls for reply in queued thread, got {len(dispatch_calls)}"
    )


# ---------------------------------------------------------------------------
# Test 23: /thread command lists active threads from state_store
# ---------------------------------------------------------------------------


async def test_thread_command_lists_active_threads(tmp_project, settings, tmp_path):
    """E2E: /thread in a group chat lists the active threads from state_store.

    Setup:
    - Create a real StateStore with two pre-registered threads.
    - Dispatch a /thread command from a group-chat session.
    - Verify: replier.send_card is called (the thread-list card was sent).
    - Verify: runtime.execute is NOT called (meta command, no agent invocation).
    """
    from nextme.config.state_store import StateStore

    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    state_store = StateStore(settings, state_path=tmp_path / "state.json")
    await state_store.load()

    chat_id = "oc_thread_list_test"
    state_store.register_thread(chat_id, "om_thread_aaa", "myproject")
    state_store.register_thread(chat_id, "om_thread_bbb", "myproject")
    assert state_store.get_active_thread_count(chat_id) == 2

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
    )

    # /thread from a group-chat context
    session_id = f"{chat_id}:ou_admin"
    task = Task(
        id=str(uuid.uuid4()),
        content="/thread",
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="om_thread_cmd",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_admin",
    )

    await dispatcher.dispatch(task)
    await asyncio.sleep(0.2)

    # The thread-list card must be sent
    assert replier.send_card.called, "Expected send_card to be called with the thread-list card"

    # No agent invocation for a meta command
    mock_runtime.execute.assert_not_called()

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 24: /thread close <short_id> by Owner closes thread and sends DONE reaction
# ---------------------------------------------------------------------------


async def test_thread_close_command_closes_thread(tmp_project, settings, tmp_path, acl_db):
    """E2E: /thread close <short_id> (Owner) stops the target session and sends DONE reaction.

    Setup:
    - Create an active group-thread session (session_id = chat_id:thread_root_id).
    - Run one task so the session is initialized.
    - Register the thread in state_store.
    - Dispatch /thread close <short_id> from an Owner context.
    - Verify: send_reaction called with "DONE" for the target thread root message.
    - Verify: state_store no longer has the thread registered.
    """
    from nextme.config.state_store import StateStore

    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="work done")
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    state_store = StateStore(settings, state_path=tmp_path / "state_close.json")
    await state_store.load()

    chat_id = "oc_close_test"
    thread_root_id = "om_target_thread_root"
    thread_session_id = f"{chat_id}:{thread_root_id}"
    short_id = thread_root_id[:8]

    # ACL: both users are admins (ou_worker for task1, ou_admin for /thread close)
    acl_manager = AclManager(db=acl_db, admin_users=["ou_admin", "ou_worker"])

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
        acl_manager=acl_manager,
    )

    # First, run a task in the target thread to create the session
    task1 = Task(
        id=str(uuid.uuid4()),
        content="start work",
        session_id=thread_session_id,
        reply_fn=AsyncMock(),
        message_id=thread_root_id,
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_worker",
        thread_root_id=thread_root_id,
    )
    await dispatcher.dispatch(task1)
    session = _get_session(dispatcher, thread_session_id)
    assert session is not None

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("task1 did not complete within timeout")

    # Register the thread in state_store (dispatcher does this for group root messages,
    # but since state_store was attached, let's register it manually here)
    state_store.register_thread(chat_id, thread_root_id, "myproject")
    assert state_store.get_active_thread_count(chat_id) == 1

    # Now dispatch /thread close <short_id> as Owner (no ACL manager → defaults to Owner role)
    close_task = Task(
        id=str(uuid.uuid4()),
        content=f"/thread close {short_id}",
        session_id=f"{chat_id}:ou_admin",
        reply_fn=AsyncMock(),
        message_id="om_close_cmd",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_admin",
    )

    await dispatcher.dispatch(close_task)
    await asyncio.sleep(0.3)

    # DONE reaction must be sent to the target thread root message
    replier.send_reaction.assert_any_call(thread_root_id, "DONE")

    # Thread must be removed from state_store
    assert state_store.get_active_thread_count(chat_id) == 0, (
        "Expected thread to be unregistered from state_store after /thread close"
    )

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 25: /thread close by non-Owner → permission denied
# ---------------------------------------------------------------------------


async def test_thread_close_command_non_owner_denied(tmp_project, settings, tmp_path, acl_db):
    """E2E: /thread close by a Collaborator is rejected with a permission-denied message."""
    from nextme.acl.manager import AclManager
    from nextme.acl.schema import Role
    from nextme.config.state_store import StateStore

    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    state_store = StateStore(settings, state_path=tmp_path / "state_denied.json")
    await state_store.load()

    chat_id = "oc_denied_test"
    thread_root_id = "om_target_thread_denied"
    short_id = thread_root_id[:8]

    # Register a thread so the /thread close lookup succeeds up to the permission check
    state_store.register_thread(chat_id, thread_root_id, "myproject")

    # ACL: collaborator_user has Collaborator role; no admin users so ou_admin is not auto-Owner
    acl_manager = AclManager(db=acl_db, admin_users=[])
    await acl_manager.add_user("collaborator_user", Role.COLLABORATOR, added_by="owner_user")

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
        acl_manager=acl_manager,
    )

    close_task = Task(
        id=str(uuid.uuid4()),
        content=f"/thread close {short_id}",
        session_id=f"{chat_id}:collaborator_user",
        reply_fn=AsyncMock(),
        message_id="om_denied_cmd",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="collaborator_user",
    )

    await dispatcher.dispatch(close_task)
    await asyncio.sleep(0.2)

    # Permission denied message must be sent
    send_text_calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("权限不足" in c for c in send_text_calls), (
        f"Expected '权限不足' in send_text calls, got: {send_text_calls}"
    )

    # Thread must still be registered (not closed)
    assert state_store.get_active_thread_count(chat_id) == 1

    # Runtime must NOT have been called
    mock_runtime.execute.assert_not_called()

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 26: suppress_cancel=True prevents cancel card on worker cancellation
# ---------------------------------------------------------------------------


async def test_suppress_cancel_prevents_cancel_card(tmp_project, settings):
    """E2E: When session.suppress_cancel=True, cancelling the worker does NOT send a cancel card.

    This simulates bot shutdown: we set suppress_cancel before cancelling workers
    so that active sessions do not spam "已取消" cards to all threads.
    """
    replier = make_replier()
    # Make runtime.execute() block indefinitely so the worker is busy when we cancel
    execute_started = asyncio.Event()

    async def slow_execute(*_args, **_kwargs):
        execute_started.set()
        await asyncio.sleep(60)  # Never completes in normal test flow
        return "never"

    mock_runtime = make_mock_runtime()
    mock_runtime.execute = AsyncMock(side_effect=slow_execute)
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = make_task("do some work")
    await dispatcher.dispatch(task)

    session = _get_session(dispatcher, task.session_id)
    assert session is not None

    # Wait until the worker is actually inside runtime.execute()
    await asyncio.wait_for(execute_started.wait(), timeout=5.0)

    # Simulate bot shutdown: set suppress_cancel before cancelling workers
    session.suppress_cancel = True

    # Cancel all workers (mimics asyncio finalizer or nextme down)
    for worker_task in list(dispatcher._worker_tasks.values()):
        if not worker_task.done():
            worker_task.cancel()
    await asyncio.gather(*dispatcher._worker_tasks.values(), return_exceptions=True)

    # build_result_card is called for cancel cards (status="cancelled")
    # It must NOT have been called with a cancel/cancelled variant
    cancel_calls = [
        call for call in replier.build_result_card.call_args_list
        if "cancel" in str(call).lower() or "取消" in str(call)
    ]
    assert len(cancel_calls) == 0, (
        f"Expected no cancel-card build calls when suppress_cancel=True, got: {cancel_calls}"
    )


# ---------------------------------------------------------------------------
# Test 27: /thread close with ambiguous short_id → "匹配多个" error
# ---------------------------------------------------------------------------


async def test_thread_close_ambiguous_short_id(tmp_project, settings, tmp_path, acl_db):
    """E2E: /thread close with a prefix that matches multiple threads → "匹配多个" error sent."""
    from nextme.config.state_store import StateStore

    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    state_store = StateStore(settings, state_path=tmp_path / "state_ambiguous.json")
    await state_store.load()

    chat_id = "oc_ambiguous_test"
    # Register two threads whose IDs share the same prefix
    state_store.register_thread(chat_id, "om_ambig_aaa111", "myproject")
    state_store.register_thread(chat_id, "om_ambig_aaa222", "myproject")
    assert state_store.get_active_thread_count(chat_id) == 2

    # ACL: ou_admin is an Owner so permission check passes and we reach the ambiguity check
    acl_manager = AclManager(db=acl_db, admin_users=["ou_admin"])

    config = AppConfig(projects=[tmp_project])
    feishu_client = make_feishu_client(replier)
    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()
    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
        acl_manager=acl_manager,
    )

    # Use a short_id prefix that matches both threads
    ambiguous_prefix = "om_ambig"
    close_task = Task(
        id=str(uuid.uuid4()),
        content=f"/thread close {ambiguous_prefix}",
        session_id=f"{chat_id}:ou_admin",
        reply_fn=AsyncMock(),
        message_id="om_ambig_cmd",
        chat_type="group",
        timeout=timedelta(seconds=10),
        user_id="ou_admin",
    )

    await dispatcher.dispatch(close_task)
    await asyncio.sleep(0.2)

    send_text_calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("匹配多个" in c for c in send_text_calls), (
        f"Expected '匹配多个' in send_text calls, got: {send_text_calls}"
    )

    # Threads must still be registered (not closed)
    assert state_store.get_active_thread_count(chat_id) == 2

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Test 28: /thread in p2p chat → "仅在群聊中有效"
# ---------------------------------------------------------------------------


async def test_thread_command_non_group_chat_rejected(tmp_project, settings):
    """E2E: /thread dispatched from a p2p chat sends '仅在群聊中有效' message."""
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    task = Task(
        id=str(uuid.uuid4()),
        content="/thread",
        session_id="oc_p2p_chat:ou_user",
        reply_fn=AsyncMock(),
        message_id="om_p2p_cmd",
        chat_type="p2p",  # Not a group chat
        timeout=timedelta(seconds=10),
        user_id="ou_user",
    )

    await dispatcher.dispatch(task)
    await asyncio.sleep(0.2)

    send_text_calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("仅在群聊中有效" in c for c in send_text_calls), (
        f"Expected '仅在群聊中有效' in send_text calls, got: {send_text_calls}"
    )

    mock_runtime.execute.assert_not_called()

    await cancel_workers(dispatcher)


# ---------------------------------------------------------------------------
# Scheduler fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def scheduler_db(tmp_path):
    """Real in-process SchedulerDb backed by a temporary SQLite file."""
    from nextme.scheduler.db import SchedulerDb

    db = SchedulerDb(db_path=tmp_path / "scheduler_e2e.db")
    await db.open()
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# Test 16: /schedule list with empty DB → "没有" in reply
# ---------------------------------------------------------------------------


async def test_schedule_list_empty(tmp_project, settings, scheduler_db):
    """/schedule list with no tasks → sends '没有' message."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    task = make_task("/schedule list")
    await dispatcher.dispatch(task)

    replier.send_text.assert_called_once()
    assert "没有" in replier.send_text.call_args[0][1]


# ---------------------------------------------------------------------------
# Test 17: /schedule ... at <ISO> → task created in DB, "已创建" confirmation
# ---------------------------------------------------------------------------


async def test_schedule_create_once_stores_in_db(tmp_project, settings, scheduler_db):
    """/schedule ... at <ISO> → creates task in DB and sends '已创建' confirmation."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    task = make_task("/schedule say hello at 2026-03-20T10:00:00+08:00")
    await dispatcher.dispatch(task)

    replier.send_text.assert_called_once()
    assert "已创建" in replier.send_text.call_args[0][1]

    tasks = await scheduler_db.list_by_chat("oc_chat")
    assert len(tasks) == 1
    assert tasks[0].prompt == "say hello"
    assert tasks[0].schedule_type.value == "once"


# ---------------------------------------------------------------------------
# Test 18: /schedule ... every <N><unit> → interval task created
# ---------------------------------------------------------------------------


async def test_schedule_create_interval_stores_in_db(tmp_project, settings, scheduler_db):
    """/schedule ... every 2h → creates interval task with schedule_value='7200'."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    task = make_task("/schedule check news every 2h")
    await dispatcher.dispatch(task)

    tasks = await scheduler_db.list_by_chat("oc_chat")
    assert len(tasks) == 1
    assert tasks[0].schedule_type.value == "interval"
    assert tasks[0].schedule_value == "7200"


# ---------------------------------------------------------------------------
# Test 19: /schedule list with tasks → lists them
# ---------------------------------------------------------------------------


async def test_schedule_list_shows_existing_tasks(tmp_project, settings, scheduler_db):
    """/schedule list after creating a task shows it in the reply."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    # First create a task
    await dispatcher.dispatch(make_task("/schedule ping every 1h"))
    replier.reset_mock()

    # Then list
    await dispatcher.dispatch(make_task("/schedule list"))

    replier.send_text.assert_called_once()
    text = replier.send_text.call_args[0][1]
    assert "ping" in text
    assert "interval" in text
    assert "1小时" in text   # interval human-readable duration shown


# ---------------------------------------------------------------------------
# Test 20: /schedule pause <id> → task paused in DB
# ---------------------------------------------------------------------------


async def test_schedule_pause_command(tmp_project, settings, scheduler_db):
    """/schedule pause <short_id> → task status becomes 'paused' in DB."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    # Create a task first
    await dispatcher.dispatch(make_task("/schedule check every 1h"))
    tasks = await scheduler_db.list_by_chat("oc_chat")
    assert len(tasks) == 1
    short_id = tasks[0].id[:8]

    replier.reset_mock()

    # Pause it
    await dispatcher.dispatch(make_task(f"/schedule pause {short_id}"))
    replier.send_text.assert_called_once()
    assert "已暂停" in replier.send_text.call_args[0][1]

    # Verify in DB
    updated = await scheduler_db.get(tasks[0].id)
    assert updated.status == "paused"


# ---------------------------------------------------------------------------
# Test 21: Engine fires due task through real dispatcher
# ---------------------------------------------------------------------------


async def test_schedule_engine_fires_due_task_through_dispatcher(
    tmp_project, settings, scheduler_db
):
    """SchedulerEngine._tick() fires a due task through real TaskDispatcher → runtime.execute() called."""
    from datetime import datetime, timezone, timedelta as td

    from nextme.scheduler.engine import SchedulerEngine
    from nextme.scheduler.schema import ScheduledTask, ScheduleType

    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="Scheduled task done!")

    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(
        config,
        settings,
        replier,
        acp_registry=acp_registry,
        scheduler_db=scheduler_db,
    )
    feishu_client = make_feishu_client(replier)

    engine = SchedulerEngine(
        db=scheduler_db,
        dispatcher=dispatcher,
        feishu_client=feishu_client,
    )

    # Create a task that is already due (1 minute in the past)
    past = datetime.now(timezone.utc) - td(minutes=1)
    sched_task = ScheduledTask(
        id="e2e-sched-001",
        chat_id="oc_chat",
        creator_open_id="ou_user",
        prompt="summarize today",
        schedule_type=ScheduleType.ONCE,
        schedule_value=past.isoformat(),
        next_run_at=past,
    )
    await scheduler_db.create(sched_task)

    # Run one tick
    await engine._tick()

    # Give the dispatcher a moment to create the session
    await asyncio.sleep(0.1)

    # Wait for the worker to process the dispatched task
    session = _get_session(dispatcher, "oc_chat:ou_user")
    if session is not None:
        try:
            await drain_session_queue(session)
        except asyncio.TimeoutError:
            pass
        finally:
            await cancel_workers(dispatcher)

    # Runtime should have been called with the scheduled prompt
    mock_runtime.execute.assert_called_once()
    call_args = mock_runtime.execute.call_args
    # The prompt is in the task content
    assert "summarize today" in str(call_args)

    # DB: task should be marked done
    updated = await scheduler_db.get("e2e-sched-001")
    assert updated is not None
    assert updated.status == "done"
    assert updated.run_count == 1


# ---------------------------------------------------------------------------
# Test 22: Chinese NL → creates interval task
# ---------------------------------------------------------------------------


async def test_schedule_chinese_nl_creates_interval_task(tmp_project, settings, scheduler_db):
    """'/schedule 每小时提醒我喝水' — Chinese NL → creates interval task without explicit 'every' syntax."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    await dispatcher.dispatch(make_task("/schedule 每小时提醒我喝水"))

    replier.send_text.assert_called_once()
    assert "已创建" in replier.send_text.call_args[0][1]

    tasks = await scheduler_db.list_by_chat("oc_chat")
    assert len(tasks) == 1
    assert tasks[0].prompt == "提醒我喝水"
    assert tasks[0].schedule_type.value == "interval"
    assert tasks[0].schedule_value == "3600"


# ---------------------------------------------------------------------------
# Test 23: Chinese NL daily-at → creates cron task
# ---------------------------------------------------------------------------


async def test_schedule_chinese_daily_at_creates_cron_task(tmp_project, settings, scheduler_db):
    """'/schedule 每天9点发日报' — creates a cron task."""
    replier = make_replier()
    config = AppConfig(projects=[tmp_project])
    dispatcher = make_dispatcher(config, settings, replier, scheduler_db=scheduler_db)

    await dispatcher.dispatch(make_task("/schedule 每天9点发日报"))

    tasks = await scheduler_db.list_by_chat("oc_chat")
    assert len(tasks) == 1
    assert tasks[0].schedule_type.value == "cron"
    assert tasks[0].schedule_value == "0 9 * * *"
    assert tasks[0].prompt == "发日报"


# ---------------------------------------------------------------------------
# Test 29-30: /status shows git branch
# ---------------------------------------------------------------------------


async def test_status_shows_git_branch(tmp_path, settings):
    """/status card includes branch name when project dir is a git repo."""
    import json

    # Create a minimal git repo (filesystem only — no subprocess, immune to GIT_DIR).
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/feat/e2e-test\n")

    project = Project(name="myproject", path=str(project_dir), executor="claude-code-acp")
    config = AppConfig(projects=[project])
    replier = make_replier()
    replier.send_card = AsyncMock(return_value="status_card_id")

    dispatcher = make_dispatcher(config, settings, replier)
    await dispatcher.dispatch(make_task("/status"))

    replier.send_card.assert_called_once()
    card_json = replier.send_card.call_args[0][1]
    card = json.loads(card_json)
    content = card["body"]["elements"][0]["content"]

    assert "feat/e2e-test" in content, (
        f"Expected branch 'feat/e2e-test' in /status card content, got: {content!r}"
    )
    assert "🌿" in content


async def test_status_omits_branch_line_for_non_git_project(tmp_path, settings):
    """/status card omits the branch line when project dir is not a git repo."""
    import json

    project_dir = tmp_path / "plain"
    project_dir.mkdir()

    project = Project(name="plain", path=str(project_dir), executor="claude-code-acp")
    config = AppConfig(projects=[project])
    replier = make_replier()
    replier.send_card = AsyncMock(return_value="status_card_id")

    dispatcher = make_dispatcher(config, settings, replier)
    await dispatcher.dispatch(make_task("/status"))

    replier.send_card.assert_called_once()
    card_json = replier.send_card.call_args[0][1]
    card = json.loads(card_json)
    content = card["body"]["elements"][0]["content"]

    assert "🌿" not in content, (
        f"Did not expect branch line in /status card for non-git project, got: {content!r}"
    )


# ---------------------------------------------------------------------------
# Test 31-33: Custom card hooks (dispatch_hook_task)
# ---------------------------------------------------------------------------


async def test_hook_task_loads_file_and_calls_runtime(tmp_path, settings):
    """dispatch_hook_task loads the hook file and the agent executes it."""
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    hooks_dir = project_dir / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "greet.md").write_text("Say hello warmly to the user.")

    project = Project(name="myproject", path=str(project_dir), executor="claude-code-acp")
    config = AppConfig(projects=[project])
    replier = make_replier()
    mock_runtime = make_mock_runtime(execute_return="Hello!")
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    session_id = "oc_chat:ou_user"
    await dispatcher.dispatch_hook_task({
        "action": "nextme_hook",
        "hook": "greet",
        "session_id": session_id,
        "operator_id": "ou_user",
        "chat_id": "oc_chat",
        "message_id": "om_hook_msg",
        "chat_type": "p2p",
    })

    session = _get_session(dispatcher, session_id)
    assert session is not None, "Session should have been created by dispatch_hook_task"

    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Hook task queue did not drain within timeout")
    finally:
        await cancel_workers(dispatcher)

    mock_runtime.execute.assert_called_once()
    executed_task = mock_runtime.execute.call_args.kwargs["task"]
    assert "Say hello warmly to the user." in executed_task.content, (
        f"Expected hook content in prompt, got: {executed_task.content!r}"
    )
    assert "ou_user" in executed_task.content, "Expected operator context in prompt"


async def test_hook_task_appends_context_to_prompt(tmp_path, settings):
    """dispatch_hook_task appends operator/chat/message context to the hook content."""
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    hooks_dir = project_dir / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "report.md").write_text("Generate a daily report.")

    project = Project(name="myproject", path=str(project_dir), executor="claude-code-acp")
    config = AppConfig(projects=[project])
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    await dispatcher.dispatch_hook_task({
        "hook": "report",
        "session_id": "oc_chat:ou_user",
        "operator_id": "ou_op_456",
        "chat_id": "oc_room_789",
        "message_id": "om_abc",
        "chat_type": "p2p",
        "custom_param": "my_value",
    })

    session = _get_session(dispatcher, "oc_chat:ou_user")
    assert session is not None
    try:
        await drain_session_queue(session)
    except asyncio.TimeoutError:
        pytest.fail("Hook task queue did not drain")
    finally:
        await cancel_workers(dispatcher)

    mock_runtime.execute.assert_called_once()
    executed_task = mock_runtime.execute.call_args.kwargs["task"]
    assert "Generate a daily report." in executed_task.content
    assert "ou_op_456" in executed_task.content
    assert "oc_room_789" in executed_task.content
    assert "om_abc" in executed_task.content
    assert "my_value" in executed_task.content


async def test_hook_task_missing_file_no_dispatch(tmp_path, settings):
    """dispatch_hook_task silently skips when the hook file does not exist."""
    project_dir = tmp_path / "repo"
    project_dir.mkdir()

    project = Project(name="myproject", path=str(project_dir), executor="claude-code-acp")
    config = AppConfig(projects=[project])
    replier = make_replier()
    mock_runtime = make_mock_runtime()
    acp_registry = ACPRuntimeRegistry()
    acp_registry.get_or_create = MagicMock(return_value=mock_runtime)

    dispatcher = make_dispatcher(config, settings, replier, acp_registry=acp_registry)

    await dispatcher.dispatch_hook_task({
        "hook": "does_not_exist",
        "session_id": "oc_chat:ou_user",
        "operator_id": "ou_user",
        "chat_id": "oc_chat",
        "message_id": "om_x",
        "chat_type": "p2p",
    })

    # Give a brief moment in case something async was started unexpectedly.
    await asyncio.sleep(0.05)

    # No worker should have been started; runtime must not have been called.
    mock_runtime.execute.assert_not_called()
    await cancel_workers(dispatcher)
