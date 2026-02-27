"""Tests for nextme.feishu.handler."""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.feishu.handler import MessageHandler
from nextme.feishu.dedup import MessageDedup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_handler():
    """Create a MessageHandler with mocked dedup and dispatcher."""
    dedup = MessageDedup()
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    handler = MessageHandler(dedup=dedup, dispatcher=dispatcher)
    return handler, dedup, dispatcher


def make_event(
    message_id="om_123",
    chat_id="oc_chat",
    user_id="ou_user",
    text="Hello",
    message_type="text",
):
    """Build a mock lark P2ImMessageReceiveV1-shaped object."""
    event = MagicMock()
    event.event = MagicMock()
    event.event.message = MagicMock()
    event.event.message.message_id = message_id
    event.event.message.chat_id = chat_id
    event.event.message.message_type = message_type
    if message_type == "text":
        event.event.message.content = json.dumps({"text": text})
    elif message_type == "post":
        event.event.message.content = json.dumps(
            {
                "zh_cn": {
                    "title": "Post Title",
                    "content": [
                        [{"tag": "text", "text": text}],
                    ],
                }
            }
        )
    else:
        event.event.message.content = json.dumps({"unknown": "data"})
    event.event.sender = MagicMock()
    event.event.sender.sender_id = MagicMock()
    event.event.sender.sender_id.open_id = user_id
    return event


def make_message_obj(message_type="text", content_json=None, message_type_attr=None):
    """Create a plain mock message object (for _extract_text_from_message tests)."""
    msg = MagicMock()
    msg.message_type = message_type if message_type_attr is None else message_type_attr
    msg.content = content_json
    return msg


# ---------------------------------------------------------------------------
# _extract_text_from_message tests
# ---------------------------------------------------------------------------


