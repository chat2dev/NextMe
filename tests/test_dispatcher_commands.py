"""Tests for dispatcher command paths: /skill, /task, and /acl routing.

Covers lines 474-711 in src/nextme/core/dispatcher.py:
- /skill empty registry → card with "当前没有已注册"
- /skill list with skills → card with skill names
- /skill invoke existing → task enqueued + worker started
- /skill invoke unknown trigger → send_text "未找到"
- /skill invoke queue full → send_text "队列已满"
- /task no active tasks → card "当前没有进行中的任务"
- /task with active task → card shows task id
- /acl add / remove / pending / approve / reject routing → correct sub-handler called
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextme.acl.schema import Role
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import AppConfig, Project, Settings
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.path_lock import PathLockRegistry
from nextme.core.session import SessionRegistry
from nextme.protocol.types import Task
from nextme.skills.loader import Skill, SkillMeta
from nextme.skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# Module-level autouse fixture: reset the SessionRegistry singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_session_registry():
    """Reset the SessionRegistry singleton before and after each test."""
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    content: str = "hello",
    session_id: str = "oc_chat:ou_user",
) -> Task:
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id="msg_001",
        chat_type="p2p",
        timeout=timedelta(seconds=10),
    )


def make_replier() -> MagicMock:
    """Create a fully-mocked Replier."""
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="card_msg_id")
    r.send_reaction = AsyncMock()
    r.reply_text = AsyncMock(return_value="thread_msg_id")
    r.reply_card = AsyncMock(return_value="progress_msg_id")
    r.reply_card_by_id = AsyncMock(return_value="card_msg_id")
    r.update_card = AsyncMock()
    r.create_card = AsyncMock(return_value="")
    r.get_card_id = AsyncMock(return_value="")
    r.stream_set_content = AsyncMock()
    r.update_card_entity = AsyncMock()
    r.send_card_by_id = AsyncMock(return_value="msg_id")
    r.build_progress_card = MagicMock(return_value='{"card":"progress"}')
    r.build_result_card = MagicMock(return_value='{"card":"result"}')
    r.build_error_card = MagicMock(return_value='{"card":"error"}')
    r.build_streaming_progress_card = MagicMock(return_value='{"card":"sp"}')
    r.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')
    r.build_help_card = MagicMock(return_value='{"card":"help"}')
    r.build_whoami_card = MagicMock(return_value='{"card":"whoami"}')
    r.build_permission_card = MagicMock(return_value='{"card":"perm"}')
    return r


def make_skill(
    trigger: str,
    name: str = "Test Skill",
    description: str = "A test skill",
    source: str = "project",
) -> Skill:
    return Skill(
        meta=SkillMeta(name=name, trigger=trigger, description=description),
        template="# {trigger}\nDo something useful.\nUser request: {user_input}",
        source=source,
    )


def make_dispatcher(
    tmp_path,
    replier: MagicMock,
    skill_registry: SkillRegistry | None = None,
    acl_manager=None,
    task_queue_capacity: int = 10,
) -> TaskDispatcher:
    """Build a TaskDispatcher wired to a real project and the given replier."""
    project_dir = tmp_path / "repo"
    project_dir.mkdir(exist_ok=True)
    project = Project(name="test", path=str(project_dir), executor="mock")
    config = AppConfig(projects=[project])

    settings = Settings(
        task_queue_capacity=task_queue_capacity,
        progress_debounce_seconds=0.0,
        streaming_enabled=False,
    )

    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=replier)

    SessionRegistry._instance = None
    session_registry = SessionRegistry.get_instance()

    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=fc,
        skill_registry=skill_registry,
        acl_manager=acl_manager,
    )


def _get_session(dispatcher: TaskDispatcher, session_id: str):
    """Return the active session for a context_id, or None."""
    user_ctx = dispatcher._session_registry.get(session_id)
    if user_ctx is None:
        return None
    return user_ctx.get_active_session()


async def cancel_workers(dispatcher: TaskDispatcher) -> None:
    """Cancel all active worker tasks owned by the dispatcher."""
    for worker_task in list(dispatcher._worker_tasks.values()):
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# /skill tests
# ---------------------------------------------------------------------------


async def test_skill_empty_registry(tmp_path):
    """/skill with empty registry sends a card containing '当前没有已注册'."""
    replier = make_replier()
    skill_registry = SkillRegistry()  # empty
    d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

    await d.dispatch(make_task("/skill"))

    replier.send_card.assert_called_once()
    card_json_arg = replier.send_card.call_args[0][1]
    assert "当前没有已注册" in card_json_arg


async def test_skill_list_with_skills(tmp_path):
    """/skill with 2 skills from different sources sends a card with their names."""
    replier = make_replier()
    skill_registry = SkillRegistry()
    skill_registry._skills["hello"] = make_skill("hello", source="project")
    skill_registry._skills["review"] = make_skill("review", source="nextme")
    d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

    await d.dispatch(make_task("/skill"))

    replier.send_card.assert_called_once()
    card_json_arg = replier.send_card.call_args[0][1]
    # Both triggers should appear in the card body
    assert "hello" in card_json_arg
    assert "review" in card_json_arg


async def test_skill_invoke_existing(tmp_path):
    """/skill hello invokes an existing skill by enqueuing it and starting a worker."""
    replier = make_replier()
    skill_registry = SkillRegistry()
    skill_registry._skills["hello"] = make_skill("hello", source="project")
    d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

    session_id = "oc_chat:ou_user"
    task = make_task("/skill hello world", session_id=session_id)

    # Patch _ensure_worker so we don't spin up a real asyncio worker
    with patch.object(d, "_ensure_worker", new=AsyncMock()) as mock_worker:
        await d.dispatch(task)
        mock_worker.assert_called_once()

    session = _get_session(d, session_id)
    assert session is not None, "Session should have been created"
    # The skill task should have been enqueued (queue size is 1)
    assert session.task_queue.qsize() == 1


async def test_skill_invoke_unknown(tmp_path):
    """/skill unknown_trigger sends send_text with '未找到'."""
    replier = make_replier()
    skill_registry = SkillRegistry()
    skill_registry._skills["hello"] = make_skill("hello", source="project")
    d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

    await d.dispatch(make_task("/skill unknown_trigger"))

    replier.send_text.assert_called_once()
    text_arg = replier.send_text.call_args[0][1]
    assert "未找到" in text_arg


async def test_skill_invoke_queue_full(tmp_path):
    """/skill hello with a full queue sends send_text with '队列已满'."""
    replier = make_replier()
    skill_registry = SkillRegistry()
    skill_registry._skills["hello"] = make_skill("hello", source="project")
    # Create dispatcher with capacity=1
    d = make_dispatcher(
        tmp_path, replier, skill_registry=skill_registry, task_queue_capacity=1
    )

    session_id = "oc_chat:ou_user"

    # Dispatch a normal task to create the session (worker is patched away)
    with patch.object(d, "_ensure_worker", new=AsyncMock()):
        await d.dispatch(make_task("first task", session_id=session_id))

    session = _get_session(d, session_id)
    assert session is not None

    # Queue should now be full (capacity=1, one item in it)
    assert session.task_queue.qsize() == 1

    # Now dispatch /skill hello — the inner put_nowait should raise QueueFull
    with patch.object(d, "_ensure_worker", new=AsyncMock()):
        await d.dispatch(make_task("/skill hello", session_id=session_id))

    replier.send_text.assert_called()
    # Find the call with the queue-full message
    queue_full_calls = [
        call
        for call in replier.send_text.call_args_list
        if "队列已满" in str(call)
    ]
    assert queue_full_calls, "Expected '队列已满' message but got: " + str(
        replier.send_text.call_args_list
    )


# ---------------------------------------------------------------------------
# /task tests
# ---------------------------------------------------------------------------


async def test_task_no_active_tasks(tmp_path):
    """/task with no running tasks sends a card with '当前没有进行中的任务'."""
    replier = make_replier()
    d = make_dispatcher(tmp_path, replier)

    # Dispatch /task; this will create a session but it has no active_task
    await d.dispatch(make_task("/task"))

    replier.send_card.assert_called_once()
    card_json_arg = replier.send_card.call_args[0][1]
    assert "当前没有进行中的任务" in card_json_arg


async def test_task_with_active_task(tmp_path):
    """/task with a running task sends a card containing the task id."""
    replier = make_replier()
    d = make_dispatcher(tmp_path, replier)

    session_id = "oc_chat:ou_user"

    # Create the session by dispatching a real message first (worker patched away)
    with patch.object(d, "_ensure_worker", new=AsyncMock()):
        await d.dispatch(make_task("do some work", session_id=session_id))

    session = _get_session(d, session_id)
    assert session is not None

    # Manually set an active task on the session
    active = make_task("doing important work", session_id=session_id)
    session.active_task = active

    # Now dispatch /task
    await d.dispatch(make_task("/task", session_id=session_id))

    replier.send_card.assert_called()
    # Find the /task card (last send_card call)
    card_json_arg = replier.send_card.call_args[0][1]
    assert active.id[:8] in card_json_arg, (
        f"Expected task id prefix {active.id[:8]!r} in card: {card_json_arg}"
    )


# ---------------------------------------------------------------------------
# /acl routing tests
# ---------------------------------------------------------------------------


def make_acl_dispatcher(tmp_path, replier: MagicMock) -> TaskDispatcher:
    """Create dispatcher with a fully-mocked ACL manager whose caller is Owner."""
    acl_manager = MagicMock()
    acl_manager.get_role = AsyncMock(return_value=Role.OWNER)
    # Provide async stubs for the sub-handler calls to prevent side-effect errors
    acl_manager.list_users = AsyncMock(return_value=[])
    acl_manager.add_user = AsyncMock()
    acl_manager.remove_user = AsyncMock()
    acl_manager.list_pending = AsyncMock(return_value=[])
    acl_manager.approve_application = AsyncMock()
    acl_manager.reject_application = AsyncMock()
    acl_manager.get_application = AsyncMock(return_value=None)
    return make_dispatcher(tmp_path, replier, acl_manager=acl_manager)


async def test_acl_no_manager_sends_disabled_text(tmp_path):
    """/acl with no ACL manager sends '未启用' message."""
    replier = make_replier()
    d = make_dispatcher(tmp_path, replier, acl_manager=None)

    await d.dispatch(make_task("/acl list"))

    replier.send_text.assert_called_once()
    text_arg = replier.send_text.call_args[0][1]
    assert "未启用" in text_arg


async def test_acl_add_routes_to_handler(tmp_path):
    """/acl add <target> collaborator with Owner role calls handle_acl_add."""
    replier = make_replier()
    d = make_acl_dispatcher(tmp_path, replier)

    with patch("nextme.core.commands.handle_acl_add", new=AsyncMock()) as mock_add:
        await d.dispatch(make_task("/acl add ou_target collaborator"))
        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["target_id"] == "ou_target"
        assert kwargs["target_role_str"] == "collaborator"


async def test_acl_remove_routes_to_handler(tmp_path):
    """/acl remove <target> with Owner role calls handle_acl_remove."""
    replier = make_replier()
    d = make_acl_dispatcher(tmp_path, replier)

    with patch("nextme.core.commands.handle_acl_remove", new=AsyncMock()) as mock_remove:
        await d.dispatch(make_task("/acl remove ou_target"))
        mock_remove.assert_called_once()
        kwargs = mock_remove.call_args.kwargs
        assert kwargs["target_id"] == "ou_target"


async def test_acl_pending_routes_to_handler(tmp_path):
    """/acl pending with Owner role calls handle_acl_pending."""
    replier = make_replier()
    d = make_acl_dispatcher(tmp_path, replier)

    with patch("nextme.core.commands.handle_acl_pending", new=AsyncMock()) as mock_pending:
        await d.dispatch(make_task("/acl pending"))
        mock_pending.assert_called_once()


async def test_acl_approve_routes_to_handler(tmp_path):
    """/acl approve 42 with Owner role calls handle_acl_approve with app_id=42."""
    replier = make_replier()
    d = make_acl_dispatcher(tmp_path, replier)

    with patch("nextme.core.commands.handle_acl_approve", new=AsyncMock()) as mock_approve:
        await d.dispatch(make_task("/acl approve 42"))
        mock_approve.assert_called_once()
        kwargs = mock_approve.call_args.kwargs
        assert kwargs["app_id"] == 42


async def test_acl_reject_routes_to_handler(tmp_path):
    """/acl reject 42 with Owner role calls handle_acl_reject with app_id=42."""
    replier = make_replier()
    d = make_acl_dispatcher(tmp_path, replier)

    with patch("nextme.core.commands.handle_acl_reject", new=AsyncMock()) as mock_reject:
        await d.dispatch(make_task("/acl reject 42"))
        mock_reject.assert_called_once()
        kwargs = mock_reject.call_args.kwargs
        assert kwargs["app_id"] == 42


# ---------------------------------------------------------------------------
# task_timeout_seconds injection
# ---------------------------------------------------------------------------

async def test_dispatcher_injects_project_task_timeout(tmp_path):
    """Dispatcher overrides task.timeout from the project's task_timeout_seconds."""
    replier = make_replier()
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    project = Project(
        name="test", path=str(project_dir), executor="mock",
        task_timeout_seconds=600,   # 10 minutes
    )
    config = AppConfig(projects=[project])
    settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)
    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=SessionRegistry(), acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(), feishu_client=feishu_client,
    )

    task = make_task("hello")
    await d.dispatch(task)

    assert task.timeout == timedelta(seconds=600)


