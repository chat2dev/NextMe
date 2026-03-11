"""Unit tests for nextme.core.commands."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.core.commands import (
    HELP_COMMANDS, handle_new, handle_stop, handle_help,
    handle_status, handle_project, handle_bind, handle_unbind, handle_remember,
    _get_git_branch,
)
from nextme.config.schema import AppConfig, Project, Settings
from nextme.core.session import Session, UserContext
from nextme.protocol.types import Task, TaskStatus
import datetime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    return Settings(task_queue_capacity=5)


@pytest.fixture
def project(tmp_path):
    return Project(name="myproj", path=str(tmp_path), executor="claude-code-acp")


@pytest.fixture
def session(project, settings):
    return Session("oc_chat:ou_user", project, settings)


@pytest.fixture
def user_ctx():
    return UserContext("oc_chat:ou_user")


@pytest.fixture
def replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="msg_123")
    r.update_card = AsyncMock()
    r.build_help_card = MagicMock(return_value='{"card": "help"}')
    r.build_permission_card = MagicMock(return_value='{"card": "perm"}')
    r.build_progress_card = MagicMock(return_value='{"card": "progress"}')
    r.build_result_card = MagicMock(return_value='{"card": "result"}')
    r.build_error_card = MagicMock(return_value='{"card": "error"}')
    return r


# ---------------------------------------------------------------------------
# HELP_COMMANDS tests
# ---------------------------------------------------------------------------

def test_help_commands_is_list_of_tuples():
    assert isinstance(HELP_COMMANDS, list)
    assert len(HELP_COMMANDS) == 27


def test_help_commands_each_item_has_two_strings():
    for item in HELP_COMMANDS:
        assert isinstance(item, tuple), f"Expected tuple, got {type(item)}"
        assert len(item) == 2, f"Expected 2 elements, got {len(item)}"
        cmd, desc = item
        assert isinstance(cmd, str), f"Command should be str, got {type(cmd)}"
        assert isinstance(desc, str), f"Description should be str, got {type(desc)}"


# ---------------------------------------------------------------------------
# handle_new tests
# ---------------------------------------------------------------------------

async def test_handle_new_resets_actual_id(session, replier):
    session.actual_id = "old-session-id"
    runtime = AsyncMock()
    runtime.reset_session = AsyncMock()
    await handle_new(session, runtime, replier, "oc_chat")
    assert session.actual_id == ""


async def test_handle_new_calls_runtime_reset_when_not_none(session, replier):
    runtime = AsyncMock()
    runtime.reset_session = AsyncMock()
    await handle_new(session, runtime, replier, "oc_chat")
    runtime.reset_session.assert_awaited_once()


async def test_handle_new_does_not_call_runtime_reset_when_none(session, replier):
    # Should not raise, even with runtime=None
    await handle_new(session, None, replier, "oc_chat")
    replier.send_text.assert_awaited_once()


async def test_handle_new_calls_send_text_with_chat_id(session, replier):
    runtime = AsyncMock()
    runtime.reset_session = AsyncMock()
    await handle_new(session, runtime, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_new_handles_runtime_exception_gracefully(session, replier):
    runtime = AsyncMock()
    runtime.reset_session = AsyncMock(side_effect=RuntimeError("reset failed"))
    # Should not raise; exception is caught internally
    await handle_new(session, runtime, replier, "oc_chat")
    # send_text should still be called despite the runtime error
    replier.send_text.assert_awaited_once()


async def test_handle_new_handles_send_text_exception_gracefully(session):
    """handle_new should not propagate exceptions raised by replier.send_text."""
    runtime = AsyncMock()
    runtime.reset_session = AsyncMock()
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_new(session, runtime, bad_replier, "oc_chat")


# ---------------------------------------------------------------------------
# handle_stop tests
# ---------------------------------------------------------------------------

async def test_handle_stop_sets_active_task_canceled(session, replier):
    task = MagicMock()
    task.canceled = False
    task.id = "task-1"
    session.active_task = task
    await handle_stop(session, replier, "oc_chat")
    assert task.canceled is True


async def test_handle_stop_no_active_task_does_not_crash(session, replier):
    session.active_task = None
    # Should not raise
    await handle_stop(session, replier, "oc_chat")


async def test_handle_stop_calls_cancel_permission(session, replier):
    with patch.object(session, "cancel_permission") as mock_cancel:
        await handle_stop(session, replier, "oc_chat")
        mock_cancel.assert_called_once()


async def test_handle_stop_calls_send_text(session, replier):
    await handle_stop(session, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_stop_handles_send_text_exception_gracefully(session):
    """handle_stop should not propagate exceptions raised by replier.send_text."""
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_stop(session, bad_replier, "oc_chat")


async def test_handle_stop_calls_runtime_cancel_when_provided(session, replier):
    """handle_stop calls runtime.cancel() when runtime is provided."""
    runtime = MagicMock()
    runtime.cancel = AsyncMock()
    await handle_stop(session, replier, "oc_chat", runtime=runtime)
    runtime.cancel.assert_awaited_once()


async def test_handle_stop_skips_runtime_cancel_when_not_provided(session, replier):
    """handle_stop is safe when no runtime is passed (backward-compat)."""
    # Should not raise and must still send the confirmation text.
    await handle_stop(session, replier, "oc_chat", runtime=None)
    replier.send_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_help tests
# ---------------------------------------------------------------------------

async def test_handle_help_calls_build_help_card(replier):
    await handle_help(replier, "oc_chat")
    replier.build_help_card.assert_called_once_with(HELP_COMMANDS)


async def test_handle_help_calls_send_card(replier):
    await handle_help(replier, "oc_chat")
    replier.send_card.assert_awaited_once()
    call_args = replier.send_card.call_args[0]
    assert call_args[0] == "oc_chat"
    # The card content should be what build_help_card returned
    assert call_args[1] == '{"card": "help"}'


async def test_handle_help_handles_exception_gracefully():
    """handle_help should not propagate exceptions from replier."""
    bad_replier = MagicMock()
    bad_replier.build_help_card = MagicMock(side_effect=RuntimeError("build failed"))
    bad_replier.send_card = AsyncMock()
    # Should not raise
    await handle_help(bad_replier, "oc_chat")


async def test_handle_help_handles_send_card_exception_gracefully():
    bad_replier = MagicMock()
    bad_replier.build_help_card = MagicMock(return_value='{"card": "help"}')
    bad_replier.send_card = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_help(bad_replier, "oc_chat")


# ---------------------------------------------------------------------------
# handle_status tests
# ---------------------------------------------------------------------------

async def test_handle_status_empty_user_ctx_sends_text(user_ctx, replier):
    """No sessions → send_text with 'no active session' message."""
    await handle_status(user_ctx, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_status_calls_send_card(user_ctx, session, project, settings, replier):
    user_ctx.get_or_create_session(project, settings)
    await handle_status(user_ctx, replier, "oc_chat")
    replier.send_card.assert_awaited_once()
    call_args = replier.send_card.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_status_card_contains_session_info(user_ctx, session, project, settings, replier):
    import json
    user_ctx.get_or_create_session(project, settings)
    await handle_status(user_ctx, replier, "oc_chat")
    call_args = replier.send_card.call_args[0]
    card_json = call_args[1]
    card = json.loads(card_json)
    assert card["schema"] == "2.0"
    assert "body" in card
    elements = card["body"]["elements"]
    assert len(elements) >= 1
    content = elements[0]["content"]
    assert session.project_name in content


async def test_handle_status_active_project_marked_with_star(user_ctx, project, settings, replier):
    import json
    user_ctx.get_or_create_session(project, settings)
    await handle_status(user_ctx, replier, "oc_chat")
    card_json = replier.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "★" in content


async def test_handle_status_handles_exception_gracefully(user_ctx, project, settings):
    """handle_status should not propagate exceptions from replier."""
    user_ctx.get_or_create_session(project, settings)
    bad_replier = MagicMock()
    bad_replier.send_card = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_status(user_ctx, bad_replier, "oc_chat")


# ---------------------------------------------------------------------------
# handle_project tests
# ---------------------------------------------------------------------------

async def test_handle_project_found_creates_session_and_sends_text(
    user_ctx, project, replier, settings
):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_project(user_ctx, project.name, config, settings, replier, "oc_chat")
    # Success path now sends a card, not plain text
    replier.send_card.assert_awaited_once()
    card_arg = replier.send_card.call_args[0][1]
    # Confirmation card JSON should mention the project name
    assert project.name in card_arg


async def test_handle_project_found_sets_active_project(
    user_ctx, project, replier, settings
):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_project(user_ctx, project.name, config, settings, replier, "oc_chat")
    assert user_ctx.active_project == project.name
    assert project.name in user_ctx.sessions


async def test_handle_project_not_found(user_ctx, replier, settings):
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    await handle_project(user_ctx, "missing", config, settings, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args[0][1]
    assert "missing" in text_arg


async def test_handle_project_not_found_lists_available_projects(
    user_ctx, project, replier, settings
):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_project(user_ctx, "nonexistent", config, settings, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args[0][1]
    # Error message should mention the missing project name
    assert "nonexistent" in text_arg
    # And the available project name
    assert project.name in text_arg


async def test_handle_project_found_sends_to_correct_chat(
    user_ctx, project, replier, settings
):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_project(user_ctx, project.name, config, settings, replier, "oc_chat_target")
    # Success path sends a card to the chat
    call_args = replier.send_card.call_args[0]
    assert call_args[0] == "oc_chat_target"


async def test_handle_project_not_found_sends_to_correct_chat(
    user_ctx, replier, settings
):
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    await handle_project(user_ctx, "missing", config, settings, replier, "oc_chat_target")
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat_target"


async def test_handle_project_send_text_exception_gracefully(
    user_ctx, project, replier, settings
):
    """handle_project should not propagate exceptions from replier.send_card."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    bad_replier = MagicMock()
    bad_replier.send_card = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_project(user_ctx, project.name, config, settings, bad_replier, "oc_chat")


