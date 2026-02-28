"""Tests for nextme.acp.direct_runtime.DirectClaudeRuntime."""
import asyncio
import json
import uuid
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.acp.direct_runtime import (
    DirectClaudeRuntime,
    _STOP_GRACEFUL_TIMEOUT_SECONDS,
    _format_tool_call,
)
from nextme.config.schema import Settings
from nextme.protocol.types import PermissionChoice, PermissionRequest, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**kw):
    return Settings(progress_debounce_seconds=0.0, **kw)


def make_task(content="hello", canceled=False, timeout_seconds=10.0):
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id="test-session",
        reply_fn=lambda r: None,
        timeout=timedelta(seconds=timeout_seconds),
        canceled=canceled,
    )


def make_runtime(tmp_path, executor="claude"):
    return DirectClaudeRuntime(
        session_id="test-session",
        cwd=str(tmp_path),
        settings=make_settings(),
        executor=executor,
    )


def make_ndjson(*events) -> list[bytes]:
    """Turn event dicts into a list of byte-lines for mock stdout."""
    lines = [json.dumps(e).encode() + b"\n" for e in events]
    return lines


def mock_proc_with_output(lines: list[bytes]):
    """Build a mock asyncio process that streams given byte-lines from stdout."""
    proc = MagicMock()
    proc.returncode = None

    # stdin
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    proc.stdin = stdin

    # stdout as async iterator
    async def _stdout_iter():
        for line in lines:
            yield line

    proc.stdout = _stdout_iter()

    # stderr as async iterator (empty)
    async def _stderr_iter():
        return
        yield  # make it an async generator

    proc.stderr = _stderr_iter()

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


async def noop_progress(delta, tool):
    pass


async def noop_permission(req):
    return PermissionChoice(request_id="", option_index=1)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_is_running_false_when_no_proc(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.is_running is False


def test_is_running_true_when_proc_alive(tmp_path):
    rt = make_runtime(tmp_path)
    mock = MagicMock()
    mock.returncode = None
    rt._current_proc = mock
    assert rt.is_running is True


def test_is_running_false_when_proc_exited(tmp_path):
    rt = make_runtime(tmp_path)
    mock = MagicMock()
    mock.returncode = 0
    rt._current_proc = mock
    assert rt.is_running is False


def test_last_access_returns_datetime(tmp_path):
    rt = make_runtime(tmp_path)
    assert isinstance(rt.last_access, datetime)


def test_actual_id_none_by_default(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.actual_id is None


# ---------------------------------------------------------------------------
# ensure_ready / stop / reset_session
# ---------------------------------------------------------------------------


async def test_ensure_ready_is_noop(tmp_path):
    rt = make_runtime(tmp_path)
    await rt.ensure_ready()  # should not raise


async def test_stop_no_proc_is_noop(tmp_path):
    rt = make_runtime(tmp_path)
    await rt.stop()  # should not raise


async def test_stop_terminates_running_proc(tmp_path):
    rt = make_runtime(tmp_path)
    mock = MagicMock()
    mock.returncode = None
    mock.terminate = MagicMock()
    mock.wait = AsyncMock(return_value=0)
    rt._current_proc = mock
    await rt.stop()
    mock.terminate.assert_called_once()


async def test_stop_kills_on_timeout(tmp_path):
    rt = make_runtime(tmp_path)
    mock = MagicMock()
    mock.returncode = None
    mock.terminate = MagicMock()

    kill_called = False

    def _kill():
        nonlocal kill_called
        kill_called = True

    mock.kill = _kill

    async def never():
        await asyncio.sleep(9999)

    mock.wait = AsyncMock(side_effect=never)
    rt._current_proc = mock

    with patch("nextme.acp.direct_runtime._STOP_GRACEFUL_TIMEOUT_SECONDS", 0.05):
        await rt.stop()

    assert kill_called or mock.terminate.called


async def test_reset_session_clears_actual_id(tmp_path):
    rt = make_runtime(tmp_path)
    rt._actual_id = "existing"
    await rt.reset_session()
    assert rt._actual_id is None


async def test_restore_session_sets_actual_id(tmp_path):
    rt = make_runtime(tmp_path)
    await rt.restore_session("my-session-id")
    assert rt._actual_id == "my-session-id"


async def test_restore_session_clears_on_empty_string(tmp_path):
    rt = make_runtime(tmp_path)
    rt._actual_id = "existing"
    await rt.restore_session("")
    assert rt._actual_id is None


async def test_restore_session_actual_id_property(tmp_path):
    rt = make_runtime(tmp_path)
    await rt.restore_session("sess-abc")
    assert rt.actual_id == "sess-abc"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_sets_flag_when_no_proc(tmp_path):
    rt = make_runtime(tmp_path)
    await rt.cancel()
    assert rt._cancel_flag is True


async def test_cancel_terminates_running_proc(tmp_path):
    rt = make_runtime(tmp_path)
    mock = MagicMock()
    mock.returncode = None
    mock.terminate = MagicMock()
    rt._current_proc = mock
    await rt.cancel()
    mock.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# execute (mocked subprocess)
# ---------------------------------------------------------------------------


async def test_execute_returns_result_from_result_event(tmp_path):
    rt = make_runtime(tmp_path)
    lines = make_ndjson(
        {"type": "system", "session_id": "sess-1", "model": "claude-3"},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "Hello world", "session_id": "sess-1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert result == "Hello world"
    assert rt.actual_id == "sess-1"


async def test_execute_accumulates_assistant_chunks(tmp_path):
    rt = make_runtime(tmp_path)
    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello "}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "world"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "Hello world", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert result == "Hello world"


async def test_execute_calls_on_progress_with_stream_event_text_delta(tmp_path):
    """stream_event/text_delta tokens are forwarded to on_progress immediately."""
    rt = make_runtime(tmp_path)
    progress_calls = []

    async def record_progress(delta, tool):
        progress_calls.append((delta, tool))

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        }},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello world"}]}},
        {"type": "result", "is_error": False, "result": "Hello world", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("hi"), record_progress, noop_permission)

    text_deltas = [d for d, t in progress_calls if d]
    assert "Hello" in text_deltas
    assert " world" in text_deltas


async def test_execute_assistant_event_does_not_call_on_progress(tmp_path):
    """assistant/text event adds to accumulated but does NOT call on_progress directly."""
    rt = make_runtime(tmp_path)
    progress_calls = []

    async def record_progress(delta, tool):
        progress_calls.append((delta, tool))

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "full text"}]}},
        {"type": "result", "is_error": False, "result": "full text", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), record_progress, noop_permission)

    assert result == "full text"
    # No text progress from assistant event — only stream_event/text_delta fires it
    assert not any(d for d, t in progress_calls)


