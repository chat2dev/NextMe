"""Unit tests for nextme.core.worker.SessionWorker."""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.core.worker import SessionWorker
from nextme.core.session import Session
from nextme.core.path_lock import PathLockRegistry
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import Project, Settings
from nextme.protocol.types import (
    Task, Reply, ReplyType, TaskStatus,
    PermissionRequest, PermissionChoice, PermOption,
)
import datetime
import uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(content="hello", canceled=False):
    replies = []

    async def reply_fn(r):
        replies.append(r)

    task = Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id="oc_chat:ou_user",
        reply_fn=reply_fn,
        canceled=canceled,
    )
    return task, replies


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    return Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,  # no debounce for tests
    )


@pytest.fixture
def project(tmp_path):
    return Project(name="p", path=str(tmp_path), executor="mock-acp")


@pytest.fixture
def session(project, settings):
    return Session("oc_chat:ou_user", project, settings)


@pytest.fixture
def replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="msg_123")
    r.send_card_by_id = AsyncMock(return_value="msg_by_id_123")
    r.update_card = AsyncMock()
    r.reply_text = AsyncMock(return_value="thread_msg_456")
    r.reply_card = AsyncMock(return_value="thread_card_789")
    r.reply_card_by_id = AsyncMock(return_value="thread_card_by_id_789")
    # Cardkit streaming — create_card returns "" by default so worker falls back to debounce path.
    r.create_card = AsyncMock(return_value="")
    r.get_card_id = AsyncMock(return_value="")
    r.stream_set_content = AsyncMock()
    r.update_card_entity = AsyncMock()
    r.build_help_card = MagicMock(return_value='{"card": "help"}')
    r.build_permission_card = MagicMock(return_value='{"card": "perm"}')
    r.build_streaming_progress_card = MagicMock(return_value='{"card": "streaming_progress"}')
    r.build_progress_card = MagicMock(return_value='{"card": "progress"}')
    r.build_result_card = MagicMock(return_value='{"card": "result"}')
    r.build_error_card = MagicMock(return_value='{"card": "error"}')
    return r


@pytest.fixture
def acp_registry():
    registry = MagicMock(spec=ACPRuntimeRegistry)
    mock_runtime = AsyncMock()
    mock_runtime.actual_id = "acp-session-123"
    mock_runtime.ensure_ready = AsyncMock()
    mock_runtime.execute = AsyncMock(return_value="Final result")
    registry.get_or_create = MagicMock(return_value=mock_runtime)
    return registry, mock_runtime


@pytest.fixture
def path_lock_registry():
    return PathLockRegistry()


@pytest.fixture
def worker(session, acp_registry, replier, settings, path_lock_registry):
    registry, _ = acp_registry
    return SessionWorker(session, registry, replier, settings, path_lock_registry)


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------

async def test_run_processes_task(worker, session, acp_registry):
    _, mock_runtime = acp_registry
    task, replies = make_task("test message")
    await session.task_queue.put(task)
    task_runner = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)  # let it process
    task_runner.cancel()
    try:
        await task_runner
    except asyncio.CancelledError:
        pass
    # Task should have been executed
    assert mock_runtime.execute.called or len(replies) > 0


async def test_run_skips_canceled_task(worker, session, acp_registry):
    _, mock_runtime = acp_registry
    task, replies = make_task("canceled message", canceled=True)
    await session.task_queue.put(task)
    task_runner = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    task_runner.cancel()
    try:
        await task_runner
    except asyncio.CancelledError:
        pass
    # Canceled task should NOT have been executed
    mock_runtime.execute.assert_not_called()
    # And no reply sent for canceled-before-dequeue task
    assert len(replies) == 0


async def test_run_dequeues_multiple_tasks(worker, session, acp_registry):
    _, mock_runtime = acp_registry
    task1, replies1 = make_task("msg1")
    task2, replies2 = make_task("msg2")
    await session.task_queue.put(task1)
    await session.task_queue.put(task2)
    task_runner = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    task_runner.cancel()
    try:
        await task_runner
    except asyncio.CancelledError:
        pass
    assert mock_runtime.execute.call_count >= 1


# ---------------------------------------------------------------------------
# _send_result tests
# ---------------------------------------------------------------------------

async def test_send_result_updates_progress_card_in_place(worker, replier):
    """Non-streaming result updates the existing progress card in place (no new card)."""
    worker._progress_message_id = "prog_msg_id"
    worker._card_id = None  # non-streaming mode: PATCH allowed
    task, _ = make_task("hello")
    await worker._send_result(task, "Great result")
    replier.update_card.assert_awaited_once()
    # update_card called with the progress card id
    assert replier.update_card.call_args.args[0] == "prog_msg_id"
    replier.reply_card.assert_not_awaited()


async def test_send_result_uses_build_result_card(worker, replier):
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_result(task, "Some content")
    replier.build_result_card.assert_called_once()
    kwargs = replier.build_result_card.call_args.kwargs
    assert kwargs.get("content") == "Some content"


async def test_send_result_handles_empty_content(worker, replier):
    """Empty content is replaced with '(无输出)'."""
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_result(task, "")
    call_args = replier.build_result_card.call_args
    content_arg = call_args.kwargs.get("content") or call_args.args[0]
    assert "(无输出)" in str(content_arg)