# ---------------------------------------------------------------------------
# handle_bind tests
# ---------------------------------------------------------------------------

async def test_handle_bind_returns_project_name_when_found(project, replier):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    result = await handle_bind("oc_chat", project.name, config, replier)
    assert result == project.name


async def test_handle_bind_returns_none_when_not_found(replier):
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    result = await handle_bind("oc_chat", "missing", config, replier)
    assert result is None


async def test_handle_bind_sends_confirmation_when_found(project, replier):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_bind("oc_chat", project.name, config, replier)
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args[0][1]
    assert project.name in text_arg


async def test_handle_bind_sends_error_when_not_found(replier):
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    await handle_bind("oc_chat", "missing", config, replier)
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args[0][1]
    assert "missing" in text_arg


async def test_handle_bind_sends_to_correct_chat(project, replier):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_bind("oc_target_chat", project.name, config, replier)
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_target_chat"


async def test_handle_bind_handles_send_text_exception_gracefully(project):
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise; result is still the project name
    result = await handle_bind("oc_chat", project.name, config, bad_replier)
    assert result == project.name


# ---------------------------------------------------------------------------
# handle_unbind tests
# ---------------------------------------------------------------------------

async def test_handle_unbind_returns_true(replier):
    result = await handle_unbind("oc_chat", replier)
    assert result is True


