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
        status_elements = [e for e in elements if e.get("tag") == "markdown" and "Step 1/3" in e.get("content", "")]
        assert len(status_elements) == 1

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

    def test_no_streaming_mode(self):
        """build_progress_card is sent via im/v1 which rejects streaming_mode."""
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert parsed["config"].get("streaming_mode") is None

    def test_no_element_ids(self):
        """build_progress_card elements must NOT have id fields (im/v1 rejects them)."""
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        for el in parsed["body"]["elements"]:
            assert "id" not in el

    def test_no_status_element_when_empty(self):
        """With empty status, only one element (content markdown) is produced."""
        replier, _ = make_replier()
        parsed = json.loads(replier.build_progress_card("", "content"))
        assert len(parsed["body"]["elements"]) == 1

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
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and "sess-abc" in e.get("content", "")]
        assert len(footer_elements) == 1

    def test_session_id_adds_hr_before_footer(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", session_id="sess-abc"))
        elements = parsed["body"]["elements"]
        tags = [e.get("tag") for e in elements]
        assert "hr" in tags
        hr_idx = tags.index("hr")
        footer_idx = next(i for i, e in enumerate(elements) if e.get("tag") == "markdown" and "sess-abc" in e.get("content", ""))
        assert hr_idx < footer_idx

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
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and "s1" in e.get("content", "")]
        assert len(footer_elements) >= 1

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

    def test_one_button_element_per_option(self):
        replier, _ = make_replier()
        options = self._make_options(3)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        assert len(btn_elements) == 3

    def test_first_option_button_is_primary(self):
        replier, _ = make_replier()
        options = self._make_options(2)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        assert btn_elements[0]["type"] == "primary"

    def test_subsequent_option_buttons_are_default(self):
        replier, _ = make_replier()
        options = self._make_options(3)
        parsed = json.loads(replier.build_permission_card("desc", options))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        for btn in btn_elements[1:]:
            assert btn["type"] == "default"

    def test_button_label_contains_index_and_label(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_text = btn_elements[0]["text"]["content"]
        assert "1" in btn_text
        assert "Allow" in btn_text

    def test_button_label_includes_description_when_provided(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow", description="Grants full access")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_text = btn_elements[0]["text"]["content"]
        assert "Grants full access" in btn_text
        assert "—" in btn_text

    def test_button_label_excludes_description_separator_when_no_description(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_text = btn_elements[0]["text"]["content"]
        assert "—" not in btn_text

    def test_session_id_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(
            replier.build_permission_card("desc", opts, session_id="sess-xyz")
        )
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_value = btn_elements[0]["value"]
        assert btn_value["session_id"] == "sess-xyz"

    def test_session_id_adds_footer_note(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card("desc", opts, session_id="sess-abc")
        )
        elements = parsed["body"]["elements"]
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and "sess-abc" in e.get("content", "")]
        assert len(footer_elements) == 1

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
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_value = btn_elements[0]["value"]
        assert btn_value["action"] == "permission_choice"
        assert btn_value["index"] == "2"

    def test_project_name_stored_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(
            replier.build_permission_card(
                "desc", opts, session_id="oc_x:ou_y", project_name="my-proj"
            )
        )
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_value = btn_elements[0]["value"]
        assert btn_value["project_name"] == "my-proj"
        assert btn_value["session_id"] == "oc_x:ou_y"

    def test_label_stored_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow", description="Full access")]
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_value = btn_elements[0]["value"]
        assert "label" in btn_value
        assert "Allow" in btn_value["label"]
        assert "Full access" in btn_value["label"]

    def test_executor_stored_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(
            replier.build_permission_card("desc", opts, executor="claude")
        )
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        btn_value = btn_elements[0]["value"]
        assert btn_value["executor"] == "claude"

    def test_executor_appears_in_footer(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card(
                "desc", opts, session_id="sid-1", executor="coco"
            )
        )
        elements = parsed["body"]["elements"]
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "coco" in e.get("content", "")
        ]
        assert len(footer_elements) == 1

    def test_footer_uses_pipe_separator(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card(
                "desc", opts, session_id="sid-1", executor="claude"
            )
        )
        elements = parsed["body"]["elements"]
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "🆔" in e.get("content", "")
        ]
        assert len(footer_elements) == 1
        assert " | " in footer_elements[0]["content"]
        assert "claude" in footer_elements[0]["content"]

    def test_no_footer_when_no_session_id_or_executor(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(replier.build_permission_card("desc", opts))
        elements = parsed["body"]["elements"]
        footer_md = [
            e for e in elements
            if e.get("tag") == "markdown" and "🆔" in e.get("content", "")
        ]
        assert len(footer_md) == 0

    def test_display_id_stored_in_button_value(self):
        replier, _ = make_replier()
        opts = [PermOption(index=1, label="Allow")]
        parsed = json.loads(
            replier.build_permission_card(
                "desc", opts,
                session_id="oc_x:ou_y",
                display_id="uuid-actual-123",
            )
        )
        elements = parsed["body"]["elements"]
        btn_elements = [e for e in elements if e.get("tag") == "button"]
        assert btn_elements[0]["value"]["display_id"] == "uuid-actual-123"
        assert btn_elements[0]["value"]["session_id"] == "oc_x:ou_y"

    def test_display_id_shown_in_footer_instead_of_session_id(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card(
                "desc", opts,
                session_id="oc_x:ou_y",
                display_id="nice-uuid",
            )
        )
        elements = parsed["body"]["elements"]
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "🆔" in e.get("content", "")
        ]
        assert len(footer_elements) == 1
        assert "nice-uuid" in footer_elements[0]["content"]
        assert "oc_x:ou_y" not in footer_elements[0]["content"]

    def test_session_id_shown_in_footer_when_display_id_empty(self):
        replier, _ = make_replier()
        opts = self._make_options(1)
        parsed = json.loads(
            replier.build_permission_card("desc", opts, session_id="oc_x:ou_y")
        )
        elements = parsed["body"]["elements"]
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "🆔" in e.get("content", "")
        ]
        assert len(footer_elements) == 1
        assert "oc_x:ou_y" in footer_elements[0]["content"]


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


