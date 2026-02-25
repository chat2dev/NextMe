"""Tests for nextme.acp.client — ACPClient JSON-RPC I/O."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.acp.client import ACPClient


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
# send_request() tests
# ---------------------------------------------------------------------------


class TestACPClientSendRequest:
    async def test_sends_valid_jsonrpc(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        req_id = await client.send_request("initialize", {"protocolVersion": 1})

        assert req_id == 1
        written = mock_proc.stdin.write.call_args[0][0]
        d = json.loads(written.decode("utf-8").rstrip())
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["method"] == "initialize"
        assert d["params"]["protocolVersion"] == 1

    async def test_increments_id(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        id1 = await client.send_request("session/new", {"cwd": "/a", "mcpServers": []})
        id2 = await client.send_request("session/prompt", {"sessionId": "x", "prompt": []})

        assert id1 == 1
        assert id2 == 2

    async def test_calls_drain(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        await client.send_request("initialize", {})

        mock_proc.stdin.drain.assert_awaited_once()

    async def test_raises_when_stdin_is_none(self):
        mock_proc = make_mock_proc(stdin=False)
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdin is not available"):
            await client.send_request("initialize", {})

    async def test_raises_on_broken_pipe(self):
        mock_proc = make_mock_proc()
        mock_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("pipe broken"))
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdin write failed"):
            await client.send_request("initialize", {})

    async def test_raises_on_connection_reset(self):
        mock_proc = make_mock_proc()
        mock_proc.stdin.drain = AsyncMock(side_effect=ConnectionResetError("reset"))
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdin write failed"):
            await client.send_request("session/new", {"cwd": "/tmp", "mcpServers": []})


# ---------------------------------------------------------------------------
# send_response() / send_error_response() tests
# ---------------------------------------------------------------------------


class TestACPClientSendResponse:
    async def test_send_response_writes_valid_json(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        await client.send_response(7, {"outcome": {"selected": {"optionId": "allow_once"}}})

        written = mock_proc.stdin.write.call_args[0][0]
        d = json.loads(written.decode("utf-8").rstrip())
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 7
        assert d["result"]["outcome"]["selected"]["optionId"] == "allow_once"

    async def test_send_error_response(self):
        mock_proc = make_mock_proc()
        client = ACPClient(mock_proc)

        await client.send_error_response(3, -32600, "invalid request")

        written = mock_proc.stdin.write.call_args[0][0]
        d = json.loads(written.decode("utf-8").rstrip())
        assert d["error"]["code"] == -32600
        assert d["error"]["message"] == "invalid request"


# ---------------------------------------------------------------------------
# read_lines() tests
# ---------------------------------------------------------------------------


class TestACPClientReadLines:
    async def test_yields_parsed_dicts(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = []
        async for msg in client.read_lines():
            results.append(msg)

        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[1]["method"] == "session/update"

    async def test_stops_at_eof(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[b""])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert results == []

    async def test_skips_empty_lines(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b"\n",
            b"   \n",
            b'{"id":1,"result":{}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert len(results) == 1

    async def test_skips_non_json_diagnostic_lines(self):
        """Lines not starting with '{' are silently ignored."""
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b"[ACP] No CLAUDE_API_KEY found, using Claude Code subscription authentication\n",
            b'{"id":1,"result":{"protocolVersion":1}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert len(results) == 1
        assert results[0]["result"]["protocolVersion"] == 1

    async def test_skips_unparseable_json(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b"{broken json\n",
            b'{"id":1,"result":{}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert len(results) == 1

    async def test_skips_unicode_decode_errors(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b"\xff\xfe",
            b'{"id":1,"result":{}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert len(results) == 1

    async def test_raises_when_stdout_is_none(self):
        mock_proc = make_mock_proc(stdout=False)
        client = ACPClient(mock_proc)

        with pytest.raises(RuntimeError, match="stdout is not available"):
            async for _ in client.read_lines():
                pass

    async def test_multiple_messages(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}\n',
            b'{"jsonrpc":"2.0","id":2,"result":{"sessionId":"abc"}}\n',
            b'{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk"}}}\n',
            b"",
        ])
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert len(results) == 3
        assert results[0]["result"]["protocolVersion"] == 1
        assert results[1]["result"]["sessionId"] == "abc"
        assert results[2]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"

    async def test_handles_readline_exception_gracefully(self):
        mock_proc = make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=OSError("read error"))
        client = ACPClient(mock_proc)

        results = [msg async for msg in client.read_lines()]
        assert results == []