async def test_handle_unbind_sends_confirmation(replier):
    await handle_unbind("oc_chat", replier)
    replier.send_text.assert_awaited_once()
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_unbind_handles_send_text_exception_gracefully():
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise; return True regardless
    result = await handle_unbind("oc_chat", bad_replier)
    assert result is True


# ---------------------------------------------------------------------------
# handle_remember tests
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_manager():
    from unittest.mock import AsyncMock, MagicMock
    mgr = MagicMock()
    mgr.load = AsyncMock()
    mgr.add_fact = MagicMock()
    mgr.get_top_facts = MagicMock(return_value=[])
    return mgr


async def test_handle_remember_sends_confirmation(memory_manager, replier):
    await handle_remember("ou_user", "I like Python", memory_manager, replier, "oc_chat")
    replier.send_text.assert_awaited_once()
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "oc_chat"
    assert "I like Python" in call_args[1]


async def test_handle_remember_calls_add_fact(memory_manager, replier):
    await handle_remember("ou_user", "remember this", memory_manager, replier, "oc_chat")
    memory_manager.add_fact.assert_called_once()
    fact = memory_manager.add_fact.call_args[0][1]
    assert fact.text == "remember this"
    assert fact.source == "user_command"


async def test_handle_remember_loads_memory_before_add(memory_manager, replier):
    # user_id (not context_id) is passed as the memory key
    await handle_remember("ou_user", "some fact", memory_manager, replier, "oc_chat")
    memory_manager.load.assert_awaited_once_with("ou_user")


async def test_handle_remember_sends_to_correct_chat(memory_manager, replier):
    await handle_remember("ou_user", "fact", memory_manager, replier, "target_chat")
    call_args = replier.send_text.call_args[0]
    assert call_args[0] == "target_chat"


async def test_handle_remember_handles_send_text_exception_gracefully(memory_manager):
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_remember("ou_user", "fact", memory_manager, bad_replier, "oc_chat")


async def test_handle_remember_handles_memory_manager_exception_gracefully(replier):
    bad_mgr = MagicMock()
    bad_mgr.load = AsyncMock(side_effect=RuntimeError("load failed"))
    bad_mgr.add_fact = MagicMock()
    # Should not raise; still tries to send confirmation
    await handle_remember("ou_user", "fact", bad_mgr, replier, "oc_chat")


# ---------------------------------------------------------------------------
# handle_done tests
# ---------------------------------------------------------------------------