async def test_send_result_fallback_uses_reply_card_when_no_progress_card(worker, replier):
    """When progress card was never sent, falls back to reply_card."""
    worker._progress_message_id = None
    task, _ = make_task("hello")
    task.message_id = "om_src"
    await worker._send_result(task, "result")
    replier.reply_card.assert_awaited()
    replier.update_card.assert_not_awaited()


async def test_send_result_falls_back_to_reply_card_when_update_card_fails(worker, replier):
    """When update_card raises (e.g. network error), reply_card is used so the result
    is never silently dropped."""
    worker._progress_message_id = "prog_msg_id"
    worker._card_id = None  # non-streaming mode
    replier.update_card.side_effect = Exception("API error")
    task, _ = make_task("hello")
    task.message_id = "om_src"
    await worker._send_result(task, "result")
    replier.update_card.assert_awaited_once()
    replier.reply_card.assert_awaited_once()


async def test_send_result_finalizes_streaming_card_via_update_card_entity(worker, replier):
    """In streaming mode (_card_id set), result replaces the full card entity.

    update_card_entity (PUT /cards/:card_id) atomically updates the header
    title to "完成" and template to "blue" — no duplicate reply card is sent.
    """
    worker._progress_message_id = "om_streaming_msg"
    worker._card_id = "ck_card_123"          # streaming mode
    task, _ = make_task("hello")
    task.message_id = "om_original_src"
    await worker._send_result(task, "great result")
    replier.update_card.assert_not_awaited()
    replier.reply_card.assert_not_awaited()
    replier.send_card.assert_not_awaited()
    # Finalized via full card replace (updates header title/template).
    replier.update_card_entity.assert_awaited_once()
    # build_result_card was called to generate the final card.
    replier.build_result_card.assert_called()


async def test_send_result_streaming_mode_falls_back_when_update_card_entity_fails(worker, replier):
    """When update_card_entity fails in streaming mode, fall back to reply_card."""
    worker._progress_message_id = "om_streaming_msg"
    worker._card_id = "ck_card_123"
    replier.update_card_entity.side_effect = Exception("cardkit down")
    task, _ = make_task("hello")
    task.message_id = "om_original_src"
    await worker._send_result(task, "great result")
    replier.update_card_entity.assert_awaited_once()
    # Fell back to new reply after update_card_entity failure.
    replier.reply_card.assert_awaited_once()
    assert replier.reply_card.call_args.args[0] == "om_original_src"


# ---------------------------------------------------------------------------
# _send_error tests
# ---------------------------------------------------------------------------

async def test_send_error_updates_progress_card_in_place(worker, replier):
    """Error updates the existing progress card (no new card)."""
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_error(task, "Something went wrong")
    replier.update_card.assert_awaited_once()
    assert replier.update_card.call_args.args[0] == "prog_msg_id"


async def test_send_error_uses_build_error_card(worker, replier):
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_error(task, "Error message")
    replier.build_error_card.assert_called_once_with("Error message", title=f"出错了 【{worker._session.project_name}】")


# ---------------------------------------------------------------------------
# _send_cancelled tests
# ---------------------------------------------------------------------------

async def test_send_cancelled_updates_progress_card_in_place(worker, replier):
    """Cancellation updates the existing progress card (no new card)."""
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_cancelled(task)
    replier.update_card.assert_awaited_once()
    assert replier.update_card.call_args.args[0] == "prog_msg_id"


# ---------------------------------------------------------------------------
# _on_progress tests
# ---------------------------------------------------------------------------

async def test_on_progress_updates_card_when_message_id_exists(worker, replier):
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0  # ensure enough time passed
    await worker._on_progress("hello", "")
    replier.update_card.assert_awaited_once()


async def test_on_progress_debounces_when_not_enough_time_elapsed(worker, replier, settings):
    # Use a very high debounce so it won't fire
    worker._settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=10.0,  # very long debounce
        streaming_enabled=True,
    )
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = time.monotonic()  # just now
    await worker._on_progress("hello", "")
    # Should NOT have updated the card because debounce hasn't elapsed
    replier.update_card.assert_not_awaited()


async def test_on_progress_sends_new_card_when_no_message_id(worker, replier):
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = None
    worker._last_progress_update = 0.0
    await worker._on_progress("some delta", "")
    replier.send_card.assert_awaited()


async def test_on_progress_skips_when_delta_empty_and_no_tool(worker, replier):
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0
    worker._progress_buffer = []  # empty buffer
    # Empty delta and no tool_name: nothing to show
    await worker._on_progress("", "")
    replier.update_card.assert_not_awaited()


async def test_on_progress_updates_when_tool_name_provided(worker, replier):
    """When tool_name is given, progress fires even if debounce not elapsed."""
    worker._settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=10.0,
        streaming_enabled=True,
    )
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = time.monotonic()  # just now
    # Provide a tool_name: this bypasses debounce
    await worker._on_progress("", "bash")
    replier.update_card.assert_awaited_once()


async def test_on_progress_accumulates_buffer(worker, replier):
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0
    await worker._on_progress("hello ", "")
    await worker._on_progress("world", "")
    # update_card should have been called for each progress event that passes debounce
    assert replier.update_card.await_count >= 1


# ---------------------------------------------------------------------------
# _on_permission tests
# ---------------------------------------------------------------------------