async def test_dispatcher_zero_task_timeout_keeps_task_default(tmp_path):
    """task_timeout_seconds=0 means no override — task keeps its default timeout."""
    replier = make_replier()
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    project = Project(
        name="test", path=str(project_dir), executor="mock",
        task_timeout_seconds=0,     # disabled
    )
    config = AppConfig(projects=[project])
    settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)
    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)
    d = TaskDispatcher(
        config=config, settings=settings,
        session_registry=SessionRegistry(), acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(), feishu_client=feishu_client,
    )

    original_timeout = timedelta(seconds=10)
    task = make_task("hello")
    task.timeout = original_timeout
    await d.dispatch(task)

    assert task.timeout == original_timeout


# ---------------------------------------------------------------------------
# @mention injection into skill user_input
# ---------------------------------------------------------------------------


class TestSkillMentionInjection:
    """Verify that task.mentions are appended to the skill prompt content."""

    async def test_skill_user_input_includes_mentions(self, tmp_path):
        """When a skill task has mentions, the enqueued skill_task content
        should include each mention's open_id and name."""
        replier = make_replier()
        skill_registry = SkillRegistry()
        skill_registry._skills["book"] = make_skill("book", name="Book Meeting")
        d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

        session_id = "oc_chat:ou_user"
        task = make_task("/skill book 明天下午3点", session_id=session_id)
        task.mentions = [
            {"name": "小明", "open_id": "ou_aaa"},
            {"name": "小红", "open_id": "ou_bbb"},
        ]

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        session = _get_session(d, session_id)
        assert session is not None
        assert session.task_queue.qsize() == 1

        skill_task = session.task_queue.get_nowait()
        assert "ou_aaa" in skill_task.content
        assert "小明" in skill_task.content
        assert "ou_bbb" in skill_task.content
        # Requester is merged into the same 参与人 block
        assert "ou_user" in skill_task.content
        assert "[预定人]" in skill_task.content

    async def test_skill_no_mentions_requester_injected(self, tmp_path):
        """When task.mentions is empty, the requester still appears in
        参与人(@mentions) block as [预定人]."""
        replier = make_replier()
        skill_registry = SkillRegistry()
        skill_registry._skills["book"] = make_skill("book", name="Book Meeting")
        d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

        session_id = "oc_chat:ou_user"
        task = make_task("/skill book 明天下午3点", session_id=session_id)
        task.mentions = []  # explicitly empty

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        session = _get_session(d, session_id)
        assert session is not None
        assert session.task_queue.qsize() == 1

        skill_task = session.task_queue.get_nowait()
        assert "参与人(@mentions)" in skill_task.content
        assert "[预定人] (open_id: ou_user)" in skill_task.content