class TestHandleDone:
    @pytest.mark.asyncio
    async def test_done_cancels_task_and_sends_reaction(self):
        """handle_done cancels active task, clears queue, sends DONE reaction."""
        from nextme.core.commands import handle_done
        from nextme.protocol.types import Task, TaskStatus
        from unittest.mock import AsyncMock, MagicMock
        import uuid, asyncio

        replier = MagicMock()
        replier.send_reaction = AsyncMock()
        replier.reply_text = AsyncMock()

        session = MagicMock()
        session.context_id = "oc_G:om_root1"
        session.project_name = "proj"
        active_task = MagicMock()
        active_task.canceled = False
        session.active_task = active_task
        session.perm_future = None
        queue = asyncio.Queue()
        await queue.put(MagicMock())   # one pending item
        session.task_queue = queue
        session.pending_tasks = [MagicMock()]

        acp_registry = MagicMock()
        acp_registry.remove = AsyncMock()

        on_thread_closed = MagicMock()

        await handle_done(
            session=session,
            runtime=None,
            replier=replier,
            chat_id="oc_G",
            root_message_id="om_root1",
            acp_registry=acp_registry,
            on_thread_closed=on_thread_closed,
        )

        # Task cancelled
        assert active_task.canceled is True
        # Queue drained
        assert session.task_queue.empty()
        assert session.pending_tasks == []
        # Runtime removed (key format: context_id:project_name)
        acp_registry.remove.assert_called_once()
        # Thread slot released
        on_thread_closed.assert_called_once()
        # Reaction sent
        replier.send_reaction.assert_called_once_with("om_root1", "DONE")

    @pytest.mark.asyncio
    async def test_done_with_runtime_cancels_it(self):
        """When runtime is provided, handle_done calls runtime.cancel()."""
        from nextme.core.commands import handle_done
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        replier = MagicMock()
        replier.send_reaction = AsyncMock()
        replier.reply_text = AsyncMock()
        runtime = AsyncMock()
        runtime.cancel = AsyncMock()
        session = MagicMock()
        session.context_id = "oc_G:om_root1"
        session.project_name = "proj"
        session.active_task = None
        session.perm_future = None
        session.task_queue = asyncio.Queue()
        session.pending_tasks = []

        acp_registry = MagicMock()
        acp_registry.remove = AsyncMock()

        await handle_done(
            session=session,
            runtime=runtime,
            replier=replier,
            chat_id="oc_G",
            root_message_id="om_root1",
            acp_registry=acp_registry,
            on_thread_closed=MagicMock(),
        )

        runtime.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# reply_msg_id branches
# ---------------------------------------------------------------------------

def _make_replier():
    """Create a full-featured mock replier including reply_text and reply_card."""
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="msg_123")
    r.reply_text = AsyncMock()
    r.reply_card = AsyncMock()
    r.update_card = AsyncMock()
    r.build_help_card = MagicMock(return_value='{"card": "help"}')
    r.send_reaction = AsyncMock()
    r.build_acl_list_card = MagicMock(return_value='{"card": "acl"}')
    r.build_whoami_card = MagicMock(return_value='{"card": "whoami"}')
    r.build_acl_pending_card = MagicMock(return_value='{"card": "pending"}')
    return r


async def test_handle_new_reply_msg_id_uses_reply_text(session):
    """handle_new with reply_msg_id sends reply_text in_thread instead of send_text."""
    r = _make_replier()
    await handle_new(session, None, r, "oc_chat", reply_msg_id="om_123")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_123"
    assert kwargs.get("in_thread") is True
    r.send_text.assert_not_awaited()


async def test_handle_stop_reply_msg_id_uses_reply_text(session):
    """handle_stop with reply_msg_id sends reply_text in_thread."""
    r = _make_replier()
    await handle_stop(session, r, "oc_chat", reply_msg_id="om_456")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_456"
    assert kwargs.get("in_thread") is True
    r.send_text.assert_not_awaited()


async def test_handle_stop_runtime_cancel_exception_swallowed(session):
    """handle_stop swallows runtime.cancel() exceptions and still sends reply."""
    r = _make_replier()
    runtime = MagicMock()
    runtime.cancel = AsyncMock(side_effect=RuntimeError("cancel failed"))
    await handle_stop(session, r, "oc_chat", runtime=runtime)
    r.send_text.assert_awaited_once()


async def test_handle_help_reply_msg_id_uses_reply_card():
    """handle_help with reply_msg_id calls reply_card in_thread."""
    r = _make_replier()
    await handle_help(r, "oc_chat", reply_msg_id="om_help")
    r.reply_card.assert_awaited_once()
    args, kwargs = r.reply_card.call_args
    assert args[0] == "om_help"
    assert kwargs.get("in_thread") is True
    r.send_card.assert_not_awaited()


async def test_handle_status_empty_reply_msg_id():
    """handle_status with empty sessions and reply_msg_id calls reply_text."""
    r = _make_replier()
    user_ctx = UserContext("oc_chat:ou_user")
    await handle_status(user_ctx, r, "oc_chat", reply_msg_id="om_st")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_st"
    assert kwargs.get("in_thread") is True


async def test_handle_status_empty_exception_swallowed():
    """handle_status with empty sessions swallows reply exceptions."""
    user_ctx = UserContext("oc_chat:ou_user")
    r = _make_replier()
    r.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    # Should not raise
    await handle_status(user_ctx, r, "oc_chat")


