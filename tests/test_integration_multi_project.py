"""Integration tests for multi-project parallel execution.

Verifies end-to-end behaviour of the feat/multi-project-parallel changes:

1. Two projects get independent workers that run concurrently.
2. Static chat binding routes messages to the right project.
3. Dynamic binding (/project bind) persists to state and routes correctly.
4. Worker key isolation: a slow project-A task does not block project-B.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import AppConfig, Project, Settings
from nextme.config.state_store import StateStore
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.path_lock import PathLockRegistry
from nextme.core.session import SessionRegistry
from nextme.protocol.types import Task, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(content: str, session_id: str = "chat_abc:user_xyz") -> Task:
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        timeout=timedelta(seconds=10),
    )


def make_mock_replier() -> MagicMock:
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
    r.build_streaming_progress_card = MagicMock(return_value='{"card": "stream"}')
    r.create_card = AsyncMock(return_value="")
    r.send_card_by_id = AsyncMock(return_value="msg_id")
    r.reply_card_by_id = AsyncMock(return_value="msg_id")
    return r


@pytest.fixture(autouse=True)
def reset_registry():
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


@pytest.fixture
def settings():
    return Settings(task_queue_capacity=10, permission_timeout_seconds=1.0)


@pytest.fixture
def project_a(tmp_path):
    d = tmp_path / "repo_a"
    d.mkdir()
    return Project(name="repo-A", path=str(d), executor="claude-code-acp")


@pytest.fixture
def project_b(tmp_path):
    d = tmp_path / "repo_b"
    d.mkdir()
    return Project(name="repo-B", path=str(d), executor="claude-code-acp")


@pytest.fixture
def replier():
    return make_mock_replier()


@pytest.fixture
def feishu_client(replier):
    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=replier)
    return fc


def make_dispatcher(config, settings, feishu_client, state_store=None):
    SessionRegistry._instance = None
    registry = SessionRegistry.get_instance()
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=registry,
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        state_store=state_store,
    )


# ---------------------------------------------------------------------------
# Test 1: Independent workers per project
# ---------------------------------------------------------------------------


async def test_two_projects_get_independent_workers(
    project_a, project_b, settings, feishu_client
):
    """Dispatching to two projects creates two independent worker tasks."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project_a, project_b])
    d = make_dispatcher(config, settings, feishu_client)

    started: list[str] = []

    async def slow_worker_a():
        started.append("A")
        await asyncio.sleep(0.1)

    async def fast_worker_b():
        started.append("B")
        await asyncio.sleep(0.01)

    call_count = 0

    from unittest.mock import patch

    def make_worker_side_effect(session, **kwargs):
        nonlocal call_count
        call_count += 1
        w = MagicMock()
        if session.project_name == "repo-A":
            w.run = slow_worker_a
        else:
            w.run = fast_worker_b
        return w

    with patch("nextme.core.dispatcher.SessionWorker", side_effect=make_worker_side_effect):
        # First task goes to repo-A (default), second to repo-B via /project switch
        task_a = make_task("work on A")
        await d.dispatch(task_a)

        # Switch active project to B and dispatch
        task_switch = make_task("/project repo-B")
        await d.dispatch(task_switch)
        task_b = make_task("work on B")
        await d.dispatch(task_b)

        # Let both workers run
        await asyncio.sleep(0.2)

    # Two separate workers were created
    assert call_count == 2
    # Both projects have sessions
    ctx = SessionRegistry.get_instance().get("chat_abc:user_xyz")
    assert "repo-A" in ctx.sessions
    assert "repo-B" in ctx.sessions


# ---------------------------------------------------------------------------
# Test 2: Project B is not blocked by project A's slow task
# ---------------------------------------------------------------------------