async def test_on_permission_success(worker, session, replier):
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-1",
        description="Allow write?",
        options=[PermOption(index=1, label="Allow"), PermOption(index=2, label="Deny")],
    )

    # Resolve permission after a short delay
    async def resolve_later():
        await asyncio.sleep(0.05)
        choice = PermissionChoice(request_id="req-1", option_index=2)
        session.resolve_permission(choice)

    asyncio.create_task(resolve_later())
    result = await worker._on_permission(req)
    assert result.option_index == 2


async def test_on_permission_sends_permission_card(worker, session, replier):
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-2",
        description="Allow read?",
        options=[PermOption(index=1, label="Allow")],
    )

    async def resolve_later():
        await asyncio.sleep(0.05)
        choice = PermissionChoice(request_id="req-2", option_index=1)
        session.resolve_permission(choice)

    asyncio.create_task(resolve_later())
    await worker._on_permission(req)
    replier.build_permission_card.assert_called_once()
    replier.send_card.assert_awaited()



async def test_on_permission_passes_context_id_as_session_id(worker, session, replier):
    """build_permission_card must receive context_id (not actual_id) as session_id.

    This is the regression test for the bug where actual_id was passed as
    session_id, causing handle_card_action to fail with "no context".
    """
    session.actual_id = "acp-session-uuid-123"
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-ctx",
        description="Write file?",
        options=[PermOption(index=1, label="Allow")],
    )

    async def resolve_later():
        await asyncio.sleep(0.05)
        session.resolve_permission(PermissionChoice(request_id="req-ctx", option_index=1))

    asyncio.create_task(resolve_later())
    await worker._on_permission(req)

    call_kwargs = replier.build_permission_card.call_args.kwargs
    # session_id must be context_id (oc_chat:ou_user) so the registry lookup works
    assert call_kwargs["session_id"] == "oc_chat:ou_user"
    # display_id must be actual_id for the footer
    assert call_kwargs["display_id"] == "acp-session-uuid-123"


async def test_on_permission_display_id_empty_when_no_actual_id(worker, session, replier):
    """display_id is empty string when session has no actual_id yet."""
    session.actual_id = None
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-no-actual",
        description="Write file?",
        options=[PermOption(index=1, label="Allow")],
    )

    async def resolve_later():
        await asyncio.sleep(0.05)
        session.resolve_permission(PermissionChoice(request_id="req-no-actual", option_index=1))

    asyncio.create_task(resolve_later())
    await worker._on_permission(req)

    call_kwargs = replier.build_permission_card.call_args.kwargs
    assert call_kwargs["session_id"] == "oc_chat:ou_user"
    assert call_kwargs["display_id"] == ""


async def test_on_permission_deny_cancels_active_task(worker, session, replier):
    """If the user chooses a deny/reject option, the session's active task is marked canceled."""
    active_task_obj, _ = make_task("running task")
    session.active_task = active_task_obj

    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-deny",
        description="Allow dangerous operation?",
        options=[
            PermOption(index=1, label="allow_once"),
            PermOption(index=2, label="deny"),
        ],
    )

    async def resolve_later():
        await asyncio.sleep(0.05)
        session.resolve_permission(PermissionChoice(request_id="req-deny", option_index=2))

    asyncio.create_task(resolve_later())
    result = await worker._on_permission(req)

    assert result.option_index == 2
    assert active_task_obj.canceled is True


async def test_on_permission_allow_does_not_cancel_active_task(worker, session, replier):
    """Allow choices do not mark the active task as canceled."""
    active_task_obj, _ = make_task("running task")
    session.active_task = active_task_obj

    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-allow",
        description="Allow safe operation?",
        options=[
            PermOption(index=1, label="allow_once"),
            PermOption(index=2, label="session_level_allow"),
        ],
    )

    async def resolve_later():
        await asyncio.sleep(0.05)
        session.resolve_permission(PermissionChoice(request_id="req-allow", option_index=1))

    asyncio.create_task(resolve_later())
    result = await worker._on_permission(req)

    assert result.option_index == 1
    assert active_task_obj.canceled is False


async def test_on_permission_auto_approve_notification_returns_immediately(worker, session, replier):
    """When options is empty (auto-approve notification), _on_permission returns
    immediately without waiting for user input."""
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-auto",
        description="Run bash command",
        options=[],  # empty = auto-approve notification
    )
    result = await worker._on_permission(req)
    assert result.option_index == 1
    assert result.request_id == "req-auto"


async def test_on_permission_auto_approve_notification_sends_text(worker, session, replier):
    """Auto-approve notification sends an informational text, not a card."""
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-auto-text",
        description="Run bash command",
        options=[],
    )
    await worker._on_permission(req)
    replier.send_text.assert_awaited_once()
    text_arg = replier.send_text.call_args.args[1]
    assert "Run bash command" in text_arg


async def test_on_permission_auto_approve_notification_does_not_build_permission_card(worker, session, replier):
    """Auto-approve notification must NOT send a clickable permission card."""
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-auto-nocard",
        description="Write file",
        options=[],
    )
    await worker._on_permission(req)
    replier.build_permission_card.assert_not_called()
    replier.send_card.assert_not_awaited()


# ---------------------------------------------------------------------------
# _execute_task integration-style tests
# ---------------------------------------------------------------------------

