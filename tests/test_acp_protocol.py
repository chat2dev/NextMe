"""Tests for nextme.acp.protocol — message types and serialization."""

import json
import pytest

from nextme.acp.protocol import (
    CancelMsg,
    LoadSessionMsg,
    NewSessionMsg,
    PermissionResponseMsg,
    PromptMsg,
    parse_acp_message,
    serialize_msg,
)


# ---------------------------------------------------------------------------
# Message type tests
# ---------------------------------------------------------------------------


class TestNewSessionMsg:
    def test_type_field(self):
        msg = NewSessionMsg(session_id="s1", cwd="/tmp")
        assert msg.type == "new_session"

    def test_session_id_field(self):
        msg = NewSessionMsg(session_id="abc", cwd="/home")
        assert msg.session_id == "abc"

    def test_cwd_field(self):
        msg = NewSessionMsg(session_id="abc", cwd="/home/user")
        assert msg.cwd == "/home/user"

    def test_defaults(self):
        msg = NewSessionMsg()
        assert msg.session_id == ""
        assert msg.cwd == ""
        assert msg.type == "new_session"


class TestLoadSessionMsg:
    def test_type_field(self):
        msg = LoadSessionMsg(session_id="s2")
        assert msg.type == "load_session"

    def test_session_id_field(self):
        msg = LoadSessionMsg(session_id="xyz")
        assert msg.session_id == "xyz"

    def test_default(self):
        msg = LoadSessionMsg()
        assert msg.session_id == ""


class TestPromptMsg:
    def test_type_field(self):
        msg = PromptMsg(content="hello")
        assert msg.type == "prompt"

    def test_content_field(self):
        msg = PromptMsg(session_id="s1", content="What is 2+2?")
        assert msg.content == "What is 2+2?"

    def test_default(self):
        msg = PromptMsg()
        assert msg.content == ""
        assert msg.session_id == ""


class TestPermissionResponseMsg:
    def test_type_field(self):
        msg = PermissionResponseMsg(request_id="req1", choice=2)
        assert msg.type == "permission_response"

    def test_request_id_field(self):
        msg = PermissionResponseMsg(request_id="req-42", choice=1)
        assert msg.request_id == "req-42"

    def test_choice_field(self):
        msg = PermissionResponseMsg(request_id="req1", choice=3)
        assert msg.choice == 3

    def test_default_choice(self):
        msg = PermissionResponseMsg()
        assert msg.choice == 1


class TestCancelMsg:
    def test_type_field(self):
        msg = CancelMsg(session_id="s1")
        assert msg.type == "cancel"

    def test_session_id_field(self):
        msg = CancelMsg(session_id="my-session")
        assert msg.session_id == "my-session"

    def test_default(self):
        msg = CancelMsg()
        assert msg.session_id == ""


# ---------------------------------------------------------------------------
# serialize_msg tests
# ---------------------------------------------------------------------------