async def test_handle_status_with_actual_id(tmp_path):
    """handle_status shows truncated actual_id when session.actual_id is set."""
    import json
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    user_ctx.sessions["proj"].actual_id = "abc123def456ghi7"
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat")
    card_json = r.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "abc123def456ghi7" in content


async def test_handle_status_executing_no_actual_id(tmp_path):
    """handle_status shows '初始化中' when status=executing but actual_id is empty."""
    import json
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    sess = user_ctx.sessions["proj"]
    sess.actual_id = ""
    sess.status = TaskStatus.EXECUTING  # "executing"
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat")
    card_json = r.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "初始化中" in content


async def test_handle_status_active_task_long_content(tmp_path):
    """handle_status truncates active_task.content longer than 50 chars."""
    import json
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    sess = user_ctx.sessions["proj"]
    long_content = "A" * 60  # 60 chars > 50 limit
    active_task = MagicMock()
    active_task.content = long_content
    sess.active_task = active_task
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat")
    card_json = r.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "…" in content  # truncated


async def test_handle_status_reply_msg_id_uses_reply_card(tmp_path):
    """handle_status with sessions and reply_msg_id calls reply_card."""
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat", reply_msg_id="om_st2")
    r.reply_card.assert_awaited_once()
    args, kwargs = r.reply_card.call_args
    assert args[0] == "om_st2"
    assert kwargs.get("in_thread") is True


# ---------------------------------------------------------------------------
# _get_git_branch tests
# ---------------------------------------------------------------------------

def test_get_git_branch_returns_branch_name(tmp_path):
    """Returns branch name for a valid git repo."""
    # Create a minimal git repo by writing .git/* directly.
    # Avoids subprocess git calls whose behaviour varies when GIT_DIR is set
    # in the environment (e.g. inside a git pre-commit hook).
    git_dir = tmp_path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/my-branch\n")
    assert _get_git_branch(str(tmp_path)) == "my-branch"


def test_get_git_branch_returns_none_for_non_git_dir(tmp_path):
    """Returns None when the path is not a git repository."""
    assert _get_git_branch(str(tmp_path)) is None


def test_get_git_branch_returns_none_on_exception():
    """Returns None when git raises (e.g. path doesn't exist)."""
    assert _get_git_branch("/nonexistent/path/xyz") is None


# ---------------------------------------------------------------------------
# handle_status branch-line tests
# ---------------------------------------------------------------------------

async def test_handle_status_shows_git_branch(tmp_path):
    """handle_status includes branch line when project path is a git repo."""
    import json
    # Create a minimal git repo by writing .git/* directly.
    git_dir = tmp_path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/feat/demo\n")
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat")
    card_json = r.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "feat/demo" in content


async def test_handle_status_no_branch_line_for_non_git(tmp_path):
    """handle_status omits branch line when project path is not a git repo."""
    import json
    settings = Settings(task_queue_capacity=5)
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    user_ctx.get_or_create_session(project, settings)
    r = _make_replier()
    await handle_status(user_ctx, r, "oc_chat")
    card_json = r.send_card.call_args[0][1]
    content = json.loads(card_json)["body"]["elements"][0]["content"]
    assert "🌿" not in content


async def test_handle_bind_not_found_reply_msg_id():
    """handle_bind when project not found with reply_msg_id calls reply_text."""
    r = _make_replier()
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    result = await handle_bind("oc_chat", "missing", config, r, reply_msg_id="om_bind")
    assert result is None
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_bind"
    assert kwargs.get("in_thread") is True


async def test_handle_bind_not_found_exception_swallowed(tmp_path):
    """handle_bind not found swallows send_text exceptions."""
    r = _make_replier()
    r.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    result = await handle_bind("oc_chat", "missing", config, r)
    assert result is None  # still returns None


async def test_handle_bind_found_reply_msg_id(tmp_path):
    """handle_bind with found project and reply_msg_id calls reply_text."""
    r = _make_replier()
    project = Project(name="myproj", path=str(tmp_path), executor="claude")
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    result = await handle_bind("oc_chat", "myproj", config, r, reply_msg_id="om_bind2")
    assert result == "myproj"
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_bind2"
    assert kwargs.get("in_thread") is True


async def test_handle_unbind_reply_msg_id():
    """handle_unbind with reply_msg_id calls reply_text in_thread."""
    r = _make_replier()
    result = await handle_unbind("oc_chat", r, reply_msg_id="om_unbind")
    assert result is True
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_unbind"
    assert kwargs.get("in_thread") is True


async def test_handle_remember_reply_msg_id(memory_manager):
    """handle_remember with reply_msg_id calls reply_text in_thread."""
    r = _make_replier()
    await handle_remember("ou_user", "some fact", memory_manager, r, "oc_chat", reply_msg_id="om_rem")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_rem"
    assert kwargs.get("in_thread") is True