async def test_execute_task_sends_initial_progress_card(worker, session, replier, acp_registry):
    """streaming_enabled=False: streaming not attempted, regular card used."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": False})
    task, replies = make_task("hello")
    await worker._execute_task(task)
    # Streaming explicitly disabled — cardkit path skipped entirely
    replier.build_streaming_progress_card.assert_not_called()
    replier.create_card.assert_not_awaited()
    # Regular card is used
    replier.build_progress_card.assert_called()
    replier.send_card.assert_awaited()


async def test_execute_task_attempts_streaming_when_enabled(worker, session, replier, acp_registry):
    """streaming_enabled=True (default): cardkit path is attempted."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    await worker._execute_task(task)
    # Streaming attempted via RunProgressCard.build_card() + create_card()
    # (create_card returns "" → fallback to regular card)
    replier.create_card.assert_awaited()


async def test_execute_task_sends_result_on_success(worker, session, replier, acp_registry):
    """Success updates the progress card in-place (no new card via reply_fn)."""
    _, mock_runtime = acp_registry
    mock_runtime.execute = AsyncMock(return_value="Success content")
    task, _ = make_task("hello")
    await worker._execute_task(task)
    # build_result_card is called and the card is applied via update_card or send_card
    replier.build_result_card.assert_called()


async def test_execute_task_sends_error_on_ensure_ready_failure(
    worker, session, replier, acp_registry
):
    """ensure_ready failure updates the progress card with an error card."""
    _, mock_runtime = acp_registry
    mock_runtime.ensure_ready = AsyncMock(side_effect=RuntimeError("not ready"))
    task, _ = make_task("hello")
    await worker._execute_task(task)
    replier.build_error_card.assert_called()
    # Card is applied via update_card or send_card (not via reply_fn)
    assert replier.update_card.await_count + replier.send_card.await_count >= 1


async def test_execute_task_sends_error_on_execute_failure(
    worker, session, replier, acp_registry
):
    _, mock_runtime = acp_registry
    mock_runtime.execute = AsyncMock(side_effect=RuntimeError("execution failed"))
    task, _ = make_task("hello")
    await worker._execute_task(task)
    replier.build_error_card.assert_called()


async def test_execute_task_sends_cancelled_when_task_marked_canceled(
    worker, session, replier, acp_registry
):
    """Cancellation updates the progress card with a cancel card."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")

    async def fake_execute(**kwargs):
        task.canceled = True
        return "result"

    mock_runtime.execute = fake_execute
    await worker._execute_task(task)
    # build_result_card called with "已取消" title
    replier.build_result_card.assert_called()
    call_kwargs = replier.build_result_card.call_args.kwargs
    assert call_kwargs.get("title") == "已取消" or "已取消" in str(call_kwargs)


async def test_execute_task_syncs_actual_id_from_runtime(
    worker, session, replier, acp_registry
):
    _, mock_runtime = acp_registry
    mock_runtime.actual_id = "new-acp-id"
    session.actual_id = ""
    task, replies = make_task("hello")
    await worker._execute_task(task)
    assert session.actual_id == "new-acp-id"


# ---------------------------------------------------------------------------
# Tests: thread-mode progress card and elapsed time
# ---------------------------------------------------------------------------

async def test_execute_task_uses_reply_card_for_group_chat(
    worker, session, replier, acp_registry
):
    """Group chat: with cardkit fallback, falls back to reply_card with in_thread=True."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "group"
    # create_card returns "" (fixture default) → fallback path
    await worker._execute_task(task)
    replier.reply_card.assert_awaited()
    call_kwargs = replier.reply_card.call_args.kwargs
    assert call_kwargs.get("in_thread") is True
    replier.send_card.assert_not_awaited()
    replier.reply_card_by_id.assert_not_awaited()


async def test_execute_task_uses_reply_card_for_p2p_chat(
    worker, session, replier, acp_registry
):
    """P2P chat: with cardkit fallback, falls back to reply_card with in_thread=False."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "p2p"
    # create_card returns "" (fixture default) → fallback path
    await worker._execute_task(task)
    replier.reply_card.assert_awaited()
    call_kwargs = replier.reply_card.call_args.kwargs
    assert call_kwargs.get("in_thread") is False
    replier.send_card.assert_not_awaited()
    replier.reply_card_by_id.assert_not_awaited()


async def test_execute_task_uses_send_card_when_no_message_id(
    worker, session, replier, acp_registry
):
    """When task has no message_id and cardkit fails, falls back to send_card."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    # message_id defaults to "", create_card returns "" → fallback
    await worker._execute_task(task)
    replier.send_card.assert_awaited()
    replier.reply_card.assert_not_awaited()
    replier.send_card_by_id.assert_not_awaited()


async def test_on_progress_status_includes_elapsed_time(worker, replier):
    """Progress card status line includes elapsed time when content is present."""
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0
    worker._task_start = time.monotonic() - 5  # pretend 5s elapsed
    worker._progress_buffer = ["some content"]
    await worker._on_progress("", "")
    # build_progress_card should be called; check status contains seconds
    call_args = replier.build_progress_card.call_args
    status = call_args.kwargs.get("status") or call_args.args[0]
    assert "s" in status  # elapsed time includes "s" suffix


async def test_on_progress_status_includes_tool_and_elapsed(worker, replier):
    """When tool_name present, status shows tool + elapsed."""
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0
    worker._task_start = time.monotonic() - 10
    worker._progress_buffer = []
    await worker._on_progress("", "bash")
    call_args = replier.build_progress_card.call_args
    status = call_args.kwargs.get("status") or call_args.args[0]
    assert "bash" in status
    assert "s" in status  # elapsed present