class TestSerializeMsg:
    def test_returns_json_string(self):
        msg = PromptMsg(session_id="s1", content="hello")
        result = serialize_msg(msg)
        assert isinstance(result, str)
        # Must be valid JSON
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_type_is_first_key(self):
        msg = NewSessionMsg(session_id="s1", cwd="/tmp")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert list(d.keys())[0] == "type"

    def test_type_is_first_key_for_prompt(self):
        msg = PromptMsg(session_id="s1", content="test content")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert list(d.keys())[0] == "type"

    def test_type_is_first_key_for_permission_response(self):
        msg = PermissionResponseMsg(request_id="r1", choice=2)
        result = serialize_msg(msg)
        d = json.loads(result)
        assert list(d.keys())[0] == "type"

    def test_raises_type_error_for_non_dataclass(self):
        with pytest.raises(TypeError):
            serialize_msg({"type": "new_session"})

    def test_raises_type_error_for_plain_string(self):
        with pytest.raises(TypeError):
            serialize_msg("new_session")

    def test_raises_type_error_for_dataclass_class_not_instance(self):
        with pytest.raises(TypeError):
            serialize_msg(PromptMsg)

    def test_raises_type_error_for_none(self):
        with pytest.raises(TypeError):
            serialize_msg(None)

    def test_no_trailing_newline(self):
        msg = PromptMsg(session_id="s1", content="hello")
        result = serialize_msg(msg)
        assert not result.endswith("\n")

    def test_includes_all_fields_new_session(self):
        msg = NewSessionMsg(session_id="sess-1", cwd="/projects/foo")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert d["type"] == "new_session"
        assert d["session_id"] == "sess-1"
        assert d["cwd"] == "/projects/foo"

    def test_includes_all_fields_prompt(self):
        msg = PromptMsg(session_id="s2", content="Do something")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert d["type"] == "prompt"
        assert d["session_id"] == "s2"
        assert d["content"] == "Do something"

    def test_includes_all_fields_permission_response(self):
        msg = PermissionResponseMsg(request_id="req-99", choice=2)
        result = serialize_msg(msg)
        d = json.loads(result)
        assert d["type"] == "permission_response"
        assert d["request_id"] == "req-99"
        assert d["choice"] == 2

    def test_includes_all_fields_cancel(self):
        msg = CancelMsg(session_id="s3")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert d["type"] == "cancel"
        assert d["session_id"] == "s3"

    def test_unicode_content_preserved(self):
        msg = PromptMsg(session_id="s1", content="你好世界")
        result = serialize_msg(msg)
        d = json.loads(result)
        assert d["content"] == "你好世界"


# ---------------------------------------------------------------------------
# parse_acp_message tests
# ---------------------------------------------------------------------------


class TestParseAcpMessage:
    def test_parses_valid_json_dict(self):
        line = '{"type": "ready"}'
        result = parse_acp_message(line)
        assert result == {"type": "ready"}

    def test_parses_dict_with_multiple_fields(self):
        line = '{"type": "done", "content": "hi there"}'
        result = parse_acp_message(line)
        assert result["type"] == "done"
        assert result["content"] == "hi there"

    def test_raises_value_error_for_empty_string(self):
        with pytest.raises(ValueError):
            parse_acp_message("")

    def test_raises_value_error_for_whitespace_only(self):
        with pytest.raises(ValueError):
            parse_acp_message("   \n  ")

    def test_raises_value_error_for_invalid_json(self):
        with pytest.raises(ValueError):
            parse_acp_message("not-json-at-all")

    def test_raises_value_error_for_json_array(self):
        with pytest.raises(ValueError):
            parse_acp_message('[{"type": "ready"}]')

    def test_raises_value_error_for_json_string(self):
        with pytest.raises(ValueError):
            parse_acp_message('"just a string"')

    def test_raises_value_error_for_json_number(self):
        with pytest.raises(ValueError):
            parse_acp_message("42")

    def test_raises_value_error_for_json_null(self):
        with pytest.raises(ValueError):
            parse_acp_message("null")

    def test_strips_trailing_newline(self):
        line = '{"type": "ready"}\n'
        result = parse_acp_message(line)
        assert result == {"type": "ready"}

    def test_strips_trailing_whitespace(self):
        line = '{"type": "ready"}   '
        result = parse_acp_message(line)
        assert result == {"type": "ready"}

    def test_strips_leading_and_trailing_whitespace(self):
        line = '  {"type": "done"}  \n'
        result = parse_acp_message(line)
        assert result == {"type": "done"}

    def test_returns_dict_type(self):
        line = '{"type": "content_delta", "delta": "hello"}'
        result = parse_acp_message(line)
        assert isinstance(result, dict)

    def test_permission_request_message(self):
        line = '{"type": "permission_request", "request_id": "r1", "description": "Allow?"}'
        result = parse_acp_message(line)
        assert result["type"] == "permission_request"
        assert result["request_id"] == "r1"
        assert result["description"] == "Allow?"