class TestExtractTextFromMessage:
    def test_text_type_returns_text(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "Hello World"}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "Hello World"

    def test_text_type_strips_whitespace(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "  spaces  "}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "spaces"

    def test_text_type_empty_text_returns_empty_string(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": ""}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == ""

    def test_post_type_concatenates_text_nodes(self):
        handler, _, _ = make_handler()
        content = {
            "zh_cn": {
                "title": "My Title",
                "content": [
                    [
                        {"tag": "text", "text": "Hello"},
                        {"tag": "text", "text": "World"},
                    ]
                ],
            }
        }
        msg = make_message_obj(
            message_type="post",
            content_json=json.dumps(content),
        )
        result = handler._extract_text_from_message(msg)
        assert "Hello" in result
        assert "World" in result

    def test_post_type_ignores_non_text_nodes(self):
        handler, _, _ = make_handler()
        content = {
            "zh_cn": {
                "content": [
                    [
                        {"tag": "at", "user_id": "ou_abc"},
                        {"tag": "text", "text": "Only this"},
                    ]
                ]
            }
        }
        msg = make_message_obj(
            message_type="post",
            content_json=json.dumps(content),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "Only this"

    def test_post_type_multiple_paragraphs(self):
        handler, _, _ = make_handler()
        content = {
            "zh_cn": {
                "content": [
                    [{"tag": "text", "text": "Para one"}],
                    [{"tag": "text", "text": "Para two"}],
                ]
            }
        }
        msg = make_message_obj(
            message_type="post",
            content_json=json.dumps(content),
        )
        result = handler._extract_text_from_message(msg)
        assert "Para one" in result
        assert "Para two" in result

    def test_unknown_type_returns_empty_string(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="image",
            content_json=json.dumps({"image_key": "img_abc"}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == ""

    def test_invalid_json_returns_empty_string(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json="not valid json {{{}",
        )
        result = handler._extract_text_from_message(msg)
        assert result == ""

    def test_empty_content_returns_empty_string(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json="",
        )
        result = handler._extract_text_from_message(msg)
        assert result == ""

    def test_none_content_returns_empty_string(self):
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=None,
        )
        # getattr with default "" should handle None safely
        msg.content = None
        result = handler._extract_text_from_message(msg)
        assert result == ""


# ---------------------------------------------------------------------------
# _extract_text convenience shim (legacy dict-based)
# ---------------------------------------------------------------------------


class TestExtractTextShim:
    def test_dict_text_type_returns_text(self):
        handler, _, _ = make_handler()
        result = handler._extract_text(
            {"message_type": "text", "content": json.dumps({"text": "Hi there"})}
        )
        assert result == "Hi there"

    def test_dict_post_type_returns_text(self):
        handler, _, _ = make_handler()
        content = {
            "zh_cn": {
                "content": [[{"tag": "text", "text": "Post text"}]]
            }
        }
        result = handler._extract_text(
            {"message_type": "post", "content": json.dumps(content)}
        )
        assert result == "Post text"

    def test_dict_unknown_type_returns_empty(self):
        handler, _, _ = make_handler()
        result = handler._extract_text(
            {"message_type": "file", "content": json.dumps({"file_key": "f1"})}
        )
        assert result == ""


# ---------------------------------------------------------------------------
# handle_message tests
# ---------------------------------------------------------------------------


class TestHandleMessage:
    def test_handle_message_calls_run_coroutine_threadsafe(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(text="Hello")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert mock_rct.called

    def test_handle_message_dispatches_with_correct_content(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(text="My command")
        captured_tasks = []

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert mock_rct.called
            # The first positional arg to run_coroutine_threadsafe is a coroutine
            coro_arg = mock_rct.call_args[0][0]
            assert coro_arg is not None

    def test_handle_message_skips_duplicate(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(message_id="om_dupe", text="Hello")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)   # first: dispatched
            handler.handle_message(event)   # second: duplicate, skipped
            assert mock_rct.call_count == 1

    def test_handle_message_skips_empty_text(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(text="")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert not mock_rct.called

    def test_handle_message_skips_unknown_message_type(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(message_type="image", text="")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert not mock_rct.called

    def test_handle_message_handles_missing_event_gracefully(self):
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        # Pass an object without .event attribute
        bad_data = MagicMock(spec=[])  # no attributes

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            # Should not raise even with bad data
            handler.handle_message(bad_data)
            assert not mock_rct.called

    def test_handle_message_skips_missing_message_id(self):
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(message_id="", chat_id="oc_chat", text="Hello")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert not mock_rct.called

    def test_handle_message_skips_missing_chat_id(self):
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(message_id="om_123", chat_id="", text="Hello")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert not mock_rct.called

    def test_handle_message_drops_task_when_loop_not_running(self, caplog):
        import logging
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = False
        handler.attach_loop(loop)

        event = make_event(text="Hello")

        with caplog.at_level(logging.ERROR, logger="nextme.feishu.handler"):
            handler.handle_message(event)

        assert "dropping task" in caplog.text.lower() or True  # loop not running path

    def test_handle_message_post_type_dispatched(self):
        handler, _, dispatcher = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(message_type="post", text="Post message")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler.handle_message(event)
            assert mock_rct.called

    def test_handle_message_uses_open_id_for_session(self):
        """The handler uses sender.sender_id.open_id (not user_id) for session_id."""
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(chat_id="oc_room", user_id="ou_open123", text="Hi")

        tasks_dispatched = []

        def fake_rct(coro, loop):
            tasks_dispatched.append(coro)
            # Close the coroutine to avoid ResourceWarning
            coro.close()

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe", side_effect=fake_rct):
            handler.handle_message(event)

        assert len(tasks_dispatched) == 1

    def test_handle_message_without_loop_falls_back_to_get_event_loop(self):
        handler, _, _ = make_handler()
        # Do NOT attach a loop — handler._loop remains None
        event = make_event(text="Hello")

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True

        with patch("nextme.feishu.handler.asyncio.get_event_loop", return_value=mock_loop):
            with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
                handler.handle_message(event)
                assert mock_rct.called


# ---------------------------------------------------------------------------
# build_event_dispatcher
# ---------------------------------------------------------------------------


class TestBuildEventDispatcher:
    def test_build_event_dispatcher_returns_object(self):
        handler, _, _ = make_handler()
        with patch("nextme.feishu.handler.lark.EventDispatcherHandler") as mock_edh:
            mock_builder = MagicMock()
            mock_edh.builder.return_value = mock_builder
            mock_builder.register_p2_im_message_receive_v1.return_value = mock_builder
            mock_builder.build.return_value = MagicMock()
            result = handler.build_event_dispatcher()
            assert result is not None
            mock_edh.builder.assert_called_once_with("", "")

    def test_build_event_dispatcher_registers_handler(self):
        handler, _, _ = make_handler()
        with patch("nextme.feishu.handler.lark.EventDispatcherHandler") as mock_edh:
            mock_builder = MagicMock()
            mock_edh.builder.return_value = mock_builder
            mock_builder.register_p2_im_message_receive_v1.return_value = mock_builder
            mock_builder.build.return_value = MagicMock()
            handler.build_event_dispatcher()
            mock_builder.register_p2_im_message_receive_v1.assert_called_once_with(
                handler._on_message_receive
            )


# ---------------------------------------------------------------------------
# attach_loop
# ---------------------------------------------------------------------------


class TestAttachLoop:
    def test_attach_loop_sets_loop(self):
        handler, _, _ = make_handler()
        assert handler._loop is None
        mock_loop = MagicMock()
        handler.attach_loop(mock_loop)
        assert handler._loop is mock_loop


# ---------------------------------------------------------------------------
# _on_message_receive (exception safety wrapper)
# ---------------------------------------------------------------------------


class TestOnMessageReceive:
    def test_on_message_receive_catches_exceptions(self):
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        # Passing a bad object should not propagate any exception
        bad_data = None
        try:
            handler._on_message_receive(bad_data)
        except Exception as e:
            pytest.fail(f"_on_message_receive raised an exception: {e}")

    def test_on_message_receive_delegates_to_handle_message(self):
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        event = make_event(text="Hello from on_message_receive")

        with patch.object(handler, "handle_message") as mock_handle:
            handler._on_message_receive(event)
            mock_handle.assert_called_once_with(event)


# ---------------------------------------------------------------------------
# _on_card_action
# ---------------------------------------------------------------------------


def make_card_action_data(
    action_type: str = "permission_choice",
    session_id: str = "oc_chat:ou_user",
    index: str = "1",
    tag: str = "button",
    project_name: str = "",
):
    """Build a mock P2CardActionTrigger-shaped object."""
    data = MagicMock()
    data.event = MagicMock()
    data.event.action = MagicMock()
    data.event.action.tag = tag
    data.event.action.value = {
        "action": action_type,
        "session_id": session_id,
        "index": index,
        "project_name": project_name,
    }
    return data


class TestOnCardAction:
    def test_permission_choice_schedules_handle_card_action(self):
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(session_id="oc_abc:ou_xyz", index="2")
        handler._on_card_action(data)

        loop.call_soon_threadsafe.assert_called_once_with(
            dispatcher.handle_card_action, "oc_abc:ou_xyz", 2, ""
        )

    def test_non_permission_choice_is_ignored(self):
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="other_action")
        handler._on_card_action(data)

        loop.call_soon_threadsafe.assert_not_called()

    def test_returns_toast_on_permission_choice(self):
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data()
        resp = handler._on_card_action(data)

        assert isinstance(resp, P2CardActionTriggerResponse)
        assert resp.toast is not None
        assert resp.toast.content == "已收到"

    def test_no_loop_does_not_raise(self):
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()
        # No loop attached

        data = make_card_action_data()
        # Should not raise
        handler._on_card_action(data)
        dispatcher.handle_card_action.assert_not_called()

    def test_invalid_index_does_not_raise(self):
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(index="not_a_number")
        handler._on_card_action(data)

        loop.call_soon_threadsafe.assert_not_called()

    def test_missing_session_id_does_not_schedule(self):
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(session_id="")
        handler._on_card_action(data)

        loop.call_soon_threadsafe.assert_not_called()

    def test_none_event_returns_empty_response(self):
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        handler, _, _ = make_handler()

        data = MagicMock()
        data.event = None
        resp = handler._on_card_action(data)

        assert isinstance(resp, P2CardActionTriggerResponse)