def test_format_elapsed_seconds_only():
    from nextme.core.worker import _format_elapsed
    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(59) == "59s"


def test_format_elapsed_minutes_and_seconds():
    from nextme.core.worker import _format_elapsed
    assert _format_elapsed(60) == "1m"
    assert _format_elapsed(90) == "1m 30s"
    assert _format_elapsed(125) == "2m 5s"


async def test_send_result_includes_elapsed_in_card(worker, session, replier, acp_registry):
    """build_result_card is called with an elapsed kwarg when task completes."""
    _, mock_runtime = acp_registry
    worker._task_start = time.monotonic() - 3
    task, _ = make_task("hello")
    await worker._execute_task(task)
    call_kwargs = replier.build_result_card.call_args.kwargs
    assert "elapsed" in call_kwargs
    assert call_kwargs["elapsed"]  # non-empty elapsed string


# ---------------------------------------------------------------------------
# _on_progress streaming path tests
# ---------------------------------------------------------------------------

async def test_on_progress_streaming_path_calls_update_card_entity(worker, replier):
    """When card_id is set, _on_progress uses update_card_entity (full card update)."""
    worker._card_id = "card_abc"
    worker._sequence = 0
    await worker._on_progress("hello", "")
    replier.update_card_entity.assert_awaited_once()
    args = replier.update_card_entity.call_args.args
    assert args[0] == "card_abc"   # card_id
    assert args[2] == 1            # sequence
    # Card JSON must contain the text chunk in the body
    card_body = json.dumps(json.loads(args[1]).get("body", {}))
    assert "hello" in card_body
    replier.update_card.assert_not_awaited()


async def test_on_progress_streaming_path_accumulates_text_in_card(worker, replier):
    """Card body contains accumulated text after multiple deltas."""
    worker._card_id = "card_abc"
    worker._sequence = 0
    worker._last_streaming_update = 0.0
    await worker._on_progress("Hello", "")
    worker._last_streaming_update = 0.0  # bypass debounce
    await worker._on_progress(", world", "")
    assert replier.update_card_entity.call_count == 2
    last_args = replier.update_card_entity.call_args_list[-1].args
    card_body = json.dumps(json.loads(last_args[1]).get("body", {}))
    assert "Hello" in card_body
    assert "world" in card_body


async def test_on_progress_streaming_path_shows_tool_in_card(worker, replier):
    """Tool name appears in the card body when tool_name is provided."""
    worker._card_id = "card_abc"
    worker._sequence = 0
    await worker._on_progress("", "Bash(ls)")
    replier.update_card_entity.assert_awaited_once()
    call_args = replier.update_card_entity.call_args
    assert call_args.args[0] == "card_abc"
    card_body = json.dumps(json.loads(call_args.args[1]).get("body", {}))
    assert "Bash(ls)" in card_body
    replier.update_card.assert_not_awaited()


async def test_on_progress_streaming_path_increments_sequence_once_per_flush(worker, replier):
    """sequence counter increments once per API flush (delta + tool in same flush = 1 tick)."""
    worker._card_id = "card_abc"
    worker._sequence = 0
    await worker._on_progress("a", "")    # flush #1 (last_update starts at 0.0)
    assert worker._sequence == 1
    await worker._on_progress("b", "Bash")  # tool_name → always flush regardless of debounce
    assert worker._sequence == 2             # one flush combines delta + tool annotation


async def test_on_progress_streaming_debounces_rapid_calls(worker, replier):
    """Calls within the debounce window are not sent to the API."""
    import time
    worker._card_id = "card_abc"
    worker._last_streaming_update = time.monotonic()  # simulate very recent update
    await worker._on_progress("chunk1", "")
    await worker._on_progress("chunk2", "")
    # Both within debounce window — no API calls.
    replier.update_card_entity.assert_not_awaited()


async def test_on_progress_streaming_flushes_on_tool_name_despite_debounce(worker, replier):
    """Tool-name events always flush even when within the debounce window."""
    import time
    worker._card_id = "card_abc"
    worker._last_streaming_update = time.monotonic()  # simulate very recent update
    await worker._on_progress("chunk1", "")       # within debounce → buffered
    await worker._on_progress("", "Bash(ls)")     # tool event → always flush
    replier.update_card_entity.assert_awaited_once()
    # Card body includes the buffered chunk AND the tool name.
    call_args = replier.update_card_entity.call_args
    card_body = json.dumps(json.loads(call_args.args[1]).get("body", {}))
    assert "chunk1" in card_body
    assert "Bash(ls)" in card_body


async def test_on_progress_streaming_exception_caught(worker, replier):
    """update_card_entity exceptions are caught and not re-raised."""
    worker._card_id = "card_abc"
    worker._sequence = 0
    replier.update_card_entity = AsyncMock(side_effect=RuntimeError("network error"))
    # Should not raise
    await worker._on_progress("hello", "")


async def test_on_progress_no_streaming_when_card_id_none(worker, replier):
    """Without card_id, fallback debounce path is used (update_card_entity not called)."""
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    worker._card_id = None
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0
    await worker._on_progress("hello", "")
    replier.update_card_entity.assert_not_awaited()
    replier.update_card.assert_awaited()


# ---------------------------------------------------------------------------
# Cardkit-first (streaming) path in _execute_task
# ---------------------------------------------------------------------------