# ---------------------------------------------------------------------------
# task.user_id extraction tests
# ---------------------------------------------------------------------------


class TestUserIdExtraction:
    """ACL and /whoami commands use task.user_id, not session_id suffix."""

    async def test_group_thread_session_uses_task_user_id(self, tmp_path):
        """When session_id is chat_id:thread_root_id, user identity comes from task.user_id."""
        replier = make_replier()
        d = make_dispatcher(tmp_path, replier)

        # Simulate group thread task: session_id ends with message_id, not user_id
        task = make_task("hello", session_id="oc_chat:om_thread_root")
        task.user_id = "ou_actual_user"
        task.thread_root_id = "om_thread_root"
        task.chat_type = "group"

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        # Should not crash; user_id correctly used for any user-identity lookup

    async def test_skill_group_thread_uses_task_user_id_for_requester(self, tmp_path):
        """When session_id is chat_id:thread_root_id, skill requester open_id
        comes from task.user_id, not the session_id suffix."""
        replier = make_replier()
        skill_registry = SkillRegistry()
        skill_registry._skills["book"] = make_skill("book", name="Book Meeting")
        d = make_dispatcher(tmp_path, replier, skill_registry=skill_registry)

        # session_id ends with message thread id, NOT user id
        session_id = "oc_chat:om_thread_root"
        task = make_task("/skill book 明天下午3点", session_id=session_id)
        task.user_id = "ou_actual_user"
        task.thread_root_id = "om_thread_root"
        task.chat_type = "group"
        task.mentions = []

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        session = _get_session(d, session_id)
        assert session is not None
        assert session.task_queue.qsize() == 1


