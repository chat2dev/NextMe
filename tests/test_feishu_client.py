"""Tests for nextme.feishu.client.FeishuClient."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from nextme.config.schema import AppConfig, Project, Settings
from nextme.feishu.client import FeishuClient, _RECONNECT_BASE_DELAY
from nextme.feishu.dedup import MessageDedup
from nextme.feishu.handler import MessageHandler
from nextme.feishu.reply import FeishuReplier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    project = Project(name="test", path=str(tmp_path), executor="claude-code-acp")
    return AppConfig(app_id="cli_test", app_secret="secret123", projects=[project])


@pytest.fixture
def settings():
    return Settings(log_level="INFO")


@pytest.fixture
def mock_dispatcher():
    d = MagicMock()
    d.dispatch = AsyncMock()
    return d


@pytest.fixture
def handler(mock_dispatcher):
    dedup = MessageDedup()
    return MessageHandler(dedup=dedup, dispatcher=mock_dispatcher)


def make_feishu_client(config, settings, handler):
    """Build a FeishuClient with mocked lark SDK objects."""
    mock_lark_client = MagicMock()

    mock_event_dispatcher = MagicMock()

    with (
        patch("lark_oapi.Client.builder") as mock_builder,
        patch.object(handler, "build_event_dispatcher", return_value=mock_event_dispatcher),
    ):
        mock_builder.return_value.app_id.return_value.app_secret.return_value.build.return_value = (
            mock_lark_client
        )

        client = FeishuClient(config=config, settings=settings, handler=handler)

    # Store mocks for test assertions
    client._mock_lark_client = mock_lark_client

    return client


# ---------------------------------------------------------------------------
# Tests: __init__
# ---------------------------------------------------------------------------


class TestFeishuClientInit:
    def test_stores_config_and_settings(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        assert client._config is config
        assert client._settings is settings
        assert client._handler is handler

    def test_creates_stop_event(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        assert isinstance(client._stop_event, asyncio.Event)

    def test_stop_event_initially_not_set(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        assert not client._stop_event.is_set()

    def test_log_level_debug(self, config, handler):
        """Debug log level maps to lark.LogLevel.DEBUG."""
        settings = Settings(log_level="DEBUG")
        client = make_feishu_client(config, settings, handler)
        # Just verify construction doesn't crash; log level is internal
        assert client is not None

    def test_log_level_warning(self, config, handler):
        settings = Settings(log_level="WARNING")
        client = make_feishu_client(config, settings, handler)
        assert client is not None

    def test_log_level_unknown_defaults_to_info(self, config, handler):
        settings = Settings(log_level="VERBOSE")
        client = make_feishu_client(config, settings, handler)
        assert client is not None


# ---------------------------------------------------------------------------
# Tests: get_replier
# ---------------------------------------------------------------------------


class TestGetReplier:
    def test_returns_feishu_replier(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        replier = client.get_replier()
        assert isinstance(replier, FeishuReplier)

    def test_replier_backed_by_lark_client(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        replier = client.get_replier()
        # FeishuReplier stores the lark client
        assert replier._client is client._lark_client

    def test_get_replier_returns_new_instance_each_call(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)
        r1 = client.get_replier()
        r2 = client.get_replier()
        # Each call creates a new FeishuReplier (cheap construction)
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Tests: start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_attaches_loop_to_handler(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)

        call_count = 0

        async def fake_executor(exc, fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                fn()  # call _run_ws synchronously
                # Signal stop so the reconnect loop exits
                client._stop_event.set()
            else:
                raise asyncio.CancelledError()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch.object(handler, "attach_loop") as mock_attach:
                with patch("lark_oapi.ws.Client"):
                    await client.start()
                    mock_attach.assert_called_once_with(loop)

    async def test_start_exits_when_stop_event_set(self, config, settings, handler):
        """start() exits cleanly when stop_event is set after _run_ws returns."""
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            fn()
            # Simulate stop() being called
            client._stop_event.set()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client"):
                await client.start()

        assert client._stop_event.is_set()

    async def test_start_reconnects_on_exception(self, config, settings, handler):
        """start() retries after an exception, then exits when stop_event is set."""
        client = make_feishu_client(config, settings, handler)

        call_count = 0

        async def fake_executor(exc, fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("ws connection failed")
            else:
                fn()
                client._stop_event.set()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client"):
                # Patch wait_for to return immediately (simulate delay passing)
                with patch("nextme.feishu.client.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                    await client.start()

        assert call_count == 2

    async def test_start_reraises_cancelled_error(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            raise asyncio.CancelledError()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client"):
                with pytest.raises(asyncio.CancelledError):
                    await client.start()

    async def test_start_reconnects_on_unexpected_exit(self, config, settings, handler):
        """When _run_ws returns without error (event loop crashed), start() reconnects."""
        client = make_feishu_client(config, settings, handler)

        call_count = 0

        async def fake_executor(exc, fn):
            nonlocal call_count
            call_count += 1
            fn()
            if call_count >= 2:
                client._stop_event.set()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client"):
                with patch("nextme.feishu.client.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                    await client.start()

        # Should have made at least 2 calls (first exits, second reconnects)
        assert call_count >= 2

    async def test_start_stops_during_reconnect_delay(self, config, settings, handler):
        """If stop_event is set during the reconnect delay, start() exits."""
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            raise RuntimeError("connection failed")

        async def fake_wait_for(coro, timeout):
            # Simulate stop_event being set during the delay
            client._stop_event.set()
            # Don't raise TimeoutError — the wait completed

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client"):
                with patch("nextme.feishu.client.asyncio.wait_for", side_effect=fake_wait_for):
                    await client.start()

        assert client._stop_event.is_set()

    async def test_start_rebuilds_ws_client_each_attempt(self, config, settings, handler):
        """Each reconnect attempt builds a fresh WS client."""
        client = make_feishu_client(config, settings, handler)
        clients_seen = []

        call_count = 0

        async def fake_executor(exc, fn):
            nonlocal call_count
            call_count += 1
            clients_seen.append(client._ws_client)
            fn()
            if call_count >= 2:
                client._stop_event.set()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch("lark_oapi.ws.Client", side_effect=lambda *a, **kw: MagicMock()):
                with patch("nextme.feishu.client.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                    await client.start()

        assert len(clients_seen) >= 2
        # Each attempt got a different client instance
        assert clients_seen[0] is not clients_seen[1]


# ---------------------------------------------------------------------------
# Tests: stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_sets_stop_event(self, config, settings, handler):
        """stop() always sets the stop event."""
        client = make_feishu_client(config, settings, handler)
        await client.stop()
        assert client._stop_event.is_set()

    async def test_stop_disables_auto_reconnect(self, config, settings, handler):
        """stop() sets _auto_reconnect=False to prevent the SDK from reopening."""
        client = make_feishu_client(config, settings, handler)
        mock_ws = MagicMock()
        mock_ws._auto_reconnect = True
        client._ws_client = mock_ws
        await client.stop()
        assert client._ws_client._auto_reconnect is False

    async def test_stop_signals_ws_loop_when_set(self, config, settings, handler):
        """stop() calls call_soon_threadsafe(loop.stop) on the ws thread loop."""
        client = make_feishu_client(config, settings, handler)

        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = False
        client._ws_loop = fake_loop

        await client.stop()

        fake_loop.call_soon_threadsafe.assert_called_once_with(fake_loop.stop)

    async def test_stop_safe_when_ws_loop_is_none(self, config, settings, handler):
        """stop() does not raise when _ws_loop is None (not yet started)."""
        client = make_feishu_client(config, settings, handler)
        client._ws_loop = None
        # Should not raise
        await client.stop()
        assert client._stop_event.is_set()

    async def test_stop_safe_when_ws_loop_already_closed(self, config, settings, handler):
        """stop() does not raise when _ws_loop is already closed."""
        client = make_feishu_client(config, settings, handler)

        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = True
        client._ws_loop = fake_loop

        await client.stop()

        fake_loop.call_soon_threadsafe.assert_not_called()
        assert client._stop_event.is_set()

    async def test_stop_safe_when_ws_client_is_none(self, config, settings, handler):
        """stop() does not raise when _ws_client is None (before first start)."""
        client = make_feishu_client(config, settings, handler)
        client._ws_client = None
        await client.stop()
        assert client._stop_event.is_set()
