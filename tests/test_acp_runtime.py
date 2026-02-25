"""Tests for nextme.acp.runtime.ACPRuntime."""
import asyncio
import uuid
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.acp.runtime import ACPRuntime, _READY_TIMEOUT_SECONDS, _STOP_GRACEFUL_TIMEOUT_SECONDS
from nextme.config.schema import Settings
from nextme.protocol.types import Task, PermissionChoice, PermOption, PermissionRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(content: str = "hello", canceled: bool = False, timeout_seconds: float = 10.0):
    """Create a Task and a list that captures replies."""
    replies = []

    async def reply_fn(r):
        replies.append(r)

    task = Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id="test-session",
        reply_fn=reply_fn,
        timeout=timedelta(seconds=timeout_seconds),
        canceled=canceled,
    )
    return task, replies


def make_runtime(tmp_path, settings=None, executor="echo"):
    """Create an ACPRuntime with a tmp working directory."""
    if settings is None:
        settings = Settings(
            progress_debounce_seconds=0.0,
            permission_timeout_seconds=1.0,
        )
    return ACPRuntime(
        session_id="test-session",
        cwd=str(tmp_path),
        settings=settings,
        executor=executor,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    return Settings(
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=1.0,
    )


@pytest.fixture
def runtime(settings, tmp_path):
    return make_runtime(tmp_path, settings=settings, executor="echo")


# ---------------------------------------------------------------------------
# Tests: properties (no subprocess needed)
# ---------------------------------------------------------------------------

def test_is_running_false_when_proc_none(runtime):
    """is_running is False when _proc is None."""
    assert runtime._proc is None
    assert runtime.is_running is False


def test_is_running_true_when_proc_returncode_none(runtime):
    """is_running is True when _proc.returncode is None (still alive)."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    assert runtime.is_running is True


def test_is_running_false_when_proc_exited(runtime):
    """is_running is False when _proc.returncode is 0 (exited cleanly)."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    runtime._proc = mock_proc
    assert runtime.is_running is False


def test_is_running_false_when_proc_exited_nonzero(runtime):
    """is_running is False when _proc.returncode is non-zero."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    runtime._proc = mock_proc
    assert runtime.is_running is False


def test_last_access_returns_datetime(runtime):
    """last_access returns a datetime instance."""
    assert isinstance(runtime.last_access, datetime)


def test_actual_id_none_by_default(runtime):
    """actual_id is None before any execute call."""
    assert runtime.actual_id is None


def test_actual_id_set_manually(runtime):
    """actual_id returns whatever _actual_id is set to."""
    runtime._actual_id = "acp-session-123"
    assert runtime.actual_id == "acp-session-123"


# ---------------------------------------------------------------------------
# Tests: reset_session
# ---------------------------------------------------------------------------

async def test_reset_session_clears_actual_id(runtime):
    """reset_session sets _actual_id back to None."""
    runtime._actual_id = "some-id"
    await runtime.reset_session()
    assert runtime._actual_id is None


async def test_reset_session_idempotent(runtime):
    """reset_session can be called when _actual_id is already None."""
    assert runtime._actual_id is None
    await runtime.reset_session()
    assert runtime._actual_id is None


# ---------------------------------------------------------------------------
# Tests: cancel
# ---------------------------------------------------------------------------

async def test_cancel_does_nothing_when_not_running(runtime):
    """cancel() is a no-op when the subprocess is not running."""
    assert not runtime.is_running
    # Should not raise
    await runtime.cancel()


async def test_cancel_sends_cancel_message_when_running(runtime):
    """cancel() sends a CancelMsg when the subprocess is alive."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc

    mock_client = MagicMock()
    mock_client.send = AsyncMock()
    runtime._client = mock_client

    await runtime.cancel()

    mock_client.send.assert_awaited_once()
    sent_msg = mock_client.send.call_args.args[0]
    # CancelMsg has a session_id attribute
    assert hasattr(sent_msg, "session_id")
    assert sent_msg.session_id == "test-session"


async def test_cancel_does_nothing_when_client_none(runtime):
    """cancel() is a no-op when _client is None even if proc seems running."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    runtime._client = None
    # Should not raise
    await runtime.cancel()


# ---------------------------------------------------------------------------
# Tests: stop
# ---------------------------------------------------------------------------

async def test_stop_handles_proc_none(runtime):
    """stop() returns immediately when _proc is None."""
    assert runtime._proc is None
    await runtime.stop()  # should not raise


async def test_stop_resets_state(runtime):
    """stop() sets _proc, _client, _ready back to None/False."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # already exited (so we skip SIGTERM path)
    runtime._proc = mock_proc
    runtime._client = MagicMock()
    runtime._ready = True

    await runtime.stop()

    assert runtime._proc is None
    assert runtime._client is None
    assert runtime._ready is False


async def test_stop_sends_sigterm(runtime):
    """stop() calls proc.terminate() when proc is still running."""
    mock_proc = MagicMock()
    mock_proc.returncode = None  # still running
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    runtime._proc = mock_proc
    runtime._ready = True

    await runtime.stop()

    mock_proc.terminate.assert_called_once()


async def test_stop_kills_on_graceful_timeout(runtime):
    """stop() calls proc.kill() when proc does not exit within the graceful timeout."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()

    kill_called = False

    def mock_kill():
        nonlocal kill_called
        kill_called = True

    mock_proc.kill = mock_kill

    # Simulate wait() never completing so graceful timeout fires
    async def never_returns():
        await asyncio.sleep(9999)

    mock_proc.wait = AsyncMock(side_effect=never_returns)
    runtime._proc = mock_proc
    runtime._ready = True

    # Patch the graceful timeout to be very short
    with patch("nextme.acp.runtime._STOP_GRACEFUL_TIMEOUT_SECONDS", 0.05):
        await runtime.stop()

    assert kill_called or mock_proc.terminate.called


async def test_stop_cancels_background_tasks(runtime):
    """stop() cancels the reader and stderr drain tasks."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # already exited

    # Create real asyncio tasks that block forever so they're not done
    async def block():
        await asyncio.sleep(9999)

    reader_task = asyncio.create_task(block())
    stderr_task = asyncio.create_task(block())

    runtime._proc = mock_proc
    runtime._reader_task = reader_task
    runtime._stderr_drain_task = stderr_task

    await runtime.stop()

    # Both background tasks should have been cancelled
    assert reader_task.cancelled()
    assert stderr_task.cancelled()


# ---------------------------------------------------------------------------
# Tests: ensure_ready (mock subprocess)
# ---------------------------------------------------------------------------

async def test_ensure_ready_idempotent_when_already_ready(runtime):
    """ensure_ready() is a no-op when already ready and running."""
    runtime._ready = True
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await runtime.ensure_ready()

    # Should not have launched a new subprocess
    mock_exec.assert_not_called()


async def test_ensure_ready_waits_for_ready_message(runtime):
    """ensure_ready completes when subprocess sends a 'ready' message."""
    mock_proc = MagicMock()
    mock_proc.stdin = AsyncMock()
    mock_proc.returncode = None

    # Build a queue of bytes for stdout
    messages = [b'{"type": "ready"}\n', b""]
    idx = 0

    async def mock_readline():
        nonlocal idx
        val = messages[idx]
        if idx < len(messages) - 1:
            idx += 1
        return val

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = mock_readline

    async def mock_stderr_readline():
        return b""

    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline = mock_stderr_readline

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await runtime.ensure_ready()

    assert runtime._ready is True


async def test_ensure_ready_raises_on_timeout(tmp_path):
    """ensure_ready raises RuntimeError when subprocess doesn't send 'ready' in time."""
    settings = Settings(progress_debounce_seconds=0.0, permission_timeout_seconds=0.1)
    rt = ACPRuntime(
        session_id="slow-session",
        cwd=str(tmp_path),
        settings=settings,
        executor="echo",
    )

    mock_proc = MagicMock()
    mock_proc.stdin = AsyncMock()
    mock_proc.returncode = None

    # stdout never sends ready
    async def block_forever():
        await asyncio.sleep(9999)

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = AsyncMock(side_effect=block_forever)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline = AsyncMock(side_effect=block_forever)
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch(
            "nextme.acp.runtime._READY_TIMEOUT_SECONDS", 0.05
        ):
            with pytest.raises(RuntimeError, match="timed out waiting for 'ready'"):
                await rt.ensure_ready()


async def test_ensure_ready_ignores_non_ready_messages_before_ready(runtime):
    """ensure_ready skips unexpected messages and waits for 'ready'."""
    mock_proc = MagicMock()
    mock_proc.stdin = AsyncMock()
    mock_proc.returncode = None

    messages = [
        b'{"type": "info", "msg": "starting"}\n',
        b'{"type": "debug", "msg": "loading"}\n',
        b'{"type": "ready"}\n',
        b"",
    ]
    idx = 0

    async def mock_readline():
        nonlocal idx
        val = messages[idx]
        if idx < len(messages) - 1:
            idx += 1
        return val

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = mock_readline
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline = AsyncMock(return_value=b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await runtime.ensure_ready()

    assert runtime._ready is True


# ---------------------------------------------------------------------------
# Tests: execute (mock _client and _msg_queue directly)
# ---------------------------------------------------------------------------

def _setup_runtime_ready(runtime):
    """Put the runtime in the ready/running state with mocked internals."""
    mock_client = MagicMock()
    mock_client.send = AsyncMock()
    runtime._client = mock_client

    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    runtime._ready = True

    queue = asyncio.Queue()
    runtime._msg_queue = queue
    return mock_client, queue


async def test_execute_returns_done_content(runtime):
    """execute returns the content from the 'done' message."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, replies = make_task("hello world")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # Seed the queue with messages
    await queue.put({"type": "session_created", "session_id": "acp-123"})
    await queue.put({"type": "done", "content": "Hello world"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert result == "Hello world"
    assert runtime._actual_id == "acp-123"


async def test_execute_sets_actual_id_from_session_created(runtime):
    """session_created message updates _actual_id."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-xyz-999"})
    await queue.put({"type": "done", "content": "ok"})

    await runtime.execute(task, on_progress, on_permission)

    assert runtime._actual_id == "acp-xyz-999"


