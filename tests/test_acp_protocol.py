"""Tests for nextme.acp.protocol — JSON-RPC 2.0 helpers."""

import json
import pytest

from nextme.acp.protocol import (
    InboundPermissionRequest,
    PermissionOption,
    cancel_params,
    classify,
    initialize_params,
    load_session_params,
    make_error_response,
    make_request,
    make_response,
    new_session_params,
    parse_message,
    parse_permission_request,
    permission_cancel_result,
    permission_response_result,
    prompt_params,
)


# ---------------------------------------------------------------------------
# make_request
# ---------------------------------------------------------------------------


class TestMakeRequest:
    def test_returns_valid_json(self):
        line = make_request("initialize", {"protocolVersion": 1}, 1)
        d = json.loads(line)
        assert isinstance(d, dict)

    def test_jsonrpc_field(self):
        d = json.loads(make_request("initialize", {}, 1))
        assert d["jsonrpc"] == "2.0"

    def test_id_field(self):
        d = json.loads(make_request("initialize", {}, 42))
        assert d["id"] == 42

    def test_method_field(self):
        d = json.loads(make_request("session/new", {}, 1))
        assert d["method"] == "session/new"

    def test_params_included(self):
        d = json.loads(make_request("session/new", {"cwd": "/tmp"}, 1))
        assert d["params"]["cwd"] == "/tmp"

    def test_no_trailing_newline(self):
        line = make_request("initialize", {}, 1)
        assert not line.endswith("\n")


# ---------------------------------------------------------------------------
# make_response / make_error_response
# ---------------------------------------------------------------------------


class TestMakeResponse:
    def test_success_response(self):
        d = json.loads(make_response(3, {"sessionId": "abc"}))
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 3
        assert d["result"]["sessionId"] == "abc"

    def test_error_response(self):
        d = json.loads(make_error_response(5, -32600, "bad request"))
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 5
        assert d["error"]["code"] == -32600
        assert d["error"]["message"] == "bad request"


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_parses_valid_json_dict(self):
        d = parse_message('{"jsonrpc": "2.0", "id": 1, "result": {}}')
        assert d["jsonrpc"] == "2.0"

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            parse_message("")

    def test_raises_on_whitespace(self):
        with pytest.raises(ValueError):
            parse_message("   \n  ")

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError):
            parse_message("not-json")

    def test_raises_on_json_array(self):
        with pytest.raises(ValueError):
            parse_message("[1, 2, 3]")

    def test_strips_whitespace(self):
        d = parse_message('  {"id": 1}  \n')
        assert d["id"] == 1

    def test_unicode_preserved(self):
        d = parse_message('{"text": "你好"}')
        assert d["text"] == "你好"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    def test_response_with_result(self):
        assert classify({"jsonrpc": "2.0", "id": 1, "result": {}}) == "response"

    def test_response_with_error(self):
        assert classify({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "err"}}) == "response"

    def test_notification(self):
        assert classify({"jsonrpc": "2.0", "method": "session/update", "params": {}}) == "notification"

    def test_server_request(self):
        assert classify({"jsonrpc": "2.0", "id": 3, "method": "session/request_permission", "params": {}}) == "server_request"

    def test_unknown(self):
        assert classify({}) == "unknown"


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


class TestParamBuilders:
    def test_initialize_params(self):
        p = initialize_params()
        assert p["protocolVersion"] == 1
        assert "clientCapabilities" in p

    def test_new_session_params(self):
        p = new_session_params("/my/project")
        assert p["cwd"] == "/my/project"
        assert p["mcpServers"] == []

    def test_load_session_params(self):
        p = load_session_params("sess-123", "/home")
        assert p["sessionId"] == "sess-123"
        assert p["cwd"] == "/home"
        assert p["mcpServers"] == []

    def test_prompt_params(self):
        p = prompt_params("sess-abc", "say hi")
        assert p["sessionId"] == "sess-abc"
        assert p["prompt"] == [{"type": "text", "text": "say hi"}]

    def test_cancel_params(self):
        p = cancel_params("sess-xyz")
        assert p["sessionId"] == "sess-xyz"


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


class TestPermissionHelpers:
    def test_permission_response_result(self):
        r = permission_response_result("allow_once")
        # ACP wire format: tagged-union with "outcome" as string discriminant
        assert r["outcome"]["outcome"] == "selected"
        assert r["outcome"]["optionId"] == "allow_once"

    def test_permission_cancel_result(self):
        r = permission_cancel_result()
        assert r["outcome"]["outcome"] == "cancelled"

    def test_parse_permission_request(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {"title": "Read file"},
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
                ],
            },
        }
        req = parse_permission_request(msg)
        assert req.jsonrpc_id == 7
        assert req.session_id == "s1"
        assert len(req.options) == 2
        assert req.options[0].option_id == "allow_once"
        assert req.options[1].option_id == "reject_once"

    def test_parse_permission_request_empty_options(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/request_permission",
            "params": {"sessionId": "s2", "toolCall": {}, "options": []},
        }
        req = parse_permission_request(msg)
        assert req.options == []
