"""Structural Protocol satisfaction tests for core/interfaces.py.

Verifies that concrete implementations structurally satisfy the Protocol
contracts declared in :mod:`nextme.core.interfaces` using Python's
``@runtime_checkable`` ``isinstance`` checks.

Test classes:

* :class:`TestReplierProtocol`      — ``FeishuReplier`` satisfies ``Replier``
* :class:`TestIMAdapterProtocol`    — ``FeishuClient`` satisfies ``IMAdapter``
* :class:`TestAgentRuntimeProtocol` — ``ACPRuntime`` satisfies ``AgentRuntime``
* :class:`TestProtocolNegativeCases`` — incomplete objects do NOT satisfy the protocols
"""

from __future__ import annotations

import asyncio
import inspect
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.config.schema import AppConfig, Project, Settings
from nextme.core.interfaces import AgentRuntime, IMAdapter, Replier
from nextme.feishu.reply import FeishuReplier
from nextme.feishu.client import FeishuClient
from nextme.feishu.dedup import MessageDedup
from nextme.feishu.handler import MessageHandler
from nextme.acp.runtime import ACPRuntime
from nextme.protocol.types import PermOption


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_mock_lark_client() -> MagicMock:
    """Return a minimal MagicMock that satisfies FeishuReplier's needs."""
    mock = MagicMock()
    ok_response = MagicMock()
    ok_response.success.return_value = True
    ok_response.data = MagicMock(message_id="msg_test_123")
    mock.im.v1.message.acreate = AsyncMock(return_value=ok_response)
    mock.im.v1.message.apatch = AsyncMock(return_value=ok_response)
    mock.im.v1.message_reaction.acreate = AsyncMock(return_value=ok_response)
    return mock


def make_feishu_replier() -> FeishuReplier:
    return FeishuReplier(make_mock_lark_client())


def make_feishu_client(tmp_path) -> FeishuClient:
    """Construct a FeishuClient with the lark SDK fully mocked."""
    project = Project(name="test", path=str(tmp_path), executor="claude-code-acp")
    config = AppConfig(app_id="app_test", app_secret="secret_test", projects=[project])
    settings = Settings()
    dedup = MessageDedup()
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch = AsyncMock()
    handler = MessageHandler(dedup=dedup, dispatcher=mock_dispatcher)

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

    return client


def make_acp_runtime(tmp_path) -> ACPRuntime:
    return ACPRuntime(
        session_id="oc_chat_test:ou_user_test",
        cwd=str(tmp_path),
        settings=Settings(),
    )


# ---------------------------------------------------------------------------
# TestReplierProtocol
# ---------------------------------------------------------------------------


class TestReplierProtocol:
    """FeishuReplier structurally satisfies the Replier Protocol."""

    def test_feishu_replier_isinstance_replier(self):
        r = make_feishu_replier()
        assert isinstance(r, Replier)

    # ---- async send methods are coroutine functions ----

    def test_send_text_is_coroutine_function(self):
        r = make_feishu_replier()
        assert inspect.iscoroutinefunction(r.send_text)

    def test_send_card_is_coroutine_function(self):
        r = make_feishu_replier()
        assert inspect.iscoroutinefunction(r.send_card)

    def test_update_card_is_coroutine_function(self):
        r = make_feishu_replier()
        assert inspect.iscoroutinefunction(r.update_card)

    def test_send_reaction_is_coroutine_function(self):
        r = make_feishu_replier()
        assert inspect.iscoroutinefunction(r.send_reaction)

    # ---- sync card builders return non-empty str ----

    def test_build_progress_card_returns_nonempty_str(self):
        r = make_feishu_replier()
        result = r.build_progress_card(status="", content="hello", title="test")
        assert isinstance(result, str) and result

    def test_build_result_card_returns_nonempty_str(self):
        r = make_feishu_replier()
        result = r.build_result_card(content="done", title="完成")
        assert isinstance(result, str) and result

    def test_build_permission_card_returns_nonempty_str(self):
        r = make_feishu_replier()
        opts = [PermOption(index=1, label="允许"), PermOption(index=2, label="拒绝")]
        result = r.build_permission_card(description="是否允许?", options=opts)
        assert isinstance(result, str) and result

    def test_build_error_card_returns_nonempty_str(self):
        r = make_feishu_replier()
        result = r.build_error_card("something went wrong")
        assert isinstance(result, str) and result

    def test_build_help_card_returns_nonempty_str(self):
        r = make_feishu_replier()
        result = r.build_help_card([("/new", "reset"), ("/help", "帮助")])
        assert isinstance(result, str) and result


# ---------------------------------------------------------------------------
# TestIMAdapterProtocol
# ---------------------------------------------------------------------------