async def test_execute_task_uses_reply_card_by_id_for_group_chat_when_create_card_succeeds(
    worker, session, replier, acp_registry
):
    """Group chat + create_card succeeds → uses reply_card_by_id for progress;
    result is appended as footer to streaming card (no new reply sent)."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="card_xyz")
    replier.reply_card_by_id = AsyncMock(return_value="om_streaming_123")
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "group"
    await worker._execute_task(task)
    replier.reply_card_by_id.assert_awaited_once()
    call_kwargs = replier.reply_card_by_id.call_args.kwargs
    assert call_kwargs.get("in_thread") is True
    # Finalized via full card replace — no new reply card sent.
    replier.reply_card.assert_not_awaited()
    replier.send_card.assert_not_awaited()
    replier.update_card_entity.assert_awaited()
    assert worker._card_id == "card_xyz"


async def test_execute_task_uses_reply_card_by_id_for_p2p_chat_when_create_card_succeeds(
    worker, session, replier, acp_registry
):
    """P2P chat + create_card succeeds → uses reply_card_by_id with in_thread=False;
    result finalizes streaming card via update_card_entity (no new reply sent)."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="card_xyz")
    replier.reply_card_by_id = AsyncMock(return_value="om_streaming_p2p")
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "p2p"
    await worker._execute_task(task)
    replier.reply_card_by_id.assert_awaited_once()
    call_kwargs = replier.reply_card_by_id.call_args.kwargs
    assert call_kwargs.get("in_thread") is False
    # Finalized via full card replace — no new reply card sent.
    replier.reply_card.assert_not_awaited()
    replier.update_card_entity.assert_awaited()


async def test_execute_task_uses_send_card_by_id_when_no_message_id_and_create_card_succeeds(
    worker, session, replier, acp_registry
):
    """No message_id + create_card succeeds → uses send_card_by_id for progress;
    result finalizes streaming card via update_card_entity (no new send_card)."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="card_xyz")
    replier.send_card_by_id = AsyncMock(return_value="om_by_id_456")
    task, _ = make_task("hello")
    # message_id defaults to ""
    await worker._execute_task(task)
    replier.send_card_by_id.assert_awaited_once()
    # Finalized via full card replace — no new card sent.
    replier.send_card.assert_not_awaited()
    replier.reply_card_by_id.assert_not_awaited()
    replier.update_card_entity.assert_awaited()
    assert worker._card_id == "card_xyz"


async def test_execute_task_card_id_is_none_after_cardkit_fallback(
    worker, session, replier, acp_registry
):
    """When create_card returns '', _card_id stays None (fallback debounce)."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="")
    task, _ = make_task("hello")
    await worker._execute_task(task)
    assert worker._card_id is None


async def test_execute_task_falls_back_to_regular_card_when_reply_card_by_id_returns_empty(
    worker, session, replier, acp_registry
):
    """When reply_card_by_id returns '' (e.g. Feishu 230099), clears _card_id and uses reply_card.

    This reproduces the scenario where create_card succeeds but the Feishu IM
    reply endpoint rejects the card_id reference format.
    """
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="card_xyz")
    replier.reply_card_by_id = AsyncMock(return_value="")  # simulate 230099
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "group"
    await worker._execute_task(task)
    # Streaming path failed → _card_id cleared → regular card was used
    assert worker._card_id is None
    replier.reply_card.assert_awaited()


async def test_execute_task_card_id_set_when_create_card_succeeds(
    worker, session, replier, acp_registry
):
    """When create_card succeeds, _card_id is set to the returned card_id."""
    _, mock_runtime = acp_registry
    worker._settings = worker._settings.model_copy(update={"streaming_enabled": True})
    replier.create_card = AsyncMock(return_value="card_abc")
    replier.send_card_by_id = AsyncMock(return_value="om_123")
    task, _ = make_task("hello")
    await worker._execute_task(task)
    assert worker._card_id == "card_abc"


# ---------------------------------------------------------------------------
# state_store persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def state_store_mock():
    from unittest.mock import MagicMock
    store = MagicMock()
    store.get_project_actual_id = MagicMock(return_value="")
    store.save_project_actual_id = MagicMock()
    return store


async def test_worker_persists_actual_id_after_execute(
    session, acp_registry, replier, settings, path_lock_registry, state_store_mock
):
    """Worker should call save_project_actual_id after executing a task."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-uuid-xyz"
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        state_store=state_store_mock,
    )
    task, _ = make_task("hello")
    await worker._execute_task(task)
    state_store_mock.save_project_actual_id.assert_called_once_with(
        session.context_id, session.project_name, "sess-uuid-xyz"
    )


async def test_worker_restores_session_from_state_store(
    session, acp_registry, replier, settings, path_lock_registry, state_store_mock
):
    """Worker should call restore_session when state_store has a persisted id."""
    registry, mock_runtime = acp_registry
    # Runtime starts with no actual_id (new process after restart)
    mock_runtime.actual_id = None
    state_store_mock.get_project_actual_id = MagicMock(return_value="persisted-id")
    mock_runtime.restore_session = AsyncMock()
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        state_store=state_store_mock,
    )
    task, _ = make_task("hello")
    await worker._execute_task(task)
    mock_runtime.restore_session.assert_awaited_once_with("persisted-id")


async def test_worker_skips_restore_when_no_persisted_id(
    session, acp_registry, replier, settings, path_lock_registry, state_store_mock
):
    """Worker should not call restore_session when state_store returns empty string."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = None
    state_store_mock.get_project_actual_id = MagicMock(return_value="")
    mock_runtime.restore_session = AsyncMock()
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        state_store=state_store_mock,
    )
    task, _ = make_task("hello")
    await worker._execute_task(task)
    mock_runtime.restore_session.assert_not_awaited()