async def test_execute_accumulates_content_deltas(runtime):
    """content_delta messages accumulate and are returned by done."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("tell me")
    deltas_received = []

    async def on_progress(delta, tool):
        if delta:
            deltas_received.append(delta)

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "content_delta", "delta": "Hello "})
    await queue.put({"type": "content_delta", "delta": "world"})
    # done without explicit content → use accumulated
    await queue.put({"type": "done"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert result == "Hello world"


async def test_execute_done_content_overrides_accumulated(runtime):
    """When 'done' has explicit content field, it takes precedence over accumulated."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "content_delta", "delta": "partial "})
    await queue.put({"type": "done", "content": "final answer"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert result == "final answer"


async def test_execute_tool_use_calls_on_progress(runtime):
    """tool_use messages invoke on_progress with the tool name."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("use a tool")
    tool_events = []

    async def on_progress(delta, tool):
        if tool:
            tool_events.append(tool)

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "tool_use", "name": "bash_tool"})
    await queue.put({"type": "done", "content": "done"})

    await runtime.execute(task, on_progress, on_permission)

    assert "bash_tool" in tool_events


async def test_execute_permission_request_calls_on_permission(runtime):
    """permission_request messages invoke on_permission and send a response."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("risky op")
    permission_requests = []

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        permission_requests.append(req)
        return PermissionChoice(request_id=req.request_id, option_index=2)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({
        "type": "permission_request",
        "request_id": "req-001",
        "description": "Run dangerous command?",
        "options": [
            {"index": 1, "label": "Deny"},
            {"index": 2, "label": "Allow"},
        ],
    })
    await queue.put({"type": "done", "content": "completed"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert len(permission_requests) == 1
    assert permission_requests[0].request_id == "req-001"
    assert permission_requests[0].description == "Run dangerous command?"
    # PermissionResponseMsg was sent
    mock_client.send.assert_awaited()
    result == "completed"


async def test_execute_permission_timeout_defaults_to_index_1(runtime):
    """Permission request timeout defaults choice to option_index=1."""
    mock_client, queue = _setup_runtime_ready(runtime)

    # Use a short timeout
    runtime._settings = Settings(
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=0.05,
    )

    task, _ = make_task("risky op")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        # Takes longer than the timeout
        await asyncio.sleep(9999)
        return PermissionChoice(request_id=req.request_id, option_index=2)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({
        "type": "permission_request",
        "request_id": "req-timeout",
        "description": "Allow?",
        "options": [{"index": 1, "label": "Allow"}],
    })
    await queue.put({"type": "done", "content": "ok"})

    result = await runtime.execute(task, on_progress, on_permission)

    # Should have sent a permission response with index 1 (the default)
    sent_calls = [call.args[0] for call in mock_client.send.call_args_list]
    from nextme.acp.protocol import PermissionResponseMsg
    perm_responses = [m for m in sent_calls if isinstance(m, PermissionResponseMsg)]
    assert len(perm_responses) == 1
    assert perm_responses[0].choice == 1


async def test_execute_error_message_raises_runtime_error(runtime):
    """An 'error' message from ACP raises RuntimeError."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("bad op")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "error", "message": "something went wrong"})

    with pytest.raises(RuntimeError, match="something went wrong"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_exception_in_queue_raises_runtime_error(runtime):
    """An Exception object in the queue raises RuntimeError."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put(ValueError("subprocess died"))

    with pytest.raises(RuntimeError, match="subprocess error"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_canceled_task_calls_cancel_and_returns(runtime):
    """A pre-canceled task triggers cancel() and returns accumulated content."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("work", canceled=True)

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # Queue has some content that won't be consumed because task is canceled
    await queue.put({"type": "session_created", "session_id": "acp-1"})

    result = await runtime.execute(task, on_progress, on_permission)

    # cancel() was called → client.send was called with CancelMsg
    cancel_calls = [call.args[0] for call in mock_client.send.call_args_list]
    from nextme.acp.protocol import CancelMsg
    cancel_msgs = [m for m in cancel_calls if isinstance(m, CancelMsg)]
    assert len(cancel_msgs) == 1
    # Returns empty (no deltas accumulated)
    assert result == ""


async def test_execute_timeout_raises_runtime_error(runtime):
    """Task timeout raises RuntimeError when queue blocks."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("slow query", timeout_seconds=0.05)

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # Queue only has session_created; done never arrives
    await queue.put({"type": "session_created", "session_id": "acp-1"})

    with pytest.raises(RuntimeError, match="timed out waiting for ACP response"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_sends_new_session_when_no_actual_id(runtime):
    """execute sends NewSessionMsg when _actual_id is None."""
    from nextme.acp.protocol import NewSessionMsg

    mock_client, queue = _setup_runtime_ready(runtime)
    runtime._actual_id = None

    task, _ = make_task("hello")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-new"})
    await queue.put({"type": "done", "content": "ok"})

    await runtime.execute(task, on_progress, on_permission)

    sent_msgs = [call.args[0] for call in mock_client.send.call_args_list]
    new_session_msgs = [m for m in sent_msgs if isinstance(m, NewSessionMsg)]
    assert len(new_session_msgs) == 1


async def test_execute_sends_load_session_when_actual_id_set(runtime):
    """execute sends LoadSessionMsg when _actual_id is already known."""
    from nextme.acp.protocol import LoadSessionMsg

    mock_client, queue = _setup_runtime_ready(runtime)
    runtime._actual_id = "existing-acp-id"

    task, _ = make_task("follow-up")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "done", "content": "response"})

    await runtime.execute(task, on_progress, on_permission)

    sent_msgs = [call.args[0] for call in mock_client.send.call_args_list]
    load_session_msgs = [m for m in sent_msgs if isinstance(m, LoadSessionMsg)]
    assert len(load_session_msgs) == 1
    assert load_session_msgs[0].session_id == "existing-acp-id"


async def test_execute_unknown_message_type_is_ignored(runtime):
    """Unknown message types are logged but do not crash execute."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "some_unknown_event", "data": "blah"})
    await queue.put({"type": "done", "content": "finished"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert result == "finished"


async def test_execute_content_delta_using_content_field(runtime):
    """content_delta message with 'content' fallback field is accumulated."""
    mock_client, queue = _setup_runtime_ready(runtime)

    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    # Using 'content' key instead of 'delta'
    await queue.put({"type": "content_delta", "content": "alt delta"})
    await queue.put({"type": "done"})

    result = await runtime.execute(task, on_progress, on_permission)

    assert result == "alt delta"


async def test_execute_updates_last_access(runtime):
    """execute updates the _last_access timestamp."""
    mock_client, queue = _setup_runtime_ready(runtime)

    before = runtime._last_access
    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"type": "session_created", "session_id": "acp-1"})
    await queue.put({"type": "done", "content": "ok"})

    await runtime.execute(task, on_progress, on_permission)

    # last_access should be >= before
    assert runtime._last_access >= before