async def test_execute_calls_on_progress_with_tool_name(tmp_path):
    rt = make_runtime(tmp_path)
    tool_events = []

    async def record_progress(delta, tool):
        if tool:
            tool_events.append(tool)

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("run"), record_progress, noop_permission)

    assert "Bash" in tool_events


async def test_execute_raises_on_error_result(tmp_path):
    rt = make_runtime(tmp_path)
    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "Something went wrong", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(RuntimeError, match="Something went wrong"):
            await rt.execute(make_task("fail"), noop_progress, noop_permission)


async def test_execute_updates_last_access(tmp_path):
    rt = make_runtime(tmp_path)
    before = rt.last_access
    lines = make_ndjson(
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert rt.last_access >= before


async def test_execute_resumes_session_when_actual_id_set(tmp_path):
    rt = make_runtime(tmp_path)
    rt._actual_id = "existing-session"

    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        lines = make_ndjson(
            {"type": "result", "is_error": False, "result": "ok", "session_id": "existing-session"},
        )
        return mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert "--resume" in captured_args
    assert "existing-session" in captured_args


async def test_execute_no_resume_for_new_session(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt._actual_id is None

    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        lines = make_ndjson(
            {"type": "result", "is_error": False, "result": "ok", "session_id": "new-s"},
        )
        return mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert "--resume" not in captured_args


async def test_execute_skips_non_json_lines(tmp_path):
    rt = make_runtime(tmp_path)
    lines = [
        b"[claude] some diagnostic\n",
        json.dumps({"type": "result", "is_error": False,
                    "result": "ok", "session_id": "s1"}).encode() + b"\n",
    ]
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert result == "ok"


async def test_execute_returns_accumulated_when_no_result_event(tmp_path):
    """If stdout ends without a result event, return accumulated assistant text."""
    rt = make_runtime(tmp_path)
    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
        # No result event — stdout just ends
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert result == "partial"


async def test_execute_session_id_updated_from_result(tmp_path):
    rt = make_runtime(tmp_path)
    lines = make_ndjson(
        {"type": "result", "is_error": False, "result": "ok", "session_id": "new-id"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert rt.actual_id == "new-id"


async def test_execute_stdin_write_failure_raises(tmp_path):
    rt = make_runtime(tmp_path)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("pipe broken"))
    proc.stdin.close = MagicMock()

    async def _empty():
        return
        yield

    proc.stdout = _empty()
    proc.stderr = _empty()
    proc.terminate = MagicMock()
    proc.wait = AsyncMock(return_value=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(RuntimeError, match="stdin write failed"):
            await rt.execute(make_task("hi"), noop_progress, noop_permission)


async def test_execute_dangerously_skip_permissions_flag(tmp_path):
    """Verify --dangerously-skip-permissions is always passed."""
    rt = make_runtime(tmp_path)
    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        lines = make_ndjson(
            {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
        )
        return mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert "--dangerously-skip-permissions" in captured_args
    assert "--output-format" in captured_args
    assert "stream-json" in captured_args
    assert "--verbose" in captured_args
    assert "--include-partial-messages" in captured_args


# ---------------------------------------------------------------------------
# _format_tool_call unit tests
# ---------------------------------------------------------------------------


def test_format_tool_call_fetch_extracts_url():
    assert _format_tool_call("Fetch", '{"url": "https://example.com/path"}') == \
        "Fetch(https://example.com/path)"


def test_format_tool_call_bash_extracts_command():
    assert _format_tool_call("Bash", '{"command": "ls -la /tmp"}') == "Bash(ls -la /tmp)"


def test_format_tool_call_read_extracts_file_path():
    assert _format_tool_call("Read", '{"file_path": "/src/main.py"}') == "Read(/src/main.py)"


def test_format_tool_call_grep_extracts_pattern():
    assert _format_tool_call("Grep", '{"pattern": "def foo"}') == "Grep(def foo)"


def test_format_tool_call_empty_json_returns_name():
    assert _format_tool_call("Bash", "") == "Bash"


def test_format_tool_call_invalid_json_returns_name():
    assert _format_tool_call("Bash", "{not json}") == "Bash"


def test_format_tool_call_truncates_long_args():
    long_url = "https://example.com/" + "x" * 100
    result = _format_tool_call("Fetch", f'{{"url": "{long_url}"}}')
    assert len(result) < len(long_url) + 10
    assert "Fetch(" in result
    assert "…" in result


def test_format_tool_call_no_matching_key_uses_first_value():
    result = _format_tool_call("Custom", '{"some_key": "some_value"}')
    assert "Custom(" in result
    assert "some_value" in result


# ---------------------------------------------------------------------------
# execute — tool input_json_delta accumulation and content_block_stop
# ---------------------------------------------------------------------------


async def test_execute_tool_name_emitted_on_content_block_start(tmp_path):
    """tool_name is emitted immediately on content_block_start."""
    rt = make_runtime(tmp_path)
    tool_events = []

    async def record_progress(delta, tool):
        if tool:
            tool_events.append(tool)

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'},
        }},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("run"), record_progress, noop_permission)

    # First event: just "Bash"; last event: "Bash(ls)" from content_block_stop.
    assert any(t == "Bash" for t in tool_events), "tool name not emitted on start"
    assert any("Bash(" in t and "ls" in t for t in tool_events), "formatted tool not emitted on stop"


async def test_execute_formatted_tool_from_fetch(tmp_path):
    """Fetch tool gets URL formatted in the tool_name."""
    rt = make_runtime(tmp_path)
    tool_events = []

    async def record_progress(delta, tool):
        if tool:
            tool_events.append(tool)

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "t2", "name": "Fetch"},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"url": "https://example.com"}'},
        }},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await rt.execute(make_task("fetch"), record_progress, noop_permission)

    final_tool = tool_events[-1]
    assert "Fetch(" in final_tool
    assert "https://example.com" in final_tool


async def test_execute_input_json_delta_for_non_tool_block_ignored(tmp_path):
    """input_json_delta for a block not in tool_blocks is safely ignored."""
    rt = make_runtime(tmp_path)

    lines = make_ndjson(
        {"type": "system", "session_id": "s1"},
        # input_json_delta for index 99 which was never started
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 99,
            "delta": {"type": "input_json_delta", "partial_json": '{"x": 1}'},
        }},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1"},
    )
    proc = mock_proc_with_output(lines)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await rt.execute(make_task("hi"), noop_progress, noop_permission)

    assert result == "ok"