class TestIMAdapterProtocol:
    """FeishuClient structurally satisfies the IMAdapter Protocol."""

    def test_feishu_client_isinstance_im_adapter(self, tmp_path):
        client = make_feishu_client(tmp_path)
        assert isinstance(client, IMAdapter)

    def test_start_is_coroutine_function(self, tmp_path):
        client = make_feishu_client(tmp_path)
        assert inspect.iscoroutinefunction(client.start)

    def test_stop_is_coroutine_function(self, tmp_path):
        client = make_feishu_client(tmp_path)
        assert inspect.iscoroutinefunction(client.stop)

    def test_get_replier_is_callable(self, tmp_path):
        client = make_feishu_client(tmp_path)
        assert callable(client.get_replier)

    def test_get_replier_returns_replier_instance(self, tmp_path):
        client = make_feishu_client(tmp_path)
        replier = client.get_replier()
        assert isinstance(replier, Replier)


# ---------------------------------------------------------------------------
# TestAgentRuntimeProtocol
# ---------------------------------------------------------------------------


class TestAgentRuntimeProtocol:
    """ACPRuntime structurally satisfies the AgentRuntime Protocol."""

    def test_acp_runtime_isinstance_agent_runtime(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert isinstance(r, AgentRuntime)

    # ---- properties have correct initial types ----

    def test_is_running_is_bool(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert isinstance(r.is_running, bool)

    def test_is_running_false_before_start(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert r.is_running is False

    def test_last_access_is_datetime(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert isinstance(r.last_access, datetime)

    def test_actual_id_is_none_before_start(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert r.actual_id is None

    # ---- async methods are coroutine functions ----

    def test_ensure_ready_is_coroutine_function(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert inspect.iscoroutinefunction(r.ensure_ready)

    def test_execute_is_coroutine_function(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert inspect.iscoroutinefunction(r.execute)

    def test_cancel_is_coroutine_function(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert inspect.iscoroutinefunction(r.cancel)

    def test_reset_session_is_coroutine_function(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert inspect.iscoroutinefunction(r.reset_session)

    def test_stop_is_coroutine_function(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        assert inspect.iscoroutinefunction(r.stop)

    # ---- safe to call cancel/stop when subprocess is not running ----

    @pytest.mark.asyncio
    async def test_cancel_does_not_raise_when_idle(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        await r.cancel()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_does_not_raise_when_idle(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        await r.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_reset_session_does_not_raise_when_idle(self, tmp_path):
        r = make_acp_runtime(tmp_path)
        await r.reset_session()
        assert r.actual_id is None


# ---------------------------------------------------------------------------
# TestProtocolNegativeCases
# ---------------------------------------------------------------------------


class TestProtocolNegativeCases:
    """Objects that do not implement all required members are not instances."""

    # ---- Replier ----

    def test_empty_class_not_replier(self):
        class Empty:
            pass

        assert not isinstance(Empty(), Replier)

    def test_missing_send_text_not_replier(self):
        class MissingSendText:
            # Has all Replier members except send_text
            async def send_card(self, chat_id, card_json): ...
            async def update_card(self, message_id, card_json): ...
            async def send_reaction(self, message_id, emoji): ...
            def build_progress_card(self, status, content, title=""): ...
            def build_result_card(self, content, title="", template="", reasoning="", session_id=""): ...
            def build_permission_card(self, description, options, session_id=""): ...
            def build_error_card(self, error): ...
            def build_help_card(self, commands): ...

        assert not isinstance(MissingSendText(), Replier)

    def test_missing_build_method_not_replier(self):
        class MissingBuilder:
            async def send_text(self, chat_id, text): ...
            async def send_card(self, chat_id, card_json): ...
            async def update_card(self, message_id, card_json): ...
            async def send_reaction(self, message_id, emoji): ...
            # Missing all build_* methods

        assert not isinstance(MissingBuilder(), Replier)

    # ---- IMAdapter ----

    def test_empty_class_not_im_adapter(self):
        class Empty:
            pass

        assert not isinstance(Empty(), IMAdapter)

    def test_missing_get_replier_not_im_adapter(self):
        class MissingGetReplier:
            async def start(self): ...
            async def stop(self): ...
            # Missing get_replier

        assert not isinstance(MissingGetReplier(), IMAdapter)

    # ---- AgentRuntime ----

    def test_empty_class_not_agent_runtime(self):
        class Empty:
            pass

        assert not isinstance(Empty(), AgentRuntime)

    def test_missing_ensure_ready_not_agent_runtime(self):
        class MissingEnsureReady:
            @property
            def is_running(self): return False

            @property
            def last_access(self): return datetime.now()

            @property
            def actual_id(self): return None

            async def execute(self, task, on_progress, on_permission): ...
            async def cancel(self): ...
            async def reset_session(self): ...
            async def stop(self): ...
            # Missing ensure_ready

        assert not isinstance(MissingEnsureReady(), AgentRuntime)