async def test_project_b_not_blocked_by_project_a(
    project_a, project_b, settings, feishu_client
):
    """A slow project-A worker does not delay project-B's worker start."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project_a, project_b])
    d = make_dispatcher(config, settings, feishu_client)

    finish_times: dict[str, float] = {}

    async def slow_run_a():
        await asyncio.sleep(0.15)
        finish_times["A"] = asyncio.get_event_loop().time()

    async def fast_run_b():
        await asyncio.sleep(0.02)
        finish_times["B"] = asyncio.get_event_loop().time()

    def worker_factory(session, **kwargs):
        w = MagicMock()
        w.run = slow_run_a if session.project_name == "repo-A" else fast_run_b
        return w

    from unittest.mock import patch

    with patch("nextme.core.dispatcher.SessionWorker", side_effect=worker_factory):
        # Bootstrap repo-A, then immediately also start repo-B
        task_a = make_task("slow task on A")
        await d.dispatch(task_a)

        task_switch = make_task("/project repo-B")
        await d.dispatch(task_switch)
        task_b = make_task("fast task on B")
        await d.dispatch(task_b)

        await asyncio.sleep(0.3)

    # B must finish before A (proves parallel execution)
    assert "A" in finish_times and "B" in finish_times, "Both workers should have run"
    assert finish_times["B"] < finish_times["A"], (
        f"B ({finish_times['B']:.3f}) should finish before A ({finish_times['A']:.3f})"
    )


# ---------------------------------------------------------------------------
# Test 3: Static binding routes to correct project
# ---------------------------------------------------------------------------


async def test_static_binding_routes_to_bound_project(
    project_a, project_b, settings, feishu_client
):
    """Messages from a bound chat go to the configured project, not the default."""
    config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project_a, project_b],  # project_a is default (first)
        bindings={"chat_bound": project_b.name},  # bind this chat to repo-B
    )
    d = make_dispatcher(config, settings, feishu_client)

    from unittest.mock import patch

    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance

        # Message from bound chat → should go to repo-B, not repo-A (default)
        task = make_task("hello", session_id="chat_bound:ou_user")
        await d.dispatch(task)

    ctx = SessionRegistry.get_instance().get("chat_bound:ou_user")
    assert ctx is not None
    # repo-B was activated, not repo-A
    assert "repo-B" in ctx.sessions
    assert ctx.active_project == "repo-B"


# ---------------------------------------------------------------------------
# Test 4: Dynamic binding via /project bind
# ---------------------------------------------------------------------------


async def test_dynamic_binding_via_project_bind_command(
    project_a, project_b, settings, feishu_client, tmp_path
):
    """'/project bind repo-B' routes subsequent messages to repo-B."""
    config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project_a, project_b],
    )
    state_store = StateStore(settings, state_path=tmp_path / "state.json")
    await state_store.load()
    d = make_dispatcher(config, settings, feishu_client, state_store=state_store)

    # Bind chat_abc → repo-B
    bind_task = make_task("/project bind repo-B")
    await d.dispatch(bind_task)

    # Verify in-memory binding was set
    assert d._dynamic_bindings.get("chat_abc") == "repo-B"
    # Verify persisted to state_store
    assert state_store.get_all_bindings().get("chat_abc") == "repo-B"

    # Next normal message should go to repo-B
    from unittest.mock import patch

    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance

        task = make_task("do some work")
        await d.dispatch(task)

    ctx = SessionRegistry.get_instance().get("chat_abc:user_xyz")
    assert "repo-B" in ctx.sessions


# ---------------------------------------------------------------------------
# Test 5: /project unbind reverts to active_project routing
# ---------------------------------------------------------------------------


async def test_unbind_reverts_to_active_project(
    project_a, project_b, settings, feishu_client, tmp_path
):
    """/project unbind removes the binding and restores active_project routing."""
    config = AppConfig(
        app_id="x", app_secret="y",
        projects=[project_a, project_b],
    )
    state_store = StateStore(settings, state_path=tmp_path / "state.json")
    await state_store.load()
    d = make_dispatcher(config, settings, feishu_client, state_store=state_store)

    # Set a binding
    d._dynamic_bindings["chat_abc"] = "repo-B"
    state_store.set_binding("chat_abc", "repo-B")

    # Unbind
    unbind_task = make_task("/project unbind")
    await d.dispatch(unbind_task)

    assert "chat_abc" not in d._dynamic_bindings
    assert "chat_abc" not in state_store.get_all_bindings()


# ---------------------------------------------------------------------------
# Test 6: Binding survives restart (loaded from state_store at construction)
# ---------------------------------------------------------------------------


async def test_binding_survives_restart(
    project_a, project_b, settings, feishu_client, tmp_path
):
    """Bindings persisted in state.json are loaded when dispatcher is created."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project_a, project_b])

    # First "run": set a binding
    state_store = StateStore(settings, state_path=tmp_path / "state.json")
    await state_store.load()
    state_store.set_binding("chat_abc", "repo-B")
    await state_store.flush()

    # Second "run": new dispatcher loads from persisted state
    state_store2 = StateStore(settings, state_path=tmp_path / "state.json")
    await state_store2.load()
    SessionRegistry._instance = None
    d2 = make_dispatcher(config, settings, feishu_client, state_store=state_store2)

    assert d2._dynamic_bindings.get("chat_abc") == "repo-B"


# ---------------------------------------------------------------------------
# Test 7: Worker keys are per project (not per context_id)
# ---------------------------------------------------------------------------


async def test_worker_keys_are_project_scoped(
    project_a, project_b, settings, feishu_client
):
    """_worker_tasks keys include the project name for proper isolation."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project_a, project_b])
    d = make_dispatcher(config, settings, feishu_client)

    from unittest.mock import patch

    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance

        # Dispatch to repo-A (default)
        await d.dispatch(make_task("work A"))

        # Switch to repo-B and dispatch
        await d.dispatch(make_task("/project repo-B"))
        await d.dispatch(make_task("work B"))

    keys = list(d._worker_tasks.keys())
    assert any("repo-A" in k for k in keys), f"Expected repo-A key in {keys}"
    assert any("repo-B" in k for k in keys), f"Expected repo-B key in {keys}"
    # Keys must include context_id as prefix
    assert all(k.startswith("chat_abc:user_xyz:") for k in keys)