async def test_worker_skips_persist_when_no_state_store(
    session, acp_registry, replier, settings, path_lock_registry
):
    """Worker without state_store should not raise and should complete normally."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "some-id"
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        # No state_store passed
    )
    task, _ = make_task("hello")
    # Should complete without error
    await worker._execute_task(task)


# ---------------------------------------------------------------------------
# memory injection
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_manager_mock():
    from unittest.mock import AsyncMock, MagicMock
    from nextme.memory.schema import Fact
    mgr = MagicMock()
    mgr.load = AsyncMock()
    mgr.get_top_facts = MagicMock(return_value=[])
    return mgr


async def test_worker_injects_memory_for_new_session(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker injects memory facts into task content when session is new."""
    from nextme.memory.schema import Fact
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = None  # new session
    fact = Fact(text="User prefers Python", source="user_command")
    memory_manager_mock.get_top_facts = MagicMock(return_value=[fact])
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("what language should I use?")
    await worker._execute_task(task)
    # The injected content should include the memory header
    injected_content = mock_runtime.execute.call_args[1]["task"].content
    assert "[用户记忆]" in injected_content
    assert "User prefers Python" in injected_content
    assert "what language should I use?" in injected_content
    # Memory is loaded with user_id (ou_user), not full context_id (oc_chat:ou_user)
    memory_manager_mock.load.assert_awaited_once_with("ou_user")