async def test_handle_project_not_found_reply_msg_id(tmp_path):
    """handle_project not found with reply_msg_id calls reply_text."""
    r = _make_replier()
    user_ctx = UserContext("oc_chat:ou_user")
    settings = Settings(task_queue_capacity=5)
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    await handle_project(user_ctx, "missing", config, settings, r, "oc_chat", reply_msg_id="om_proj")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_proj"
    assert kwargs.get("in_thread") is True


async def test_handle_project_not_found_exception_swallowed(tmp_path):
    """handle_project not found swallows reply exceptions."""
    r = _make_replier()
    r.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    user_ctx = UserContext("oc_chat:ou_user")
    settings = Settings(task_queue_capacity=5)
    config = AppConfig(app_id="x", app_secret="y", projects=[])
    # Should not raise
    await handle_project(user_ctx, "missing", config, settings, r, "oc_chat")


async def test_handle_project_found_reply_msg_id(tmp_path):
    """handle_project found with reply_msg_id calls reply_card."""
    r = _make_replier()
    project = Project(name="myproj", path=str(tmp_path), executor="claude")
    user_ctx = UserContext("oc_chat:ou_user")
    settings = Settings(task_queue_capacity=5)
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    await handle_project(user_ctx, "myproj", config, settings, r, "oc_chat", reply_msg_id="om_proj2")
    r.reply_card.assert_awaited_once()
    args, kwargs = r.reply_card.call_args
    assert args[0] == "om_proj2"
    assert kwargs.get("in_thread") is True


# ---------------------------------------------------------------------------
# handle_done exception paths
# ---------------------------------------------------------------------------

async def test_handle_done_perm_future_cancelled():
    """handle_done cancels perm_future when session has no cancel_permission."""
    import asyncio
    replier = _make_replier()
    session = MagicMock(spec=[
        "context_id", "project_name", "active_task",
        "perm_future", "task_queue", "pending_tasks",
    ])
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    perm_future = asyncio.get_event_loop().create_future()
    session.perm_future = perm_future
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []

    from nextme.core.commands import handle_done
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    await handle_done(
        session=session,
        runtime=None,
        replier=replier,
        chat_id="oc_G",
        root_message_id="om_root",
        acp_registry=acp_registry,
        on_thread_closed=MagicMock(),
    )
    assert perm_future.cancelled()


async def test_handle_done_runtime_cancel_exception_swallowed():
    """handle_done swallows runtime.cancel() exceptions."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []
    runtime = AsyncMock()
    runtime.cancel = AsyncMock(side_effect=RuntimeError("cancel boom"))
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    # Should not raise
    await handle_done(
        session=session, runtime=runtime, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=MagicMock(),
    )
    replier.send_reaction.assert_awaited()


async def test_handle_done_queue_drain_exception_swallowed():
    """handle_done handles queue.get_nowait() raising an exception."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None

    # Create a queue that reports non-empty but raises on get_nowait
    bad_queue = MagicMock()
    bad_queue.empty = MagicMock(side_effect=[False, True])  # first call: non-empty, then empty
    bad_queue.get_nowait = MagicMock(side_effect=RuntimeError("queue error"))
    session.task_queue = bad_queue
    session.pending_tasks = []
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    # Should not raise
    await handle_done(
        session=session, runtime=None, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=MagicMock(),
    )


async def test_handle_done_acp_registry_remove_exception_swallowed():
    """handle_done swallows acp_registry.remove() exceptions."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock(side_effect=RuntimeError("remove boom"))
    on_cb = MagicMock()
    # Should not raise
    await handle_done(
        session=session, runtime=None, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=on_cb,
    )
    on_cb.assert_called_once()  # on_thread_closed still called


async def test_handle_done_on_thread_closed_exception_swallowed():
    """handle_done swallows on_thread_closed exceptions."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    bad_cb = MagicMock(side_effect=RuntimeError("cb boom"))
    # Should not raise
    await handle_done(
        session=session, runtime=None, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=bad_cb,
    )
    replier.send_reaction.assert_awaited()