# ---------------------------------------------------------------------------
# Thread limit tests
# ---------------------------------------------------------------------------


class TestThreadLimit:
    """Group thread limit: queue new threads when max_active_threads_per_chat reached."""

    async def test_thread_within_limit_dispatched_normally(self, tmp_path):
        """First thread in a chat is dispatched without restriction."""
        from nextme.config.state_store import StateStore

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=2)
        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        task = make_task("hi", session_id="oc_G:om_root1")
        task.user_id = "ou_A"
        task.thread_root_id = "om_root1"
        task.message_id = "om_root1"  # same as thread_root_id = new thread
        task.chat_type = "group"

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        # No queuing message should have been sent
        for call in replier.reply_text.call_args_list:
            assert "排队" not in str(call) and "上限" not in str(call)

        # Thread should be registered as active
        assert store.get_active_thread_count("oc_G") == 1

    async def test_thread_at_limit_sends_queue_message(self, tmp_path):
        """When active thread count == limit, new root message is queued."""
        from nextme.config.state_store import StateStore

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=1)

        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()
        store.register_thread("oc_G", "om_root1", "test")  # already 1 active

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        task = make_task("hi", session_id="oc_G:om_root2")
        task.user_id = "ou_B"
        task.thread_root_id = "om_root2"
        task.message_id = "om_root2"
        task.chat_type = "group"

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        # Should have sent a queuing reply
        queue_calls = [
            call
            for call in replier.reply_text.call_args_list
            if "排队" in str(call) or "上限" in str(call)
        ]
        assert queue_calls, (
            "Expected a queue-full reply but got: " + str(replier.reply_text.call_args_list)
        )

        # Task should be in pending queue, not dispatched
        assert "oc_G" in d._pending_thread_queue
        assert len(d._pending_thread_queue["oc_G"]) == 1

    async def test_thread_at_limit_does_not_enqueue_task(self, tmp_path):
        """Queued thread tasks are NOT placed in the session task queue."""
        from nextme.config.state_store import StateStore

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=1)

        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()
        store.register_thread("oc_G", "om_root1", "test")

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        task = make_task("hi", session_id="oc_G:om_root2")
        task.user_id = "ou_B"
        task.thread_root_id = "om_root2"
        task.message_id = "om_root2"
        task.chat_type = "group"

        ensure_worker_mock = AsyncMock()
        with patch.object(d, "_ensure_worker", new=ensure_worker_mock):
            await d.dispatch(task)

        # _ensure_worker should NOT have been called (task was queued, not dispatched)
        ensure_worker_mock.assert_not_called()

    async def test_on_thread_closed_dispatches_next_queued(self, tmp_path):
        """_on_thread_closed releases slot and re-dispatches the next pending task."""
        from nextme.config.state_store import StateStore
        import collections

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=1)

        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()
        store.register_thread("oc_G", "om_root1", "test")  # 1 active

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        # Pre-populate pending queue
        pending_task = make_task("hi", session_id="oc_G:om_root2")
        pending_task.user_id = "ou_B"
        pending_task.thread_root_id = "om_root2"
        pending_task.message_id = "om_root2"
        pending_task.chat_type = "group"
        d._pending_thread_queue["oc_G"] = collections.deque([pending_task])

        dispatched = []

        async def fake_dispatch(task):
            dispatched.append(task)

        with patch.object(d, "dispatch", side_effect=fake_dispatch):
            d._on_thread_closed("oc_G", "om_root1")
            # give asyncio.create_task a chance to run
            await asyncio.sleep(0)

        # thread should have been unregistered
        assert store.get_active_thread_count("oc_G") == 0
        # pending queue should now be empty
        assert len(d._pending_thread_queue["oc_G"]) == 0
        # pending task should have been re-dispatched
        assert len(dispatched) == 1
        assert dispatched[0] is pending_task

    async def test_non_root_group_message_not_limited(self, tmp_path):
        """Follow-up messages in a thread (message_id != thread_root_id) bypass limit."""
        from nextme.config.state_store import StateStore

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=1)

        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()
        store.register_thread("oc_G", "om_root1", "test")  # at limit

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        # Follow-up message: session_id uses thread_root_id but message_id is different
        task = make_task("follow up", session_id="oc_G:om_root1")
        task.user_id = "ou_A"
        task.thread_root_id = "om_root1"
        task.message_id = "om_reply_789"  # different from thread_root_id
        task.chat_type = "group"

        ensure_worker_mock = AsyncMock()
        with patch.object(d, "_ensure_worker", new=ensure_worker_mock):
            await d.dispatch(task)

        # Should be dispatched normally, not queued
        ensure_worker_mock.assert_called_once()
        assert "oc_G" not in d._pending_thread_queue or len(d._pending_thread_queue["oc_G"]) == 0


