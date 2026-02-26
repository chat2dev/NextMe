"""Unit tests for nextme.core.worker.SessionWorker."""

import asyncio
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
        permission_timeout_seconds=1.0,
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
    r.update_card = AsyncMock()
    r.reply_text = AsyncMock(return_value="thread_msg_456")
    r.reply_card = AsyncMock(return_value="thread_card_789")
    r.build_help_card = MagicMock(return_value='{"card": "help"}')
    r.build_permission_card = MagicMock(return_value='{"card": "perm"}')
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
    """Result updates the existing progress card (no new card created)."""
    worker._progress_message_id = "prog_msg_id"
    task, _ = make_task("hello")
    await worker._send_result(task, "Great result")
    replier.update_card.assert_awaited_once()
    # update_card called with the progress card id
    assert replier.update_card.call_args.args[0] == "prog_msg_id"


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


async def test_send_result_no_fallback_when_update_card_fails(worker, replier):
    """Even if update_card raises, reply_card must NOT be called (no duplicate card)."""
    worker._progress_message_id = "prog_msg_id"
    replier.update_card.side_effect = Exception("API error")
    task, _ = make_task("hello")
    task.message_id = "om_src"
    await worker._send_result(task, "result")
    replier.update_card.assert_awaited_once()
    replier.reply_card.assert_not_awaited()


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
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = 0.0  # ensure enough time passed
    await worker._on_progress("hello", "")
    replier.update_card.assert_awaited_once()


async def test_on_progress_debounces_when_not_enough_time_elapsed(worker, replier, settings):
    # Use a very high debounce so it won't fire
    worker._settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=10.0,  # very long debounce
        permission_timeout_seconds=1.0,
    )
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = time.monotonic()  # just now
    await worker._on_progress("hello", "")
    # Should NOT have updated the card because debounce hasn't elapsed
    replier.update_card.assert_not_awaited()


async def test_on_progress_sends_new_card_when_no_message_id(worker, replier):
    worker._progress_message_id = None
    worker._last_progress_update = 0.0
    await worker._on_progress("some delta", "")
    replier.send_card.assert_awaited()


async def test_on_progress_skips_when_delta_empty_and_no_tool(worker, replier):
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
        permission_timeout_seconds=1.0,
    )
    worker._progress_message_id = "msg_123"
    worker._last_progress_update = time.monotonic()  # just now
    # Provide a tool_name: this bypasses debounce
    await worker._on_progress("", "bash")
    replier.update_card.assert_awaited_once()


async def test_on_progress_accumulates_buffer(worker, replier):
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


async def test_on_permission_timeout_returns_default_choice(worker, session, replier):
    """When no one resolves the future, _on_permission should timeout and return index=1."""
    # Use a very short timeout
    worker._settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=0.05,  # 50ms
    )
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-timeout",
        description="This will timeout",
        options=[PermOption(index=1, label="Default"), PermOption(index=2, label="Other")],
    )
    # Don't resolve the future — let it time out
    result = await worker._on_permission(req)
    assert result.option_index == 1
    assert result.request_id == "req-timeout"


async def test_on_permission_timeout_calls_cancel_permission(worker, session, replier):
    """After a timeout, cancel_permission should be called on the session."""
    worker._settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=0.05,
    )
    req = PermissionRequest(
        session_id="oc_chat:ou_user",
        request_id="req-cancel",
        description="This will timeout and cancel",
        options=[PermOption(index=1, label="Default")],
    )
    with patch.object(session, "cancel_permission", wraps=session.cancel_permission) as mock_cancel:
        await worker._on_permission(req)
        # cancel_permission is called on timeout
        mock_cancel.assert_called()


# ---------------------------------------------------------------------------
# _execute_task integration-style tests
# ---------------------------------------------------------------------------

async def test_execute_task_sends_initial_progress_card(worker, session, replier, acp_registry):
    _, mock_runtime = acp_registry
    task, replies = make_task("hello")
    await worker._execute_task(task)
    # Initial progress card is sent at the start
    replier.build_progress_card.assert_called()
    replier.send_card.assert_awaited()


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
    """Group chat: initial progress card sent via reply_card with in_thread=True."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "group"
    await worker._execute_task(task)
    replier.reply_card.assert_awaited()
    call_kwargs = replier.reply_card.call_args.kwargs
    assert call_kwargs.get("in_thread") is True
    replier.send_card.assert_not_awaited()


async def test_execute_task_uses_reply_card_for_p2p_chat(
    worker, session, replier, acp_registry
):
    """P2P chat: initial progress card sent via reply_card with in_thread=False (quote)."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    task.message_id = "om_src_123"
    task.chat_type = "p2p"
    await worker._execute_task(task)
    replier.reply_card.assert_awaited()
    call_kwargs = replier.reply_card.call_args.kwargs
    assert call_kwargs.get("in_thread") is False
    replier.send_card.assert_not_awaited()


async def test_execute_task_uses_send_card_when_no_message_id(
    worker, session, replier, acp_registry
):
    """When task has no message_id, initial progress card uses send_card fallback."""
    _, mock_runtime = acp_registry
    task, _ = make_task("hello")
    # message_id defaults to ""
    await worker._execute_task(task)
    replier.send_card.assert_awaited()
    replier.reply_card.assert_not_awaited()


async def test_on_progress_status_includes_elapsed_time(worker, replier):
    """Progress card status line includes elapsed time when content is present."""
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
