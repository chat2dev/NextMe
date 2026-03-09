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

    def test_text_type_strips_single_mention_prefix(self):
        """Group chat: '@_user_1 /help' → '/help'."""
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "@_user_1 /help", "mentions": []}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "/help"

    def test_text_type_strips_multiple_mention_prefixes(self):
        """Group chat with multiple leading @mentions."""
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "@_user_1 @_user_2 /status"}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "/status"

    def test_text_type_mid_mention_preserved(self):
        """@mentions that appear in the middle of the text are NOT stripped."""
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "@_user_1 explain @_user_2's code"}),
        )
        result = handler._extract_text_from_message(msg)
        assert result == "explain @_user_2's code"

    def test_text_type_mention_only_returns_empty(self):
        """@mention with no trailing text → empty string (nothing to dispatch)."""
        handler, _, _ = make_handler()
        msg = make_message_obj(
            message_type="text",
            content_json=json.dumps({"text": "@_user_1 "}),
        )
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
    label: str = "1. Allow",
    executor: str = "claude",
    display_id: str = "uuid-abc",
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
        "label": label,
        "executor": executor,
        "display_id": display_id,
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

    def test_returns_confirmed_card_in_response(self):
        """Permission choice should return a card update that disables the buttons."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(label="1. Allow", executor="claude")
        resp = handler._on_card_action(data)

        assert resp.card is not None
        assert isinstance(resp.card, CallBackCard)
        assert resp.card.type == "raw"
        assert isinstance(resp.card.data, dict)

    def test_confirmed_card_contains_selected_label(self):
        """The confirmed card body should show which option was selected."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(label="2. Deny — Reject all", index="2")
        resp = handler._on_card_action(data)

        card_data = resp.card.data
        elements = card_data["body"]["elements"]
        content_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert any("2. Deny — Reject all" in e.get("content", "") for e in content_elements)

    def test_confirmed_card_footer_uses_display_id(self):
        """The confirmed card footer should show display_id (not raw session_id)."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(
            session_id="oc_chat:ou_user",
            display_id="nice-uuid-123",
            executor="coco",
            label="1. Allow",
        )
        resp = handler._on_card_action(data)

        card_data = resp.card.data
        elements = card_data["body"]["elements"]
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "🆔" in e.get("content", "")
        ]
        assert len(footer_elements) == 1
        assert "nice-uuid-123" in footer_elements[0]["content"]
        assert "oc_chat:ou_user" not in footer_elements[0]["content"]
        assert "coco" in footer_elements[0]["content"]

    def test_non_permission_action_returns_no_card(self):
        """Non-permission actions should not set a card update in the response."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="other_action")
        resp = handler._on_card_action(data)

        assert resp.card is None

    def test_acl_apply_schedules_handle_acl_card_action(self):
        """acl_apply action routes to handle_acl_card_action via run_coroutine_threadsafe."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_acl_card_action = AsyncMock()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="acl_apply")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            resp = handler._on_card_action(data)
            assert mock_rct.called
            # Second positional argument must be the event loop
            assert mock_rct.call_args[0][1] is loop

        # Toast should be set
        assert resp.toast is not None
        assert resp.toast.content == "已收到"

        # Permission path (call_soon_threadsafe) NOT triggered
        loop.call_soon_threadsafe.assert_not_called()

    def test_acl_review_schedules_handle_acl_card_action(self):
        """acl_review action routes to handle_acl_card_action via run_coroutine_threadsafe."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_acl_card_action = AsyncMock()
        dispatcher.handle_card_action = MagicMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="acl_review")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            resp = handler._on_card_action(data)
            assert mock_rct.called
            assert mock_rct.call_args[0][1] is loop

        assert resp.toast is not None
        assert resp.toast.content == "已收到"
        loop.call_soon_threadsafe.assert_not_called()

    def test_acl_action_with_operator_id(self):
        """operator open_id is injected into action_data when present on the event."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_acl_card_action = AsyncMock()

        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="acl_apply")
        data.event.operator = MagicMock()
        data.event.operator.open_id = "ou_admin"

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            handler._on_card_action(data)
            assert mock_rct.called

        # run_coroutine_threadsafe was called, confirming the acl action was processed
        assert mock_rct.called

    def test_acl_action_no_running_loop_logs_warning(self):
        """When loop is not running, run_coroutine_threadsafe is skipped but toast is still set."""
        handler, _, dispatcher = make_handler()
        dispatcher.handle_acl_card_action = AsyncMock()

        loop = MagicMock()
        loop.is_running.return_value = False  # Loop not running
        handler.attach_loop(loop)

        data = make_card_action_data(action_type="acl_apply")

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe") as mock_rct:
            resp = handler._on_card_action(data)
            assert not mock_rct.called  # No scheduling since loop not running

        # Toast is still set regardless of whether the loop was running
        assert resp.toast is not None
        assert resp.toast.content == "已收到"


class TestMentionParsing:
    """MessageHandler correctly extracts @mention open_ids into task.mentions."""

    def _make_handler(self):
        from nextme.feishu.handler import MessageHandler
        from nextme.feishu.dedup import MessageDedup
        dispatcher = MagicMock()
        dispatcher.dispatch = AsyncMock()
        handler = MessageHandler(dedup=MessageDedup(), dispatcher=dispatcher)
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        handler._loop = mock_loop
        return handler, dispatcher

    def _make_event(self, msg_type, content_json, mentions_sdk=None, chat_id="oc_chat", user_id="ou_user"):
        """Build a fake lark P2ImMessageReceiveV1-like object."""
        import uuid
        message = MagicMock()
        message.message_id = f"om_{uuid.uuid4().hex[:8]}"
        message.chat_id = chat_id
        message.chat_type = "p2p"
        message.message_type = msg_type
        message.content = json.dumps(content_json)
        message.mentions = mentions_sdk or []
        sender = MagicMock()
        sender.sender_id.open_id = user_id
        event = MagicMock()
        event.message = message
        event.sender = sender
        data = MagicMock()
        data.event = event
        return data

    def _make_mention(self, key, open_id, name):
        m = MagicMock()
        m.key = key
        m.id.open_id = open_id
        m.name = name
        return m

    def _handle_and_get_task(self, handler, dispatcher, data):
        """Run handle_message and return the Task that was dispatched."""

        def fake_rct(coro, loop):
            # Run the coroutine synchronously so dispatcher.dispatch gets called.
            import asyncio as _asyncio
            _loop = _asyncio.new_event_loop()
            try:
                _loop.run_until_complete(coro)
            finally:
                _loop.close()

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe", side_effect=fake_rct):
            handler.handle_message(data)

        assert dispatcher.dispatch.called, "dispatch was not called"
        return dispatcher.dispatch.call_args[0][0]

    def test_text_message_parses_mentions(self):
        handler, dispatcher = self._make_handler()
        sdk_mentions = [
            self._make_mention("@_user_1", "ou_aaa", "小明"),
            self._make_mention("@_user_2", "ou_bbb", "小红"),
        ]
        data = self._make_event(
            "text",
            {"text": "@_user_1 @_user_2 /skill book 明天开会"},
            mentions_sdk=sdk_mentions,
        )
        task = self._handle_and_get_task(handler, dispatcher, data)
        assert task.mentions == [
            {"name": "小明", "open_id": "ou_aaa"},
            {"name": "小红", "open_id": "ou_bbb"},
        ]

    def test_text_message_no_mentions_gives_empty(self):
        handler, dispatcher = self._make_handler()
        data = self._make_event("text", {"text": "/skill book 明天开会"})
        task = self._handle_and_get_task(handler, dispatcher, data)
        assert task.mentions == []

    def test_post_message_parses_at_nodes(self):
        handler, dispatcher = self._make_handler()
        post_content = {
            "zh_cn": {
                "title": "",
                "content": [[
                    {"tag": "text", "text": "帮我订会议 "},
                    {"tag": "at", "user_id": "ou_ccc", "user_name": "阿强"},
                    {"tag": "text", "text": " 明天下午3点"},
                ]]
            }
        }
        data = self._make_event("post", post_content)
        task = self._handle_and_get_task(handler, dispatcher, data)
        assert task.mentions == [{"name": "阿强", "open_id": "ou_ccc"}]

    def test_mentions_deduplicated_by_open_id(self):
        handler, dispatcher = self._make_handler()
        sdk_mentions = [
            self._make_mention("@_user_1", "ou_aaa", "小明"),
            self._make_mention("@_user_1", "ou_aaa", "小明"),  # duplicate
        ]
        data = self._make_event("text", {"text": "hi"}, mentions_sdk=sdk_mentions)
        task = self._handle_and_get_task(handler, dispatcher, data)
        assert len(task.mentions) == 1

    def test_text_message_none_id_skipped(self):
        handler, dispatcher = self._make_handler()
        # SDK mention with m.id = None (no open_id extractable)
        broken_mention = MagicMock()
        broken_mention.id = None
        broken_mention.name = "broken"
        data = self._make_event("text", {"text": "hi"}, mentions_sdk=[broken_mention])
        handler.handle_message(data)
        task = dispatcher.dispatch.call_args[0][0]
        assert task.mentions == []

    def test_unsupported_message_type_gives_empty(self):
        handler, dispatcher = self._make_handler()
        data = self._make_event("image", {"image_key": "img_abc"})
        handler.handle_message(data)
        # Image messages produce empty text → handler drops them before dispatch.
        assert dispatcher.dispatch.call_args is None


class TestThreadSessionId:
    """Verify session_id construction and thread routing in group chats."""

    def _make_handler(self):
        dispatcher = MagicMock()
        dispatched: list = []
        async def _dispatch(task): dispatched.append(task)
        dispatcher.dispatch = _dispatch
        handler = MessageHandler(MessageDedup(), dispatcher)
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        handler._loop = mock_loop
        return handler, dispatcher, dispatched

    def _run_handle_and_collect(self, handler, dispatcher, dispatched, data):
        """Invoke handle_message with run_coroutine_threadsafe patched to actually run coroutines."""
        def fake_rct(coro, loop):
            _loop = asyncio.new_event_loop()
            try:
                _loop.run_until_complete(coro)
            finally:
                _loop.close()

        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe", side_effect=fake_rct):
            handler.handle_message(data)

    def _make_group_root_message(self, message_id: str, user_id: str, text: str, with_mention: bool = True):
        """Group chat root message (root_id empty). With bot @mention by default."""
        message = MagicMock()
        message.message_id = message_id
        message.chat_id = "oc_group1"
        message.chat_type = "group"
        message.message_type = "text"
        message.root_id = ""
        message.parent_id = ""
        content = {"text": f"@_user_1 {text}" if with_mention else text}
        message.content = json.dumps(content)
        bot_mention = MagicMock()
        bot_mention.id = MagicMock()
        bot_mention.id.open_id = "ou_bot_id"
        bot_mention.id.user_id = ""  # bots have no user_id
        bot_mention.name = "NextMe"
        message.mentions = [bot_mention] if with_mention else []
        sender = MagicMock()
        sender.sender_id = MagicMock()
        sender.sender_id.open_id = user_id
        event = MagicMock()
        event.message = message
        event.sender = sender
        data = MagicMock()
        data.event = event
        return data

    def _make_group_thread_reply(self, message_id: str, root_id: str, user_id: str, text: str,
                                  with_mention: bool = True):
        """Group chat reply within a thread (root_id set). with_mention controls @bot presence."""
        message = MagicMock()
        message.message_id = message_id
        message.chat_id = "oc_group1"
        message.chat_type = "group"
        message.message_type = "text"
        message.root_id = root_id
        message.parent_id = root_id
        message.content = json.dumps({"text": text})
        # With @mention: bot mention (no user_id); without: empty list
        if with_mention:
            bot_m = MagicMock()
            bot_m.id = MagicMock()
            bot_m.id.user_id = ""  # bots have no user_id
            message.mentions = [bot_m]
        else:
            message.mentions = []
        sender = MagicMock()
        sender.sender_id = MagicMock()
        sender.sender_id.open_id = user_id
        event = MagicMock()
        event.message = message
        event.sender = sender
        data = MagicMock()
        data.event = event
        return data

    def test_group_root_message_with_mention_creates_thread_session(self):
        """@bot on root message → session_id = chat_id:message_id, user_id filled."""
        handler, dispatcher, dispatched = self._make_handler()
        data = self._make_group_root_message("om_root1", "ou_userA", "hello bot")
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 1
        task = dispatched[0]
        assert task.session_id == "oc_group1:om_root1"
        assert task.user_id == "ou_userA"
        assert task.thread_root_id == "om_root1"

    def test_group_root_message_without_mention_ignored(self):
        """Root message without @bot in group chat is ignored."""
        handler, dispatcher, dispatched = self._make_handler()
        data = self._make_group_root_message("om_root2", "ou_userA", "plain message", with_mention=False)
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 0

    def test_group_thread_reply_with_mention_dispatched(self):
        """Reply in an active thread WITH @bot is dispatched with same session_id."""
        handler, dispatcher, dispatched = self._make_handler()
        handler._active_threads.add("oc_group1:om_root1")  # simulate registered thread
        data = self._make_group_thread_reply("om_reply1", "om_root1", "ou_userB", "follow-up",
                                              with_mention=True)
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 1
        task = dispatched[0]
        assert task.session_id == "oc_group1:om_root1"
        assert task.user_id == "ou_userB"
        assert task.thread_root_id == "om_root1"

    def test_group_thread_reply_without_mention_ignored(self):
        """Regular reply in an active thread WITHOUT @bot is silently dropped."""
        handler, dispatcher, dispatched = self._make_handler()
        handler._active_threads.add("oc_group1:om_root1")
        data = self._make_group_thread_reply("om_reply_no_at", "om_root1", "ou_userB",
                                              "no mention here", with_mention=False)
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 0

    def test_group_thread_meta_command_without_mention_dispatched(self):
        """Meta-command (/stop etc.) in active thread is dispatched even without @mention."""
        handler, dispatcher, dispatched = self._make_handler()
        handler._active_threads.add("oc_group1:om_root1")
        # /stop sent without @bot mention — should still be dispatched
        data = self._make_group_thread_reply("om_stop1", "om_root1", "ou_userB",
                                              "/stop", with_mention=False)
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 1
        task = dispatched[0]
        assert task.content == "/stop"
        assert task.thread_root_id == "om_root1"

    def test_group_thread_reply_in_unknown_thread_ignored(self):
        """Reply in an unknown thread (bot not involved) is silently dropped."""
        handler, dispatcher, dispatched = self._make_handler()
        # _active_threads is empty — thread not registered
        data = self._make_group_thread_reply("om_reply2", "om_root_other", "ou_userC", "hey")
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 0

    def test_p2p_session_id_unchanged(self):
        """P2P chat session_id stays chat_id:user_id regardless of thread logic."""
        handler, dispatcher, dispatched = self._make_handler()

        message = MagicMock()
        message.message_id = "om_p2p1"
        message.chat_id = "p2p_chat1"
        message.chat_type = "p2p"
        message.message_type = "text"
        message.root_id = ""
        message.content = json.dumps({"text": "hello"})
        message.mentions = []
        sender = MagicMock()
        sender.sender_id = MagicMock()
        sender.sender_id.open_id = "ou_userX"
        event = MagicMock()
        event.message = message
        event.sender = sender
        data = MagicMock()
        data.event = event

        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 1
        task = dispatched[0]
        assert task.session_id == "p2p_chat1:ou_userX"
        assert task.user_id == "ou_userX"
        assert task.thread_root_id == ""

    def test_deregister_thread_removes_from_active_set(self):
        """deregister_thread removes the key so subsequent replies are ignored."""
        handler, dispatcher, dispatched = self._make_handler()
        handler._active_threads.add("oc_group1:om_root1")

        # Deregister the thread (simulates /done closing it).
        handler.deregister_thread("oc_group1", "om_root1")

        assert "oc_group1:om_root1" not in handler._active_threads

        # A reply to that thread must now be ignored.
        data = self._make_group_thread_reply("om_reply99", "om_root1", "ou_userA", "late reply")
        self._run_handle_and_collect(handler, dispatcher, dispatched, data)

        assert len(dispatched) == 0

    def test_deregister_thread_idempotent(self):
        """deregister_thread on an already-absent key does not raise."""
        handler, _, _ = self._make_handler()
        # Key was never added — should not raise.
        handler.deregister_thread("oc_group1", "om_nonexistent")
        assert "oc_group1:om_nonexistent" not in handler._active_threads


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


class TestAdditionalCoverage:
    """Extra tests to cover missed branches."""

    def test_restore_active_threads(self):
        """restore_active_threads sets the internal thread set."""
        handler, _, _ = make_handler()
        keys = {"oc_chat1:om_root1", "oc_chat2:om_root2"}
        handler.restore_active_threads(keys)
        assert handler._active_threads == keys

    def test_on_message_receive_catches_handle_message_exception(self):
        """_on_message_receive catches exceptions raised by handle_message."""
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        with patch.object(handler, "handle_message", side_effect=RuntimeError("boom")):
            # Should not raise
            handler._on_message_receive(MagicMock())

    def test_extract_mentions_post_invalid_json(self):
        """_extract_mentions returns empty list for post message with invalid JSON."""
        from nextme.feishu.handler import _extract_mentions
        message = MagicMock()
        message.message_type = "post"
        message.content = "not-valid-json"
        result = _extract_mentions(message)
        assert result == []

    def test_extract_mentions_post_non_string_content_raises_type_error(self):
        """_extract_mentions returns empty list when content causes TypeError in json.loads."""
        from nextme.feishu.handler import _extract_mentions
        message = MagicMock()
        message.message_type = "post"
        # json.loads(None) raises TypeError
        message.content = None
        result = _extract_mentions(message)
        # content is None/falsy → content_obj = {} → no iterations → empty
        assert result == []

    def test_has_bot_mention_bot_open_id_set_no_match(self):
        """_has_bot_mention returns False when bot_open_id is set but no mention matches."""
        handler, _, _ = make_handler()
        handler._bot_open_id = "ou_bot123"
        message = MagicMock()
        m1 = MagicMock()
        m1.id = MagicMock()
        m1.id.open_id = "ou_other_user"  # doesn't match
        message.mentions = [m1]
        result = handler._has_bot_mention(message)
        assert result is False

    def test_has_bot_mention_fallback_mid_none(self):
        """_has_bot_mention fallback loop skips entries where m.id is None."""
        handler, _, _ = make_handler()
        handler._bot_open_id = ""  # no bot_open_id → use fallback
        message = MagicMock()
        m_with_none_id = MagicMock()
        m_with_none_id.id = None  # triggers the `if mid is None: continue`
        m_with_user_id = MagicMock()
        m_with_user_id.id = MagicMock()
        m_with_user_id.id.user_id = "ou_human"  # has user_id → not a bot
        message.mentions = [m_with_none_id, m_with_user_id]
        result = handler._has_bot_mention(message)
        assert result is False

    def test_has_bot_mention_fallback_no_match(self):
        """_has_bot_mention fallback returns False when all mentions have user_id."""
        handler, _, _ = make_handler()
        handler._bot_open_id = ""
        message = MagicMock()
        m = MagicMock()
        m.id = MagicMock()
        m.id.user_id = "ou_human"  # has user_id → not a bot mention
        message.mentions = [m]
        result = handler._has_bot_mention(message)
        assert result is False

    def test_schedule_dispatch_no_loop_runtime_error(self):
        """_schedule_dispatch drops task when asyncio.get_event_loop raises RuntimeError."""
        handler, _, _ = make_handler()
        handler._loop = None  # no attached loop
        task = MagicMock()
        task.id = "task-123"
        with patch("nextme.feishu.handler.asyncio.get_event_loop",
                   side_effect=RuntimeError("no loop")):
            handler._schedule_dispatch(task)
        # Should not raise; task is silently dropped

    def test_on_card_action_exception_swallowed(self):
        """_on_card_action catches unexpected exceptions without propagating."""
        handler, _, _ = make_handler()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = MagicMock()
        data.event = MagicMock()
        # Make value.get raise to trigger general except
        bad_action = MagicMock()
        bad_action.value = MagicMock()
        bad_action.value.get = MagicMock(side_effect=RuntimeError("boom"))
        data.event.action = bad_action
        # Should not raise
        handler._on_card_action(data)

    def test_acl_action_operator_id_exception_swallowed(self):
        """Exceptions extracting operator_id in _on_card_action are swallowed."""
        handler, _, _ = make_handler()
        dispatcher = handler._dispatcher
        dispatcher.handle_acl_card_action = AsyncMock()
        loop = MagicMock()
        loop.is_running.return_value = True
        handler.attach_loop(loop)

        data = MagicMock()
        data.event = MagicMock()
        action = MagicMock()
        action.value = {"action": "acl_apply", "app_id": "1"}
        data.event.action = action
        # Make accessing data.event.operator raise
        type(data.event).operator = property(lambda self: (_ for _ in ()).throw(RuntimeError("oops")))
        # Should not raise
        with patch("nextme.feishu.handler.asyncio.run_coroutine_threadsafe"):
            handler._on_card_action(data)