# ---------------------------------------------------------------------------
# /done command routing tests
# ---------------------------------------------------------------------------


class TestDoneCommand:
    """Verify /done command routing in dispatcher."""

    async def test_done_command_calls_handle_done(self, tmp_path):
        """/done in a group thread triggers handle_done."""
        replier = make_replier()
        d = make_dispatcher(tmp_path, replier)

        task = make_task("/done", session_id="oc_G:om_root1")
        task.user_id = "ou_A"
        task.thread_root_id = "om_root1"
        task.message_id = "om_reply1"
        task.chat_type = "group"

        with patch("nextme.core.dispatcher.handle_done", new=AsyncMock()) as mock_done:
            await d.dispatch(task)
            mock_done.assert_called_once()

    async def test_done_outside_thread_sends_error(self, tmp_path):
        """/done in p2p chat sends helpful error message (not group thread)."""
        replier = make_replier()
        d = make_dispatcher(tmp_path, replier)

        task = make_task("/done", session_id="p2p_chat:ou_user")
        task.user_id = "ou_user"
        task.thread_root_id = ""
        task.chat_type = "p2p"

        await d.dispatch(task)
        # Should send an error about /done only working in group threads
        assert replier.send_text.called or replier.reply_text.called

    async def test_done_group_without_thread_root_sends_error(self, tmp_path):
        """/done in group chat with no thread_root_id sends an error."""
        replier = make_replier()
        d = make_dispatcher(tmp_path, replier)

        task = make_task("/done", session_id="oc_G:ou_user")
        task.user_id = "ou_user"
        task.thread_root_id = ""  # not in a thread
        task.chat_type = "group"

        await d.dispatch(task)
        replier.send_text.assert_called()
        text_arg = replier.send_text.call_args[0][1]
        assert "话题" in text_arg or "/done" in text_arg


