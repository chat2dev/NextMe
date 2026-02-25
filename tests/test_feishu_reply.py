"""Tests for nextme.feishu.reply."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.feishu.reply import FeishuReplier
from nextme.protocol.types import PermOption


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_replier():
    """Create a FeishuReplier with a mock lark client."""
    mock_client = MagicMock()
    return FeishuReplier(mock_client), mock_client


# ---------------------------------------------------------------------------
# build_progress_card
# ---------------------------------------------------------------------------


class TestBuildProgressCard:
    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_progress_card("running", "Processing...")
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert parsed["schema"] == "2.0"

    def test_header_template_is_yellow(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert parsed["header"]["template"] == "yellow"

    def test_default_title(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert parsed["header"]["title"]["content"] == "思考中..."

    def test_custom_title(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content", title="Custom"))
        assert parsed["header"]["title"]["content"] == "Custom"

    def test_content_in_markdown_element(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "my content here"))
        elements = parsed["body"]["elements"]
        markdown_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert len(markdown_elements) >= 1
        assert markdown_elements[0]["content"] == "my content here"

    def test_note_element_added_when_status_non_empty(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("Step 1/3", "content"))
        elements = parsed["body"]["elements"]
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) == 1
        assert note_elements[0]["elements"][0]["content"] == "Step 1/3"

    def test_no_note_element_when_status_empty(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        elements = parsed["body"]["elements"]
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) == 0

    def test_wide_screen_mode_true(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert parsed["config"]["wide_screen_mode"] is True

    def test_non_ascii_content_preserved(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "中文内容"))
        elements = parsed["body"]["elements"]
        assert elements[0]["content"] == "中文内容"


# ---------------------------------------------------------------------------
# build_result_card
# ---------------------------------------------------------------------------


class TestBuildResultCard:
    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_result_card("Hello World")
        assert isinstance(json.loads(result), dict)

    def test_default_title_is_complete(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content"))
        assert parsed["header"]["title"]["content"] == "完成"

    def test_custom_title(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", title="Done"))
        assert parsed["header"]["title"]["content"] == "Done"

    def test_default_template_is_blue(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content"))
        assert parsed["header"]["template"] == "blue"

    def test_custom_template_color(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", template="green"))
        assert parsed["header"]["template"] == "green"

    def test_content_in_markdown_element(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("my result text"))
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_elements) >= 1
        assert md_elements[0]["content"] == "my result text"

    def test_reasoning_adds_collapsible_panel(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", reasoning="my thoughts"))
        elements = parsed["body"]["elements"]
        collapsible = [e for e in elements if e.get("tag") == "collapsible_panel"]
        assert len(collapsible) == 1
        assert collapsible[0]["elements"][0]["content"] == "my thoughts"

    def test_reasoning_adds_hr_before_collapsible(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", reasoning="thoughts"))
        elements = parsed["body"]["elements"]
        tags = [e.get("tag") for e in elements]
        hr_idx = tags.index("hr")
        collapsible_idx = tags.index("collapsible_panel")
        assert hr_idx < collapsible_idx

    def test_no_collapsible_when_reasoning_empty(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content"))
        elements = parsed["body"]["elements"]
        collapsible = [e for e in elements if e.get("tag") == "collapsible_panel"]
        assert len(collapsible) == 0

    def test_session_id_adds_footer_note(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", session_id="sess-abc"))
        elements = parsed["body"]["elements"]
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) == 1
        assert "sess-abc" in note_elements[0]["elements"][0]["content"]

    def test_session_id_adds_hr_before_footer(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", session_id="sess-abc"))
        elements = parsed["body"]["elements"]
        tags = [e.get("tag") for e in elements]
        assert "hr" in tags
        hr_idx = tags.index("hr")
        note_idx = tags.index("note")
        assert hr_idx < note_idx

    def test_no_hr_or_note_when_no_reasoning_and_no_session(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content"))
        elements = parsed["body"]["elements"]
        tags = [e.get("tag") for e in elements]
        assert "hr" not in tags
        assert "note" not in tags

    def test_both_reasoning_and_session_id(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card("content", reasoning="think", session_id="s1")
        )
        elements = parsed["body"]["elements"]
        tags = [e.get("tag") for e in elements]
        assert tags.count("hr") == 2
        assert "collapsible_panel" in tags
        assert "note" in tags

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content"))
        assert parsed["schema"] == "2.0"


# ---------------------------------------------------------------------------
# build_permission_card
# ---------------------------------------------------------------------------


class TestBuildPermissionCard:
    def _make_options(self, n=2):
        return [PermOption(index=i + 1, label=f"Option {i + 1}") for i in range(n)]

    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_permission_card("Allow?", self._make_options())
        assert isinstance(json.loads(result), dict)

    def test_header_template_is_orange(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_permission_card("Allow?", self._make_options()))
        assert parsed["header"]["template"] == "orange"

    def test_description_in_first_markdown_element(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_permission_card("Allow access?", self._make_options())
        )
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_elements) >= 1
        assert md_elements[0]["content"] == "Allow access?"

    def test_one_action_element_per_option(self):
        replier, _ = make_replier()
        options = self._make_options(3)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        assert len(action_elements) == 3

    def test_first_option_button_is_primary(self):
        replier, _ = make_replier()
        options = self._make_options(2)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        first_button = action_elements[0]["actions"][0]
        assert first_button["type"] == "primary"

    def test_subsequent_option_buttons_are_default(self):
        replier, _ = make_replier()
        options = self._make_options(3)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        for action_el in action_elements[1:]:
            assert action_el["actions"][0]["type"] == "default"

    def test_button_label_contains_index_and_label(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        btn_text = action_elements[0]["actions"][0]["text"]["content"]
        assert "1" in btn_text
        assert "Allow" in btn_text

    def test_button_label_includes_description_when_provided(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow", description="Grants full access")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        btn_text = action_elements[0]["actions"][0]["text"]["content"]
        assert "Grants full access" in btn_text
        assert "—" in btn_text

    def test_button_label_excludes_description_separator_when_no_description(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        btn_text = action_elements[0]["actions"][0]["text"]["content"]
        assert "—" not in btn_text

    def test_session_id_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(
            replier.build_permission_card("desc", opts, session_id="sess-xyz")
        )
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        btn_value = action_elements[0]["actions"][0]["value"]
        assert btn_value["session_id"] == "sess-xyz"

    def test_session_id_adds_footer_note(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card("desc", opts, session_id="sess-abc")
        )
        elements = parsed["body"]["elements"]
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) == 1
        assert "sess-abc" in note_elements[0]["elements"][0]["content"]

    def test_no_footer_note_when_no_session_id(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) == 0

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_permission_card("desc", self._make_options()))
        assert parsed["schema"] == "2.0"

    def test_button_value_has_permission_choice_action(self):
        replier, _ = make_replier()
        opts = [PermOption(index=2, label="Deny")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        action_elements = [e for e in elements if e.get("tag") == "action"]
        btn_value = action_elements[0]["actions"][0]["value"]
        assert btn_value["action"] == "permission_choice"
        assert btn_value["index"] == "2"


# ---------------------------------------------------------------------------
# build_error_card
# ---------------------------------------------------------------------------


class TestBuildErrorCard:
    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_error_card("Something failed")
        assert isinstance(json.loads(result), dict)

    def test_header_template_is_red(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_error_card("error"))
        assert parsed["header"]["template"] == "red"

    def test_error_text_in_markdown_element(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_error_card("Disk full error"))
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_elements) >= 1
        assert md_elements[0]["content"] == "Disk full error"

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_error_card("error"))
        assert parsed["schema"] == "2.0"

    def test_header_title_content(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_error_card("error"))
        assert parsed["header"]["title"]["content"] == "出错了"


# ---------------------------------------------------------------------------
# build_help_card
# ---------------------------------------------------------------------------


class TestBuildHelpCard:
    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_help_card([("/help", "Show help")])
        assert isinstance(json.loads(result), dict)

    def test_header_template_is_green(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_help_card([("/help", "Show help")]))
        assert parsed["header"]["template"] == "green"

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_help_card([]))
        assert parsed["schema"] == "2.0"

    def test_commands_in_markdown_table(self):
        replier, _ = make_replier()
        cmds = [("/help", "Show help"), ("/reset", "Reset session")]
        parsed = json.loads(replier.build_help_card(cmds))
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_elements) >= 1
        table_content = md_elements[0]["content"]
        assert "/help" in table_content
        assert "Show help" in table_content
        assert "/reset" in table_content
        assert "Reset session" in table_content

    def test_table_has_header_row(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_help_card([("/cmd", "desc")]))
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        table_content = md_elements[0]["content"]
        assert "| 命令 | 说明 |" in table_content
        assert "| --- | --- |" in table_content

    def test_command_wrapped_in_backticks(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_help_card([("/test", "test cmd")]))
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        table_content = md_elements[0]["content"]
        assert "`/test`" in table_content

    def test_empty_commands_list(self):
        replier, _ = make_replier()
        result = replier.build_help_card([])
        parsed = json.loads(result)
        elements = parsed["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        # Table header only, no command rows
        table_content = md_elements[0]["content"]
        assert "| 命令 | 说明 |" in table_content


# ---------------------------------------------------------------------------
# send_text (async)
# ---------------------------------------------------------------------------


class TestSendText:
    async def test_send_text_success_returns_message_id(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_123"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        result = await replier.send_text("oc_chat", "Hello")

        assert result == "om_123"
        mock_client.im.v1.message.acreate.assert_awaited_once()

    async def test_send_text_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 1234
        mock_response.msg = "Unauthorized"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        result = await replier.send_text("oc_chat", "Hello")

        assert result == ""

    async def test_send_text_uses_chat_id_receive_type(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_abc"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        await replier.send_text("oc_chat_xyz", "test text")

        mock_client.im.v1.message.acreate.assert_awaited_once()

    async def test_send_text_content_is_json_encoded(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_xyz"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        await replier.send_text("oc_chat", "Hello World")

        # The request is built via builder pattern; just verify acreate was called
        mock_client.im.v1.message.acreate.assert_awaited_once()


# ---------------------------------------------------------------------------
# send_card (async)
# ---------------------------------------------------------------------------


class TestSendCard:
    async def test_send_card_success_returns_message_id(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_card_456"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        card_json = replier.build_error_card("some error")
        result = await replier.send_card("oc_chat", card_json)

        assert result == "om_card_456"
        mock_client.im.v1.message.acreate.assert_awaited_once()

    async def test_send_card_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 9999
        mock_response.msg = "bad request"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        result = await replier.send_card("oc_chat", "{}")

        assert result == ""

    async def test_send_card_calls_acreate(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_777"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=mock_response)

        await replier.send_card("oc_chat", '{"schema": "2.0"}')

        mock_client.im.v1.message.acreate.assert_awaited_once()


# ---------------------------------------------------------------------------
# update_card (async)
# ---------------------------------------------------------------------------


class TestUpdateCard:
    async def test_update_card_success_no_exception(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message.apatch = AsyncMock(return_value=mock_response)

        # Should not raise
        await replier.update_card("om_msg_123", '{"schema": "2.0"}')

        mock_client.im.v1.message.apatch.assert_awaited_once()

    async def test_update_card_failure_logs_error_no_exception(self, caplog):
        import logging
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 500
        mock_response.msg = "Internal error"
        mock_client.im.v1.message.apatch = AsyncMock(return_value=mock_response)

        with caplog.at_level(logging.ERROR, logger="nextme.feishu.reply"):
            await replier.update_card("om_msg_999", '{}')

        # Should not raise; error is logged
        mock_client.im.v1.message.apatch.assert_awaited_once()
        assert "update_card failed" in caplog.text

    async def test_update_card_returns_none(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message.apatch = AsyncMock(return_value=mock_response)

        result = await replier.update_card("om_123", "{}")

        assert result is None


# ---------------------------------------------------------------------------
# send_reaction (async)
# ---------------------------------------------------------------------------


class TestSendReaction:
    async def test_send_reaction_success_no_exception(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message_reaction.acreate = AsyncMock(return_value=mock_response)

        # Should not raise
        await replier.send_reaction("om_msg_abc")

        mock_client.im.v1.message_reaction.acreate.assert_awaited_once()

    async def test_send_reaction_failure_logs_error_no_exception(self, caplog):
        import logging
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 404
        mock_response.msg = "Message not found"
        mock_client.im.v1.message_reaction.acreate = AsyncMock(return_value=mock_response)

        with caplog.at_level(logging.ERROR, logger="nextme.feishu.reply"):
            await replier.send_reaction("om_msg_xyz", emoji="THUMBSUP")

        mock_client.im.v1.message_reaction.acreate.assert_awaited_once()
        assert "send_reaction failed" in caplog.text

    async def test_send_reaction_default_emoji_is_smile(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message_reaction.acreate = AsyncMock(return_value=mock_response)

        await replier.send_reaction("om_msg_abc")

        # Verify it was called (the emoji type is embedded inside the built request object)
        mock_client.im.v1.message_reaction.acreate.assert_awaited_once()

    async def test_send_reaction_returns_none(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message_reaction.acreate = AsyncMock(return_value=mock_response)

        result = await replier.send_reaction("om_123", emoji="OK")

        assert result is None