async def test_worker_skips_memory_injection_for_resumed_session(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker does NOT inject memory when session already has an actual_id (resumed)."""
    from nextme.memory.schema import Fact
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "existing-session-id"  # resumed session
    fact = Fact(text="Should not be injected", source="user_command")
    memory_manager_mock.get_top_facts = MagicMock(return_value=[fact])
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("hello again")
    await worker._execute_task(task)
    # Original content, no memory injection
    executed_content = mock_runtime.execute.call_args[1]["task"].content
    assert "[用户记忆]" not in executed_content
    assert "hello again" == executed_content


async def test_worker_skips_memory_injection_when_no_facts(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker does not inject memory header when there are no facts."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = None
    memory_manager_mock.get_top_facts = MagicMock(return_value=[])
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("plain message")
    await worker._execute_task(task)
    executed_content = mock_runtime.execute.call_args[1]["task"].content
    assert "[用户记忆]" not in executed_content
    assert "plain message" == executed_content


# ---------------------------------------------------------------------------
# memory extraction / writeback
# ---------------------------------------------------------------------------


def test_extract_and_strip_memory_single_fact():
    """Extracts a single <memory> fact from agent output."""
    content = "Here is my answer.\n<memory>User prefers dark mode</memory>\nDone."
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "add"
    assert ops[0].text == "User prefers dark mode"
    assert "<memory>" not in stripped
    assert "Here is my answer." in stripped
    assert "Done." in stripped


def test_extract_and_strip_memory_multiple_facts():
    """Extracts multiple <memory> blocks from agent output."""
    content = (
        "Answer here.\n"
        "<memory>User works in Python</memory>\n"
        "More text.\n"
        "<memory>User is in Shanghai timezone</memory>"
    )
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 2
    assert ops[0].op == "add"
    assert ops[1].op == "add"
    assert ops[0].text == "User works in Python"
    assert ops[1].text == "User is in Shanghai timezone"
    assert "<memory>" not in stripped


def test_extract_and_strip_memory_no_tags():
    """Returns empty list and original content when no <memory> tags present."""
    content = "Just a normal answer with no memory tags."
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops == []
    assert stripped == content


def test_extract_and_strip_memory_strips_blank_lines():
    """Stripped content does not have extra blank lines where tags were."""
    content = "Line one.\n\n<memory>Some fact</memory>\n\nLine two."
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops[0].text == "Some fact"
    # No more than one consecutive blank line in stripped output
    assert "\n\n\n" not in stripped


async def test_worker_saves_memory_facts_after_task(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker calls add_fact() for each <memory> block in agent output."""
    from nextme.memory.schema import Fact
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "existing-session"
    mock_runtime.execute = AsyncMock(
        return_value="Great answer!\n<memory>User likes concise replies</memory>"
    )
    memory_manager_mock.add_fact = MagicMock()
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("be brief")
    await worker._execute_task(task)
    # add_fact should be called once with the extracted fact
    assert memory_manager_mock.add_fact.call_count == 1
    call_args = memory_manager_mock.add_fact.call_args
    assert call_args[0][0] == "ou_user"   # user_id
    saved_fact = call_args[0][1]
    assert isinstance(saved_fact, Fact)
    assert saved_fact.text == "User likes concise replies"
    assert saved_fact.source == "agent_output"


async def test_worker_result_content_excludes_memory_tags(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """The content sent to the result card has <memory> tags stripped out."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "existing-session"
    mock_runtime.execute = AsyncMock(
        return_value="The answer is 42.\n<memory>User asked about meaning of life</memory>"
    )
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("what is the meaning?")
    await worker._execute_task(task)
    # build_result_card should be called with stripped content
    result_card_calls = replier.build_result_card.call_args_list
    assert result_card_calls, "build_result_card was never called"
    result_content = result_card_calls[-1][1].get("content", "")
    assert "<memory>" not in result_content
    assert "The answer is 42." in result_content


async def test_worker_no_memory_writeback_without_memory_manager(
    session, acp_registry, replier, settings, path_lock_registry
):
    """Worker does not crash when memory_manager is None and output has <memory> tags."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "existing-session"
    mock_runtime.execute = AsyncMock(
        return_value="Answer.\n<memory>Some fact</memory>"
    )
    worker = SessionWorker(session, registry, replier, settings, path_lock_registry)
    task, _ = make_task("test")
    # Should complete without error; result content is stripped
    await worker._execute_task(task)
    result_card_calls = replier.build_result_card.call_args_list
    assert result_card_calls
    result_content = result_card_calls[-1][1].get("content", "")
    assert "<memory>" not in result_content


def test_extract_and_strip_memory_oversized_kept_in_display():
    """Large <memory> blocks (> 500 chars) are kept visible to prevent content loss."""
    big_plan = "方案A：" + "x" * 510
    content = f"Intro.\n<memory>{big_plan}</memory>\n方案B：bbb"
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    # Fact is still recorded
    assert len(ops) == 1
    assert ops[0].op == "add"
    assert ops[0].text == big_plan
    # Content is kept visible — the display should NOT lose Plan A
    assert big_plan in stripped
    assert "方案B：bbb" in stripped
    assert "<memory>" not in stripped


def test_extract_and_strip_memory_short_fact_is_stripped():
    """Short <memory> blocks (≤ 500 chars) are stripped from the display as intended."""
    content = "Answer.\n<memory>User prefers dark mode</memory>\nDone."
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "add"
    assert ops[0].text == "User prefers dark mode"
    # Short fact removed from display
    assert "User prefers dark mode" not in stripped
    assert "Answer." in stripped
    assert "Done." in stripped


def test_extract_and_strip_memory_replace_op():
    content = 'Done.\n<memory op="replace" idx="0">updated fact</memory>'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "replace"
    assert ops[0].idx == 0
    assert ops[0].text == "updated fact"
    assert "<memory" not in stripped


def test_extract_and_strip_memory_forget_op():
    content = 'Done.\n<memory op="forget" idx="2"></memory>'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "forget"
    assert ops[0].idx == 2
    assert "<memory" not in stripped


def test_extract_and_strip_memory_mixed_ops():
    content = (
        '<memory>new fact</memory>\n'
        '<memory op="replace" idx="1">replacement</memory>\n'
        '<memory op="forget" idx="3"></memory>'
    )
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 3
    assert ops[0].op == "add"
    assert ops[1].op == "replace" and ops[1].idx == 1
    assert ops[2].op == "forget" and ops[2].idx == 3


def test_extract_and_strip_memory_replace_missing_idx_ignored():
    content = '<memory op="replace">no idx</memory> text'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops == []
    assert "text" in stripped


def test_extract_and_strip_memory_forget_missing_idx_ignored():
    content = '<memory op="forget"></memory> text'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops == []


# ---------------------------------------------------------------------------
# memory ops dispatch (replace / forget) — Task 5
# ---------------------------------------------------------------------------


async def test_worker_dispatches_replace_op_to_memory_manager(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker calls replace_fact() when agent outputs a replace tag."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='Done.\n<memory op="replace" idx="0">updated fact</memory>'
    )
    memory_manager_mock.replace_fact = MagicMock(return_value=True)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("update memory")
    await worker._execute_task(task)
    memory_manager_mock.replace_fact.assert_called_once_with(
        "ou_user", 0, "updated fact"
    )


async def test_worker_dispatches_forget_op_to_memory_manager(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker calls forget_fact() when agent outputs a forget tag."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='Done.\n<memory op="forget" idx="2"></memory>'
    )
    memory_manager_mock.forget_fact = MagicMock(return_value=True)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("forget memory")
    await worker._execute_task(task)
    memory_manager_mock.forget_fact.assert_called_once_with("ou_user", 2)


async def test_worker_logs_warning_when_replace_idx_out_of_range(
    session, acp_registry, replier, settings, path_lock_registry,
    memory_manager_mock, caplog
):
    """Worker logs a warning when replace_fact returns False."""
    import logging
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='<memory op="replace" idx="99">x</memory>'
    )
    memory_manager_mock.replace_fact = MagicMock(return_value=False)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    with caplog.at_level(logging.WARNING, logger="nextme.core.worker"):
        await worker._execute_task(make_task("x")[0])
    assert "replace_fact" in caplog.text


async def test_worker_memory_injection_uses_numbered_format(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """New-session injection uses '0. fact' numbered format from template."""
    from nextme.memory.schema import Fact
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = None   # new session

    captured_task = {}

    async def capture(task, on_progress, on_permission):
        captured_task["content"] = task.content
        return "answer"

    mock_runtime.execute = capture
    memory_manager_mock.get_top_facts = MagicMock(
        return_value=[Fact(text="use uv")]
    )
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    await worker._execute_task(make_task("hello")[0])
    assert "0. use uv" in captured_task.get("content", "")
