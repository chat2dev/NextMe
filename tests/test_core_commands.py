"""Unit tests for nextme.core.commands."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.core.commands import (
    HELP_COMMANDS, handle_new, handle_stop, handle_help,
    handle_status, handle_project,
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
    assert len(HELP_COMMANDS) == 6


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

async def test_handle_status_calls_send_card(session, replier):
    await handle_status(session, replier, "oc_chat")
    replier.send_card.assert_awaited_once()
    call_args = replier.send_card.call_args[0]
    assert call_args[0] == "oc_chat"


async def test_handle_status_card_contains_session_info(session, replier):
    import json
    await handle_status(session, replier, "oc_chat")
    call_args = replier.send_card.call_args[0]
    card_json = call_args[1]
    card = json.loads(card_json)
    # Verify card structure
    assert card["schema"] == "2.0"
    assert "body" in card
    elements = card["body"]["elements"]
    assert len(elements) >= 1
    content = elements[0]["content"]
    # Should contain project name
    assert session.project_name in content


async def test_handle_status_handles_exception_gracefully(session):
    """handle_status should not propagate exceptions from replier."""
    bad_replier = MagicMock()
    bad_replier.send_card = AsyncMock(side_effect=RuntimeError("send failed"))
    # Should not raise
    await handle_status(session, bad_replier, "oc_chat")


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