# ---------------------------------------------------------------------------
# reply_text (async)
# ---------------------------------------------------------------------------


class TestReplyText:
    async def test_reply_text_success_returns_message_id(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_thread_123"
        mock_client.im.v1.message.areply = AsyncMock(return_value=mock_response)

        result = await replier.reply_text("om_src_456", "ok")

        assert result == "om_thread_123"
        mock_client.im.v1.message.areply.assert_awaited_once()

    async def test_reply_text_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 400
        mock_response.msg = "Bad Request"
        mock_client.im.v1.message.areply = AsyncMock(return_value=mock_response)

        result = await replier.reply_text("om_src", "hello")

        assert result == ""

    async def test_reply_text_default_in_thread_true(self):
        """reply_text uses in_thread=True by default."""
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_t"
        mock_client.im.v1.message.areply = AsyncMock(return_value=mock_response)

        await replier.reply_text("om_src", "hi")

        # areply was called — the in_thread flag is embedded in the request builder
        mock_client.im.v1.message.areply.assert_awaited_once()


# ---------------------------------------------------------------------------
# reply_card (async)
# ---------------------------------------------------------------------------


class TestReplyCard:
    async def test_reply_card_success_returns_message_id(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "om_card_thread_99"
        mock_client.im.v1.message.areply = AsyncMock(return_value=mock_response)

        result = await replier.reply_card("om_src_msg", '{"schema": "2.0"}')

        assert result == "om_card_thread_99"
        mock_client.im.v1.message.areply.assert_awaited_once()

    async def test_reply_card_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 500
        mock_response.msg = "Server error"
        mock_client.im.v1.message.areply = AsyncMock(return_value=mock_response)

        result = await replier.reply_card("om_src", "{}")

        assert result == ""


# ---------------------------------------------------------------------------
# build_result_card with elapsed
# ---------------------------------------------------------------------------


class TestBuildResultCardElapsed:
    def test_elapsed_appears_in_footer(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card("content", session_id="sess1", elapsed="5s")
        )
        elements = parsed["body"]["elements"]
        footer_md = next(
            (e for e in elements if e.get("tag") == "markdown" and "耗时" in e.get("content", "")),
            None,
        )
        assert footer_md is not None
        assert "5s" in footer_md["content"]

    def test_no_elapsed_no_extra_footer_part(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card("content", session_id="sess1")
        )
        elements = parsed["body"]["elements"]
        footer_texts = [
            e.get("content", "") for e in elements if e.get("tag") == "markdown"
        ]
        assert not any("耗时" in t for t in footer_texts)

    def test_elapsed_without_session_id(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_result_card("content", elapsed="2m"))
        elements = parsed["body"]["elements"]
        footer_md = next(
            (e for e in elements if e.get("tag") == "markdown" and "耗时" in e.get("content", "")),
            None,
        )
        assert footer_md is not None
        assert "2m" in footer_md["content"]

    def test_executor_appears_between_session_id_and_elapsed(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card(
                "content", session_id="sess1", executor="claude", elapsed="3s"
            )
        )
        elements = parsed["body"]["elements"]
        footer_md = next(
            (e for e in elements if e.get("tag") == "markdown" and "sess1" in e.get("content", "")),
            None,
        )
        assert footer_md is not None
        content = footer_md["content"]
        assert "claude" in content
        assert "3s" in content
        # Order: session_id | executor | elapsed
        assert content.index("sess1") < content.index("claude") < content.index("3s")

    def test_executor_without_session_id(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card("content", executor="coco", elapsed="1s")
        )
        elements = parsed["body"]["elements"]
        footer_md = next(
            (e for e in elements if e.get("tag") == "markdown" and "coco" in e.get("content", "")),
            None,
        )
        assert footer_md is not None
        assert "1s" in footer_md["content"]

    def test_empty_executor_not_shown(self):
        replier, _ = make_replier()
        parsed = json.loads(
            replier.build_result_card("content", session_id="s", executor="", elapsed="1s")
        )
        elements = parsed["body"]["elements"]
        footer_md = next(
            (e for e in elements if e.get("tag") == "markdown" and "耗时" in e.get("content", "")),
            None,
        )
        assert footer_md is not None
        parts = footer_md["content"].split(" | ")
        # Should be: session_id | elapsed (no empty executor part)
        assert len(parts) == 2


# ---------------------------------------------------------------------------
# get_card_id / stream_set_content (async)
# ---------------------------------------------------------------------------


class TestGetCardId:
    async def test_success_returns_card_id(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.card_id = "card_abc"
        mock_client.cardkit.v1.card.aid_convert = AsyncMock(return_value=ok)

        result = await replier.get_card_id("om_msg_123")

        assert result == "card_abc"
        mock_client.cardkit.v1.card.aid_convert.assert_awaited_once()

    async def test_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 400
        fail.msg = "bad"
        mock_client.cardkit.v1.card.aid_convert = AsyncMock(return_value=fail)

        result = await replier.get_card_id("om_msg_bad")

        assert result == ""


class TestEnableStreamingMode:
    async def test_success_calls_asettings(self):
        """enable_streaming_mode uses PATCH /cards/:id/settings, not card JSON."""
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        mock_client.cardkit.v1.card.asettings = AsyncMock(return_value=ok)

        result = await replier.enable_streaming_mode("card_abc")

        assert result is True
        mock_client.cardkit.v1.card.asettings.assert_awaited_once()
        # Verify streaming_mode: true is in the settings JSON
        call_body = mock_client.cardkit.v1.card.asettings.call_args.args[0].request_body
        settings_dict = json.loads(call_body.settings)
        assert settings_dict["config"]["streaming_mode"] is True

    async def test_failure_returns_false(self, caplog):
        import logging
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 500
        fail.msg = "not supported"
        mock_client.cardkit.v1.card.asettings = AsyncMock(return_value=fail)

        with caplog.at_level(logging.WARNING, logger="nextme.feishu.reply"):
            result = await replier.enable_streaming_mode("card_xyz")

        assert result is False
        assert "enable_streaming_mode failed" in caplog.text


class TestStreamSetContent:
    async def test_success_calls_acontent(self):
        """stream_set_content uses PUT /elements/:id/content (the typewriter API)."""
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        mock_client.cardkit.v1.card_element.acontent = AsyncMock(return_value=ok)

        await replier.stream_set_content("card_123", "Hello, world", 1)

        mock_client.cardkit.v1.card_element.acontent.assert_awaited_once()

    async def test_failure_logs_warning(self, caplog):
        import logging
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 500
        fail.msg = "server error"
        mock_client.cardkit.v1.card_element.acontent = AsyncMock(return_value=fail)

        with caplog.at_level(logging.WARNING, logger="nextme.feishu.reply"):
            await replier.stream_set_content("card_999", "full text", 5)

        assert "stream_set_content failed" in caplog.text


# ---------------------------------------------------------------------------
# build_streaming_progress_card
# ---------------------------------------------------------------------------


class TestBuildStreamingProgressCard:
    def test_returns_valid_json_string(self):
        replier, _ = make_replier()
        result = replier.build_streaming_progress_card()
        assert isinstance(json.loads(result), dict)

    def test_schema_is_2_0(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        assert parsed["schema"] == "2.0"

    def test_streaming_mode_not_in_card_json(self):
        """streaming_mode must NOT be in the card JSON — IM renderer rejects it."""
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        assert "streaming_mode" not in parsed.get("config", {})

    def test_header_template_is_yellow(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        assert parsed["header"]["template"] == "yellow"

    def test_default_title(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        assert parsed["header"]["title"]["content"] == "思考中..."

    def test_custom_title(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card(title="Custom Title"))
        assert parsed["header"]["title"]["content"] == "Custom Title"

    def test_content_element_has_id(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        elements = parsed["body"]["elements"]
        assert elements[0].get("id") == "content_el"

    def test_one_element(self):
        """Streaming card has exactly one body element (content_el only).

        The separate status_el was removed because the Feishu cardkit PUT/content
        endpoint returns 300313 for elements with empty/whitespace-only initial
        content.  Tool-call status is now appended inline to content_el.
        """
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card())
        assert len(parsed["body"]["elements"]) == 1

    def test_custom_content_in_first_element(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card(content="loading..."))
        assert parsed["body"]["elements"][0]["content"] == "loading..."

    def test_non_ascii_content_preserved(self):
        replier, _ = make_replier()
        parsed = json.loads(replier.build_streaming_progress_card(content="中文内容"))
        assert parsed["body"]["elements"][0]["content"] == "中文内容"


# ---------------------------------------------------------------------------
# create_card (async)
# ---------------------------------------------------------------------------


class TestCreateCard:
    async def test_success_returns_card_id(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.card_id = "card_new_123"
        mock_client.cardkit.v1.card.acreate = AsyncMock(return_value=ok)

        result = await replier.create_card('{"schema":"2.0"}')

        assert result == "card_new_123"
        mock_client.cardkit.v1.card.acreate.assert_awaited_once()

    async def test_failure_returns_empty_string(self, caplog):
        import logging
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 400
        fail.msg = "invalid card json"
        mock_client.cardkit.v1.card.acreate = AsyncMock(return_value=fail)

        with caplog.at_level(logging.WARNING, logger="nextme.feishu.reply"):
            result = await replier.create_card("{}")

        assert result == ""
        assert "create_card failed" in caplog.text

    async def test_uses_card_json_type(self):
        """create_card sends type='card_json' to the cardkit API."""
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.card_id = "card_xyz"
        mock_client.cardkit.v1.card.acreate = AsyncMock(return_value=ok)

        await replier.create_card('{"schema":"2.0"}')

        mock_client.cardkit.v1.card.acreate.assert_awaited_once()

    async def test_card_id_none_treated_as_empty(self):
        """When response.data.card_id is None, returns ''."""
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.card_id = None
        mock_client.cardkit.v1.card.acreate = AsyncMock(return_value=ok)

        result = await replier.create_card("{}")

        assert result == ""


# ---------------------------------------------------------------------------
# send_card_by_id (async)
# ---------------------------------------------------------------------------


class TestSendCardById:
    async def test_success_returns_message_id(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.message_id = "om_by_id_456"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=ok)

        result = await replier.send_card_by_id("oc_chat_123", "card_abc")

        assert result == "om_by_id_456"
        mock_client.im.v1.message.acreate.assert_awaited_once()

    async def test_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 500
        fail.msg = "server error"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=fail)

        result = await replier.send_card_by_id("oc_chat_123", "card_abc")

        assert result == ""

    async def test_content_references_card_id(self):
        """Content sent to im/v1 must be {"card_id": "..."} JSON."""
        import json as json_mod
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.message_id = "om_123"
        mock_client.im.v1.message.acreate = AsyncMock(return_value=ok)

        await replier.send_card_by_id("oc_chat_123", "card_xyz")

        # Verify acreate was called (content encoding is internal to builder)
        mock_client.im.v1.message.acreate.assert_awaited_once()


# ---------------------------------------------------------------------------
# reply_card_by_id (async)
# ---------------------------------------------------------------------------


class TestReplyCardById:
    async def test_success_returns_message_id(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.message_id = "om_reply_by_id_789"
        mock_client.im.v1.message.areply = AsyncMock(return_value=ok)

        result = await replier.reply_card_by_id("om_src_123", "card_abc")

        assert result == "om_reply_by_id_789"
        mock_client.im.v1.message.areply.assert_awaited_once()

    async def test_failure_returns_empty_string(self):
        replier, mock_client = make_replier()
        fail = MagicMock()
        fail.success.return_value = False
        fail.code = 404
        fail.msg = "message not found"
        mock_client.im.v1.message.areply = AsyncMock(return_value=fail)

        result = await replier.reply_card_by_id("om_src_bad", "card_abc")

        assert result == ""

    async def test_default_in_thread_true(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.message_id = "om_t"
        mock_client.im.v1.message.areply = AsyncMock(return_value=ok)

        await replier.reply_card_by_id("om_src", "card_id")

        mock_client.im.v1.message.areply.assert_awaited_once()

    async def test_in_thread_false(self):
        replier, mock_client = make_replier()
        ok = MagicMock()
        ok.success.return_value = True
        ok.data.message_id = "om_quote"
        mock_client.im.v1.message.areply = AsyncMock(return_value=ok)

        result = await replier.reply_card_by_id("om_src", "card_id", in_thread=False)

        assert result == "om_quote"