# ---------------------------------------------------------------------------
# Thread queue bug-fix regression tests
# ---------------------------------------------------------------------------


class TestThreadQueueBugFixes:
    """Regression tests for the three thread-queue bugs found and fixed.

    Bug 1: When dispatcher queues a root task (limit exceeded), it must call
           _thread_closed_callback to roll back handler's optimistic
           _active_threads entry.

    Bug 2: When _on_thread_closed dequeues the next task, it must call
           _thread_accept_callback to restore the thread in handler's
           _active_threads.

    Bug 3: When /done triggers _on_thread_closed, _thread_closed_callback
           (= handler.deregister_thread) must be called so the thread is
           removed from handler._active_threads.
    """

    async def test_bug1_active_threads_rolled_back_when_queued(self, tmp_path):
        """Bug 1: When task is queued (limit exceeded), _thread_closed_callback must
        be called to roll back handler's optimistic _active_threads entry."""
        from nextme.config.state_store import StateStore

        replier = make_replier()
        settings_obj = Settings(max_active_threads_per_chat=1)
        store = StateStore(settings_obj, state_path=tmp_path / "state.json")
        await store.load()
        store.register_thread("oc_G", "om_existing", "default")  # fill the one slot

        d = make_dispatcher(tmp_path, replier)
        d._state_store = store
        d._settings = settings_obj

        # Install callback spy
        rollback_calls: list[tuple[str, str]] = []
        d._thread_closed_callback = lambda chat_id, thread_root_id: rollback_calls.append(
            (chat_id, thread_root_id)
        )

        task = make_task("hi", session_id="oc_G:om_new_root")
        task.user_id = "ou_A"
        task.thread_root_id = "om_new_root"
        task.message_id = "om_new_root"  # root message = new thread
        task.chat_type = "group"

        with patch.object(d, "_ensure_worker", new=AsyncMock()):
            await d.dispatch(task)

        # Task should be queued (not dispatched) due to limit
        assert "oc_G" in d._pending_thread_queue
        assert len(d._pending_thread_queue["oc_G"]) == 1

        # Rollback callback must have been called to undo the handler's optimistic entry
        assert len(rollback_calls) == 1, (
            f"Expected 1 rollback call, got {rollback_calls}"
        )
        assert rollback_calls[0] == ("oc_G", "om_new_root")

    async def test_bug2_active_threads_restored_when_queued_task_released(self, tmp_path):
        """Bug 2: When _on_thread_closed dequeues the next task, _thread_accept_callback
        must be called so handler knows the thread is now active."""
        import collections

        d = make_dispatcher(tmp_path, make_replier())

        # Install accept callback spy
        accept_calls: list[tuple[str, str]] = []
        d._thread_accept_callback = lambda chat_id, thread_root_id: accept_calls.append(
            (chat_id, thread_root_id)
        )
        # Silence the closed callback
        d._thread_closed_callback = lambda *args: None

        # Put a queued task in the pending queue
        queued_task = make_task("queued", session_id="oc_G:om_queued_root")
        queued_task.user_id = "ou_B"
        queued_task.thread_root_id = "om_queued_root"
        queued_task.message_id = "om_queued_root"
        queued_task.chat_type = "group"
        d._pending_thread_queue["oc_G"] = collections.deque([queued_task])

        # Patch state_store to avoid errors on unregister
        if d._state_store is not None:
            d._state_store.unregister_thread = MagicMock()

        # Patch dispatch so the re-dispatched task doesn't run for real
        d.dispatch = AsyncMock()

        # Trigger _on_thread_closed (simulates /done on the currently-active thread)
        d._on_thread_closed("oc_G", "om_active_root")

        # Give asyncio.create_task a chance to schedule
        await asyncio.sleep(0)

        # accept callback must have been called with the queued task's thread_root_id
        assert len(accept_calls) == 1, (
            f"Expected 1 accept call, got {accept_calls}"
        )
        assert accept_calls[0] == ("oc_G", "om_queued_root")

    async def test_bug3_done_command_deregisters_from_active_threads(self, tmp_path):
        """Bug 3: _on_thread_closed must call _thread_closed_callback (= handler.deregister_thread)
        so the thread is removed from handler._active_threads after /done."""
        import collections

        d = make_dispatcher(tmp_path, make_replier())

        # Install closed callback spy
        closed_calls: list[tuple[str, str]] = []
        d._thread_closed_callback = lambda chat_id, thread_root_id: closed_calls.append(
            (chat_id, thread_root_id)
        )

        # No pending tasks — simple close
        d._pending_thread_queue = {}
        if d._state_store is not None:
            d._state_store.unregister_thread = MagicMock()

        d._on_thread_closed("oc_G", "om_root1")

        # Closed callback must have been called — this removes from handler._active_threads
        assert len(closed_calls) == 1, (
            f"Expected 1 closed call, got {closed_calls}"
        )
        assert closed_calls[0] == ("oc_G", "om_root1")

    def test_handler_register_and_deregister_thread(self):
        """Integration: handler.register_thread adds, handler.deregister_thread removes."""
        from nextme.feishu.handler import MessageHandler
        from nextme.feishu.dedup import MessageDedup

        handler = MessageHandler(MessageDedup(), MagicMock())

        # Initially empty
        assert "oc_G:om_root1" not in handler._active_threads

        # register adds it
        handler.register_thread("oc_G", "om_root1")
        assert "oc_G:om_root1" in handler._active_threads

        # deregister removes it
        handler.deregister_thread("oc_G", "om_root1")
        assert "oc_G:om_root1" not in handler._active_threads

        # deregister is idempotent — must not raise
        handler.deregister_thread("oc_G", "om_root1")
        assert "oc_G:om_root1" not in handler._active_threads

    def test_full_callback_chain_wiring(self, tmp_path):
        """Verify wiring: after register_thread_closed/accept_callback, invoking
        the dispatcher callbacks mutates handler._active_threads correctly."""
        from nextme.feishu.handler import MessageHandler
        from nextme.feishu.dedup import MessageDedup

        handler = MessageHandler(MessageDedup(), MagicMock())
        d = make_dispatcher(tmp_path, make_replier())

        # Wire up the way main.py does
        d.register_thread_closed_callback(handler.deregister_thread)
        d.register_thread_accept_callback(handler.register_thread)

        # Simulate: dispatcher accepts a thread → added to _active_threads
        handler._active_threads = set()
        d._thread_accept_callback("oc_G", "om_root1")
        assert "oc_G:om_root1" in handler._active_threads

        # Simulate: dispatcher closes the thread → removed from _active_threads
        d._thread_closed_callback("oc_G", "om_root1")
        assert "oc_G:om_root1" not in handler._active_threads
