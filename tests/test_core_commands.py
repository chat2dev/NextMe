"""Unit tests for nextme.core.commands."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.core.commands import (
    HELP_COMMANDS, handle_new, handle_stop, handle_help,
    handle_status, handle_project, handle_bind, handle_unbind, handle_remember,
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
    assert len(HELP_COMMANDS) == 12


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
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args[0][1]
    # Confirmation message should mention the project name
    assert project.name in text_arg


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
    call_args = replier.send_text.call_args[0]
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
    """handle_project should not propagate exceptions from replier.send_text."""
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    bad_replier = MagicMock()
    bad_replier.send_text = AsyncMock(side_effect=RuntimeError("send failed"))
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
