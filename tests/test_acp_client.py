"""Tests for nextme.acp.client — ACPClient send/read_lines."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.acp.client import ACPClient
from nextme.acp.protocol import PromptMsg, NewSessionMsg, serialize_msg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_proc(stdin=True, stdout=True):
    """Build a MagicMock process with optional async stdin/stdout."""
    mock_proc = MagicMock()

    if stdin:
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
    else:
        mock_proc.stdin = None

    if stdout:
        mock_proc.stdout = AsyncMock()
    else:
        mock_proc.stdout = None

    return mock_proc


# ---------------------------------------------------------------------------
# send() tests
# ---------------------------------------------------------------------------


class TestACPClientSend:
    async def test_send_writes_serialized_ndjson_with_newline(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)
        msg = PromptMsg(session_id="s1", content="hello")

        await client.send(msg)

        expected_line = (serialize_msg(msg) + "\n").encode("utf-8")
        mock_proc.stdin.write.assert_called_once_with(expected_line)

    async def test_send_calls_drain(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        await client.send(PromptMsg(session_id="s1", content="test"))

        mock_proc.stdin.drain.assert_awaited_once()

    async def test_send_raises_runtime_error_when_stdin_is_none(self):
        mock_proc = make_mock_proc(stdin=False)
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdin is not available"):
            await client.send(PromptMsg(session_id="s1", content="test"))

    async def test_send_raises_runtime_error_on_broken_pipe(self):
        mock_proc = make_mock_proc()
        mock_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("pipe broken"))
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="failed to write"):
            await client.send(PromptMsg(session_id="s1", content="test"))

    async def test_send_raises_runtime_error_on_connection_reset(self):
        mock_proc = make_mock_proc()
        mock_proc.stdin.drain = AsyncMock(side_effect=ConnectionResetError("reset"))
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="failed to write"):
            await client.send(PromptMsg(session_id="s1", content="test"))

    async def test_send_new_session_msg(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)
        msg = NewSessionMsg(session_id="sess-abc", cwd="/home/user")

        await client.send(msg)

        expected_bytes = (serialize_msg(msg) + "\n").encode("utf-8")
        mock_proc.stdin.write.assert_called_once_with(expected_bytes)


# ---------------------------------------------------------------------------
# read_lines() tests
# ---------------------------------------------------------------------------


class TestACPClientReadLines:
    async def test_read_lines_yields_parsed_dicts(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b'{"type": "ready"}\n',
            b'{"type": "done", "content": "hi"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert len(results) == 2
        assert results[0] == {"type": "ready"}
        assert results[1] == {"type": "done", "content": "hi"}

    async def test_read_lines_stops_at_eof(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[b""])
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert results == []

    async def test_read_lines_skips_empty_lines(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b"\n",
            b"   \n",
            b'{"type": "ready"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert len(results) == 1
        assert results[0] == {"type": "ready"}

    async def test_read_lines_skips_unparseable_lines(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b"this is not json\n",
            b'{"type": "done"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        # Unparseable line is skipped; only the valid dict is yielded
        assert len(results) == 1
        assert results[0] == {"type": "done"}

    async def test_read_lines_skips_json_array_lines(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b'[{"type": "ready"}]\n',
            b'{"type": "done"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert len(results) == 1
        assert results[0] == {"type": "done"}

    async def test_read_lines_handles_unicode_decode_error(self):
        mock_proc = make_mock_proc()
        # First bytes: invalid UTF-8 sequence; second: valid JSON; third: EOF
        invalid_utf8 = b"\xff\xfe"
        side_effects = [
            invalid_utf8,
            b'{"type": "done"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        # The bad bytes line is skipped, valid line is yielded
        assert len(results) == 1
        assert results[0] == {"type": "done"}

    async def test_read_lines_raises_runtime_error_when_stdout_is_none(self):
        mock_proc = make_mock_proc(stdout=False)
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdout is not available"):
            async for _ in client.read_lines():
                pass

    async def test_read_lines_multiple_valid_messages(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b'{"type": "session_created", "session_id": "abc"}\n',
            b'{"type": "content_delta", "delta": "Hello"}\n',
            b'{"type": "content_delta", "delta": " World"}\n',
            b'{"type": "done"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert len(results) == 4
        assert results[0]["type"] == "session_created"
        assert results[1]["type"] == "content_delta"
        assert results[2]["delta"] == " World"
        assert results[3]["type"] == "done"

    async def test_read_lines_strips_trailing_newline_from_lines(self):
        mock_proc = make_mock_proc()
        side_effects = [
            b'{"type": "ready"}\n',
            b"",
        ]
        mock_proc.stdout.readline = AsyncMock(side_effect=side_effects)
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert results[0] == {"type": "ready"}

    async def test_read_lines_handles_readline_exception_gracefully(self):
        """A generic exception from readline breaks the loop without raising."""
        mock_proc = make_mock_proc()
        # First call raises OSError, which should break the loop
        mock_proc.stdout.readline = AsyncMock(side_effect=OSError("read error"))
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        # No messages yielded, no exception propagated
        assert results == []
