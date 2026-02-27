"""Tests for nextme.acp.runtime.ACPRuntime (JSON-RPC 2.0 protocol)."""
import asyncio
import uuid
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.acp.runtime import (
    ACPRuntime,
    _INIT_TIMEOUT_SECONDS,
    _STOP_GRACEFUL_TIMEOUT_SECONDS,
    _STRIP_ENV_EXACT,
    _STRIP_ENV_PREFIX,
)
from nextme.config.schema import Settings
from nextme.protocol.types import Task, PermissionChoice, PermOption, PermissionRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(content: str = "hello", canceled: bool = False, timeout_seconds: float = 10.0):
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
# Properties (no subprocess)
# ---------------------------------------------------------------------------


def test_is_running_false_when_proc_none(runtime):
    assert runtime._proc is None
    assert runtime.is_running is False


def test_is_running_true_when_proc_returncode_none(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    assert runtime.is_running is True


def test_is_running_false_when_proc_exited(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    runtime._proc = mock_proc
    assert runtime.is_running is False


def test_is_running_false_when_proc_exited_nonzero(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    runtime._proc = mock_proc
    assert runtime.is_running is False


def test_last_access_returns_datetime(runtime):
    assert isinstance(runtime.last_access, datetime)


def test_actual_id_none_by_default(runtime):
    assert runtime.actual_id is None


def test_actual_id_set_manually(runtime):
    runtime._actual_id = "acp-session-123"
    assert runtime.actual_id == "acp-session-123"


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------


async def test_reset_session_clears_actual_id(runtime):
    runtime._actual_id = "some-id"
    await runtime.reset_session()
    assert runtime._actual_id is None


async def test_reset_session_idempotent(runtime):
    assert runtime._actual_id is None
    await runtime.reset_session()
    assert runtime._actual_id is None


async def test_restore_session_sets_actual_id(runtime):
    await runtime.restore_session("restored-id")
    assert runtime._actual_id == "restored-id"


async def test_restore_session_clears_on_empty_string(runtime):
    runtime._actual_id = "existing"
    await runtime.restore_session("")
    assert runtime._actual_id is None


async def test_restore_session_actual_id_property(runtime):
    await runtime.restore_session("prop-test-id")
    assert runtime.actual_id == "prop-test-id"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_does_nothing_when_not_running(runtime):
    assert not runtime.is_running
    await runtime.cancel()  # should not raise


async def test_cancel_sends_cancel_request_when_running(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    runtime._actual_id = "sess-abc"

    mock_client = MagicMock()
    mock_client.send_request = AsyncMock(return_value=99)
    runtime._client = mock_client

    await runtime.cancel()

    mock_client.send_request.assert_awaited_once()
    method, params = mock_client.send_request.call_args.args
    assert method == "session/cancel"
    assert params["sessionId"] == "sess-abc"


async def test_cancel_does_nothing_when_client_none(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    runtime._client = None
    await runtime.cancel()  # no raise


async def test_cancel_does_nothing_when_no_actual_id(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    mock_client = MagicMock()
    mock_client.send_request = AsyncMock()
    runtime._client = mock_client
    runtime._actual_id = None

    await runtime.cancel()

    mock_client.send_request.assert_not_awaited()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


async def test_stop_handles_proc_none(runtime):
    assert runtime._proc is None
    await runtime.stop()  # should not raise


async def test_stop_resets_state(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    runtime._proc = mock_proc
    runtime._client = MagicMock()
    runtime._ready = True

    await runtime.stop()

    assert runtime._proc is None
    assert runtime._client is None
    assert runtime._ready is False


async def test_stop_sends_sigterm(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    runtime._proc = mock_proc
    runtime._ready = True

    await runtime.stop()

    mock_proc.terminate.assert_called_once()


async def test_stop_kills_on_graceful_timeout(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()

    kill_called = False

    def mock_kill():
        nonlocal kill_called
        kill_called = True

    mock_proc.kill = mock_kill

    async def never_returns():
        await asyncio.sleep(9999)

    mock_proc.wait = AsyncMock(side_effect=never_returns)
    runtime._proc = mock_proc
    runtime._ready = True

    with patch("nextme.acp.runtime._STOP_GRACEFUL_TIMEOUT_SECONDS", 0.05):
        await runtime.stop()

    assert kill_called or mock_proc.terminate.called


async def test_stop_cancels_background_tasks(runtime):
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    async def block():
        await asyncio.sleep(9999)

    reader_task = asyncio.create_task(block())
    stderr_task = asyncio.create_task(block())

    runtime._proc = mock_proc
    runtime._reader_task = reader_task
    runtime._stderr_drain_task = stderr_task

    await runtime.stop()

    assert reader_task.cancelled()
    assert stderr_task.cancelled()


# ---------------------------------------------------------------------------
# ensure_ready (mock subprocess)
# ---------------------------------------------------------------------------


async def test_ensure_ready_idempotent_when_already_ready(runtime):
    runtime._ready = True
    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await runtime.ensure_ready()

    mock_exec.assert_not_called()


async def test_ensure_ready_sends_initialize_and_waits(runtime):
    """ensure_ready completes when subprocess responds to initialize."""
    mock_proc = MagicMock()
    mock_proc.returncode = None

    # stdin
    stdin_mock = MagicMock()
    stdin_mock.write = MagicMock()
    stdin_mock.drain = AsyncMock()
    mock_proc.stdin = stdin_mock

    # stdout: initialize response (id=1)
    init_response = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}\n'
    messages = [init_response, b""]
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


async def test_ensure_ready_raises_on_timeout(tmp_path):
    """ensure_ready raises RuntimeError when initialize response never arrives."""
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

    async def block_forever():
        await asyncio.sleep(9999)

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = AsyncMock(side_effect=block_forever)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline = AsyncMock(side_effect=block_forever)
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("nextme.acp.runtime._INIT_TIMEOUT_SECONDS", 0.05):
            with pytest.raises(RuntimeError, match="initialize timed out"):
                await rt.ensure_ready()


async def test_ensure_ready_skips_notifications_before_initialize_response(runtime):
    """Notifications arriving before the initialize response are stashed and re-queued."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.drain = AsyncMock()

    # A diagnostic non-JSON line, then notification, then initialize response
    messages = [
        b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}\n',
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
# execute (mock _client and _msg_queue directly)
# ---------------------------------------------------------------------------


def _setup_runtime_ready(runtime, session_id_for_new="acp-new-123"):
    """Put the runtime in ready/running state with a JSON-RPC mock client.

    The mock send_request() auto-increments IDs. The queue is seeded
    with a session/new response so execute() can complete session setup.
    """
    call_count = [0]

    async def mock_send_request(method, params):
        call_count[0] += 1
        return call_count[0]

    mock_client = MagicMock()
    mock_client.send_request = AsyncMock(side_effect=mock_send_request)
    mock_client.send_response = AsyncMock()
    mock_client.send_error_response = AsyncMock()
    runtime._client = mock_client

    mock_proc = MagicMock()
    mock_proc.returncode = None
    runtime._proc = mock_proc
    runtime._ready = True

    queue = asyncio.Queue()
    runtime._msg_queue = queue

    return mock_client, queue, call_count


async def test_execute_returns_accumulated_content(runtime):
    """execute returns accumulated content when prompt response arrives."""
    mock_client, queue, _ = _setup_runtime_ready(runtime)

    task, _ = make_task("say hi")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # session/new response (id=1) and session/prompt response (id=2)
    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "acp-abc"}})
    await queue.put({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "acp-abc",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hi there!"}},
        },
    })
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    result = await runtime.execute(task, on_progress, on_permission)
    assert result == "Hi there!"
    assert runtime._actual_id == "acp-abc"


async def test_execute_accumulates_multiple_chunks(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("tell me")
    chunks = []

    async def on_progress(delta, tool):
        if delta:
            chunks.append(delta)

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello "}}
    }})
    await queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "world"}}
    }})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    result = await runtime.execute(task, on_progress, on_permission)
    assert result == "Hello world"


async def test_execute_tool_call_calls_on_progress(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("use tool")
    tool_events = []

    async def on_progress(delta, tool):
        if tool:
            tool_events.append(tool)

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
        "update": {"sessionUpdate": "tool_call", "title": "Read file"}
    }})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    await runtime.execute(task, on_progress, on_permission)
    assert "Read file" in tool_events


async def test_execute_handles_permission_request(runtime):
    """session/request_permission triggers immediate allow response + async on_permission notification."""
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("risky")
    perm_calls = []

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        perm_calls.append(req)
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    # Inbound permission request from cc-acp
    await queue.put({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {"title": "Run bash command"},
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
            ],
        },
    })
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    await runtime.execute(task, on_progress, on_permission)
    # Let the background notification task run.
    await asyncio.sleep(0.05)

    # on_permission is called asynchronously via background task.
    assert len(perm_calls) == 1
    assert perm_calls[0].description == "Run bash command"
    # Response sent immediately with the first "allow" option (before user interaction).
    mock_client.send_response.assert_awaited_once_with(
        99, {"outcome": {"selected": {"optionId": "allow_once"}}}
    )


async def test_execute_permission_response_sent_immediately_before_user_input(runtime):
    """Response is sent immediately with the default allow option — user input does not block it."""
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("risky")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        # Simulates a slow user — runs in a background task and does not block execute().
        await asyncio.sleep(9999)
        return PermissionChoice(request_id="", option_index=2)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({
        "jsonrpc": "2.0",
        "id": 88,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {},
            "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"}],
        },
    })
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    # execute() must complete without waiting for the slow on_permission.
    await runtime.execute(task, on_progress, on_permission)

    # Response sent immediately with the first option (no timeout needed).
    mock_client.send_response.assert_awaited_once()
    args = mock_client.send_response.call_args.args
    assert args[1]["outcome"]["selected"]["optionId"] == "allow_once"


async def test_execute_error_response_raises(runtime):
    """Error JSON-RPC response on session/prompt raises RuntimeError."""
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("bad")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "Claude process failed"}})

    with pytest.raises(RuntimeError, match="Claude process failed"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_exception_in_queue_raises(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put(ValueError("subprocess died"))

    with pytest.raises(RuntimeError, match="reader error"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_canceled_task_returns_immediately(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("work", canceled=True)

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # provide session/new response so setup can complete
    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})

    result = await runtime.execute(task, on_progress, on_permission)
    assert result == ""
    # cancel() sends session/cancel request
    methods_called = [call.args[0] for call in mock_client.send_request.call_args_list]
    assert "session/cancel" in methods_called


async def test_execute_timeout_raises(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("slow", timeout_seconds=0.05)

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # Only session/new response; prompt response never arrives
    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})

    with pytest.raises(RuntimeError, match="timed out"):
        await runtime.execute(task, on_progress, on_permission)


async def test_execute_uses_load_session_when_actual_id_known(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    runtime._actual_id = "existing-id"
    task, _ = make_task("follow-up")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    # load_session response (id=1), prompt response (id=2)
    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {}})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    await runtime.execute(task, on_progress, on_permission)

    methods = [call.args[0] for call in mock_client.send_request.call_args_list]
    assert methods[0] == "session/load"
    params = mock_client.send_request.call_args_list[0].args[1]
    assert params["sessionId"] == "existing-id"


async def test_execute_uses_new_session_when_no_actual_id(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    runtime._actual_id = None
    task, _ = make_task("first")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "brand-new"}})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    await runtime.execute(task, on_progress, on_permission)

    methods = [call.args[0] for call in mock_client.send_request.call_args_list]
    assert methods[0] == "session/new"
    assert runtime._actual_id == "brand-new"


async def test_execute_updates_last_access(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    before = runtime._last_access
    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    await runtime.execute(task, on_progress, on_permission)
    assert runtime._last_access >= before


async def test_execute_ignores_unknown_update_types(runtime):
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    await queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
        "update": {"sessionUpdate": "some_unknown_event"}
    }})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    result = await runtime.execute(task, on_progress, on_permission)
    assert result == ""


async def test_execute_ignores_stale_response_ids(runtime):
    """Response with an id that doesn't match the prompt request is silently ignored."""
    mock_client, queue, _ = _setup_runtime_ready(runtime)
    task, _ = make_task("query")

    async def on_progress(delta, tool):
        pass

    async def on_permission(req):
        return PermissionChoice(request_id="", option_index=1)

    await queue.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})
    # Stale response with wrong id (e.g. from a prior session/new that arrived late)
    await queue.put({"jsonrpc": "2.0", "id": 999, "result": {"irrelevant": True}})
    await queue.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    result = await runtime.execute(task, on_progress, on_permission)
    assert result == ""


# ---------------------------------------------------------------------------
# Environment variable filtering
# ---------------------------------------------------------------------------


def _build_child_env(parent_env: dict) -> dict:
    """Replicate the env-building logic from ACPRuntime.ensure_ready."""
    child_env = {
        k: v
        for k, v in parent_env.items()
        if k not in _STRIP_ENV_EXACT and not k.startswith(_STRIP_ENV_PREFIX)
    }
    child_env["CI"] = "true"
    child_env.setdefault("TERM", "xterm")
    return child_env


def test_strip_claudecode_exact():
    env = _build_child_env({"CLAUDECODE": "1", "PATH": "/usr/bin"})
    assert "CLAUDECODE" not in env
    assert env["PATH"] == "/usr/bin"


def test_strip_claude_code_entrypoint():
    env = _build_child_env({"CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/home/user"})
    assert "CLAUDE_CODE_ENTRYPOINT" not in env


def test_strip_claude_code_experimental_agent_teams():
    env = _build_child_env({"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"})
    assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in env


def test_strip_all_claude_code_prefix_vars():
    """Any CLAUDE_CODE_* var is stripped, even ones not explicitly listed."""
    parent = {
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_API_USAGE_TELEMETRY": "on",
        "CLAUDE_CODE_VERSION": "1.2.3",
        "CLAUDE_CODE_UNKNOWN_FUTURE_VAR": "x",
        "PATH": "/usr/bin",
    }
    env = _build_child_env(parent)
    for key in parent:
        if key.startswith("CLAUDE_CODE_"):
            assert key not in env, f"{key} should be stripped"
    assert env["PATH"] == "/usr/bin"


def test_preserve_anthropic_auth_token():
    """ANTHROPIC_AUTH_TOKEN is kept for proxy auth (not stripped)."""
    env = _build_child_env({"ANTHROPIC_AUTH_TOKEN": "cr_abc123"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "cr_abc123"


def test_anthropic_auth_token_and_base_url_both_preserved():
    """Both ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL pass through unchanged."""
    env = _build_child_env({
        "ANTHROPIC_AUTH_TOKEN": "cr_abc123",
        "ANTHROPIC_BASE_URL": "https://proxy.example.com/api",
        "PATH": "/bin",
    })
    assert env["ANTHROPIC_AUTH_TOKEN"] == "cr_abc123"
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example.com/api"


def test_anthropic_api_key_preserved_alongside_auth_token():
    """When both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are present, both survive."""
    env = _build_child_env({
        "ANTHROPIC_API_KEY": "sk-ant-real",
        "ANTHROPIC_AUTH_TOKEN": "cr_override",
    })
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-real"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "cr_override"


def test_ci_always_set():
    env = _build_child_env({"PATH": "/bin"})
    assert env["CI"] == "true"


def test_ci_overrides_existing():
    """CI=true is forced even if parent had a different value."""
    env = _build_child_env({"CI": "false"})
    assert env["CI"] == "true"


def test_term_defaulted_to_xterm():
    env = _build_child_env({"PATH": "/bin"})
    assert env["TERM"] == "xterm"


def test_term_not_overridden_when_present():
    env = _build_child_env({"TERM": "xterm-256color"})
    assert env["TERM"] == "xterm-256color"


def test_regular_vars_preserved():
    parent = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/alice",
        "USER": "alice",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "VIRTUAL_ENV": "/home/alice/.venv",
    }
    env = _build_child_env(parent)
    assert env["PATH"] == parent["PATH"]
    assert env["HOME"] == parent["HOME"]
    assert env["USER"] == parent["USER"]
    assert env["ANTHROPIC_API_KEY"] == parent["ANTHROPIC_API_KEY"]
    assert env["VIRTUAL_ENV"] == parent["VIRTUAL_ENV"]
