"""Tests for nextme.feishu.client.FeishuClient."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from nextme.config.schema import AppConfig, Project, Settings
from nextme.feishu.client import FeishuClient
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
    mock_ws_client = MagicMock()
    mock_ws_client.start = MagicMock()
    mock_ws_client.stop = MagicMock()

    mock_event_dispatcher = MagicMock()

    with (
        patch("lark_oapi.Client.builder") as mock_builder,
        patch("lark_oapi.ws.Client") as mock_ws_cls,
        patch.object(handler, "build_event_dispatcher", return_value=mock_event_dispatcher),
    ):
        mock_builder.return_value.app_id.return_value.app_secret.return_value.build.return_value = (
            mock_lark_client
        )
        mock_ws_cls.return_value = mock_ws_client

        client = FeishuClient(config=config, settings=settings, handler=handler)

    # Store mocks for test assertions
    client._mock_lark_client = mock_lark_client
    client._mock_ws_client = mock_ws_client

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
        # run_in_executor call will block; mock it to return immediately
        client._mock_ws_client.start = MagicMock()

        async def fake_executor(exc, fn):
            fn()  # call ws_client.start synchronously

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with patch.object(handler, "attach_loop") as mock_attach:
                await client.start()
                mock_attach.assert_called_once_with(loop)

    async def test_start_sets_stop_event_on_completion(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            fn()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            await client.start()

        assert client._stop_event.is_set()

    async def test_start_sets_stop_event_on_exception(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            raise RuntimeError("ws connection failed")

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with pytest.raises(RuntimeError, match="ws connection failed"):
                await client.start()

        assert client._stop_event.is_set()

    async def test_start_reraises_cancelled_error(self, config, settings, handler):
        client = make_feishu_client(config, settings, handler)

        async def fake_executor(exc, fn):
            raise asyncio.CancelledError()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_executor):
            with pytest.raises(asyncio.CancelledError):
                await client.start()

        # stop_event must still be set in finally block
        assert client._stop_event.is_set()


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
        client._ws_client._auto_reconnect = True
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