async def test_handle_done_send_reaction_exception_swallowed():
    """handle_done swallows send_reaction exceptions."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    replier.send_reaction = AsyncMock(side_effect=RuntimeError("reaction boom"))
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    # Should not raise; reply_text still called
    await handle_done(
        session=session, runtime=None, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=MagicMock(),
    )
    replier.reply_text.assert_awaited()


async def test_handle_done_reply_text_exception_swallowed():
    """handle_done swallows reply_text exceptions."""
    import asyncio
    from nextme.core.commands import handle_done
    replier = _make_replier()
    replier.reply_text = AsyncMock(side_effect=RuntimeError("reply boom"))
    session = MagicMock()
    session.context_id = "oc_G:ou_user"
    session.project_name = "proj"
    session.active_task = None
    session.task_queue = asyncio.Queue()
    session.pending_tasks = []
    acp_registry = MagicMock()
    acp_registry.remove = AsyncMock()
    # Should not raise
    await handle_done(
        session=session, runtime=None, replier=replier,
        chat_id="oc_G", root_message_id="om_root",
        acp_registry=acp_registry, on_thread_closed=MagicMock(),
    )


# ---------------------------------------------------------------------------
# handle_threads_list tests
# ---------------------------------------------------------------------------

async def test_handle_threads_list_empty_sends_text():
    """handle_threads_list with empty list calls send_text."""
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    await handle_threads_list("oc_chat", [], r)
    r.send_text.assert_awaited_once()
    assert "没有活跃话题" in r.send_text.call_args[0][1]


async def test_handle_threads_list_empty_reply_msg_id():
    """handle_threads_list empty list + reply_msg_id calls reply_text in_thread."""
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    await handle_threads_list("oc_chat", [], r, reply_msg_id="om_th")
    r.reply_text.assert_awaited_once()
    args, kwargs = r.reply_text.call_args
    assert args[0] == "om_th"
    assert kwargs.get("in_thread") is True


async def test_handle_threads_list_empty_exception_swallowed():
    """handle_threads_list empty list swallows send exceptions."""
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    r.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    # Should not raise
    await handle_threads_list("oc_chat", [], r)


async def test_handle_threads_list_non_empty_sends_card():
    """handle_threads_list with threads sends a card."""
    import json, datetime
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    thread = MagicMock()
    thread.thread_root_id = "om_rootabc12345"
    thread.project_name = "myproj"
    thread.created_at = datetime.datetime(2024, 1, 15, 10, 30)
    await handle_threads_list("oc_chat", [thread], r)
    r.send_card.assert_awaited_once()
    card_json = r.send_card.call_args[0][1]
    card = json.loads(card_json)
    assert "活跃话题" in card["header"]["title"]["content"]


async def test_handle_threads_list_non_empty_reply_msg_id():
    """handle_threads_list with threads and reply_msg_id calls reply_card."""
    import datetime
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    thread = MagicMock()
    thread.thread_root_id = "om_rootabc12345"
    thread.project_name = "myproj"
    thread.created_at = datetime.datetime(2024, 1, 15, 10, 30)
    await handle_threads_list("oc_chat", [thread], r, reply_msg_id="om_th2")
    r.reply_card.assert_awaited_once()
    args, kwargs = r.reply_card.call_args
    assert args[0] == "om_th2"
    assert kwargs.get("in_thread") is True


async def test_handle_threads_list_non_empty_exception_swallowed():
    """handle_threads_list with threads swallows send_card exceptions."""
    import datetime
    from nextme.core.commands import handle_threads_list
    r = _make_replier()
    r.send_card = AsyncMock(side_effect=RuntimeError("boom"))
    thread = MagicMock()
    thread.thread_root_id = "om_rootabc12345"
    thread.project_name = "myproj"
    thread.created_at = datetime.datetime(2024, 1, 15, 10, 30)
    # Should not raise
    await handle_threads_list("oc_chat", [thread], r)


# ---------------------------------------------------------------------------
# ACL command tests
# ---------------------------------------------------------------------------

@pytest.fixture
async def acl_db(tmp_path):
    """Return a real AclDb backed by a temp sqlite file."""
    from nextme.acl.db import AclDb
    db_path = tmp_path / "test.db"
    db = AclDb(db_path=db_path)
    await db.open()
    return db


@pytest.fixture
def acl_manager(acl_db):
    from nextme.acl.manager import AclManager
    return AclManager(db=acl_db, admin_users=["ou_admin"])


async def test_handle_acl_list_sends_card(acl_manager):
    """handle_acl_list sends a card with admin/owner/collaborator info."""
    from nextme.core.commands import handle_acl_list
    r = _make_replier()
    await handle_acl_list(acl_manager, r, "oc_chat")
    r.send_card.assert_awaited_once()


async def test_handle_acl_list_exception_swallowed(acl_manager):
    """handle_acl_list swallows send_card exceptions."""
    from nextme.core.commands import handle_acl_list
    r = _make_replier()
    r.send_card = AsyncMock(side_effect=RuntimeError("boom"))
    # Should not raise
    await handle_acl_list(acl_manager, r, "oc_chat")


async def test_handle_acl_add_unknown_role(acl_manager):
    """handle_acl_add with unknown role string sends error text."""
    from nextme.core.commands import handle_acl_add
    from nextme.acl.schema import Role
    r = _make_replier()
    await handle_acl_add("ou_admin", Role.ADMIN, "ou_target", "superuser", acl_manager, r, "oc_chat")
    r.send_text.assert_awaited_once()
    assert "未知角色" in r.send_text.call_args[0][1]


async def test_handle_acl_add_db_error(acl_manager):
    """handle_acl_add swallows db errors and sends failure message."""
    from nextme.core.commands import handle_acl_add
    from nextme.acl.schema import Role
    r = _make_replier()
    # Patch add_user to raise
    acl_manager.add_user = AsyncMock(side_effect=RuntimeError("db error"))
    await handle_acl_add("ou_admin", Role.ADMIN, "ou_target", "collaborator", acl_manager, r, "oc_chat")
    r.send_text.assert_awaited()
    last_text = r.send_text.call_args[0][1]
    assert "失败" in last_text


async def test_handle_acl_remove_valueerror(acl_manager):
    """handle_acl_remove sends ValueError message as text."""
    from nextme.core.commands import handle_acl_remove
    from nextme.acl.schema import Role
    import datetime
    r = _make_replier()
    # Add a user first so get_user returns something
    from nextme.acl.schema import AclUser
    acl_manager.get_user = AsyncMock(return_value=AclUser(
        open_id="ou_target",
        role=Role.COLLABORATOR,
        added_by="ou_admin",
        added_at=datetime.datetime.now(),
    ))
    acl_manager.can_remove = MagicMock(return_value=True)
    acl_manager.remove_user = AsyncMock(side_effect=ValueError("cannot remove last owner"))
    await handle_acl_remove("ou_admin", Role.OWNER, "ou_target", acl_manager, r, "oc_chat")
    r.send_text.assert_awaited()
    last_text = r.send_text.call_args[0][1]
    assert "cannot remove last owner" in last_text


async def test_handle_acl_remove_generic_exception(acl_manager):
    """handle_acl_remove swallows generic exceptions and sends failure message."""
    from nextme.core.commands import handle_acl_remove
    from nextme.acl.schema import Role, AclUser
    import datetime
    r = _make_replier()
    acl_manager.get_user = AsyncMock(return_value=AclUser(
        open_id="ou_target",
        role=Role.COLLABORATOR,
        added_by="ou_admin",
        added_at=datetime.datetime.now(),
    ))
    acl_manager.can_remove = MagicMock(return_value=True)
    acl_manager.remove_user = AsyncMock(side_effect=RuntimeError("db crash"))
    await handle_acl_remove("ou_admin", Role.OWNER, "ou_target", acl_manager, r, "oc_chat")
    last_text = r.send_text.call_args[0][1]
    assert "失败" in last_text


async def test_handle_acl_approve_result_none(acl_manager):
    """handle_acl_approve sends 'already processed' if approve returns None."""
    from nextme.core.commands import handle_acl_approve
    from nextme.acl.schema import Role
    r = _make_replier()
    # Simulate app exists but approve returns None (already processed)
    from nextme.acl.schema import AclApplication
    import datetime
    mock_app = AclApplication(
        id=99, applicant_id="ou_x", requested_role=Role.COLLABORATOR,
        status="pending", requested_at=datetime.datetime.now(),
    )
    acl_manager.get_application = AsyncMock(return_value=mock_app)
    acl_manager.can_review = MagicMock(return_value=True)
    acl_manager.approve = AsyncMock(return_value=None)
    await handle_acl_approve(99, "ou_admin", Role.ADMIN, acl_manager, r, "oc_chat")
    r.send_text.assert_awaited()
    assert "已处理或不存在" in r.send_text.call_args[0][1]


async def test_handle_acl_reject_cannot_review(acl_manager):
    """handle_acl_reject sends permission error when reviewer lacks permission."""
    from nextme.core.commands import handle_acl_reject
    from nextme.acl.schema import Role, AclApplication
    import datetime
    r = _make_replier()
    mock_app = AclApplication(
        id=77, applicant_id="ou_y", requested_role=Role.OWNER,
        status="pending", requested_at=datetime.datetime.now(),
    )
    acl_manager.get_application = AsyncMock(return_value=mock_app)
    acl_manager.can_review = MagicMock(return_value=False)
    await handle_acl_reject(77, "ou_collab", Role.COLLABORATOR, acl_manager, r, "oc_chat")
    r.send_text.assert_awaited_once()
    assert "权限不足" in r.send_text.call_args[0][1]


async def test_handle_acl_reject_result_none(acl_manager):
    """handle_acl_reject sends 'already processed' if reject returns None."""
    from nextme.core.commands import handle_acl_reject
    from nextme.acl.schema import Role, AclApplication
    import datetime
    r = _make_replier()
    mock_app = AclApplication(
        id=88, applicant_id="ou_z", requested_role=Role.COLLABORATOR,
        status="pending", requested_at=datetime.datetime.now(),
    )
    acl_manager.get_application = AsyncMock(return_value=mock_app)
    acl_manager.can_review = MagicMock(return_value=True)
    acl_manager.reject = AsyncMock(return_value=None)
    await handle_acl_reject(88, "ou_admin", Role.ADMIN, acl_manager, r, "oc_chat")
    assert "已处理或不存在" in r.send_text.call_args[0][1]
