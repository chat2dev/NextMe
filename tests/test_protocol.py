"""Tests for nextme.protocol.types."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nextme.protocol.types import (
    PermissionChoice,
    PermissionRequest,
    PermOption,
    ProgressEvent,
    Reply,
    ReplyType,
    Task,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# TaskStatus enum
# ---------------------------------------------------------------------------


class TestTaskStatus:
    def test_idle_value(self):
        assert TaskStatus.IDLE == "idle"

    def test_queued_value(self):
        assert TaskStatus.QUEUED == "queued"

    def test_waiting_lock_value(self):
        assert TaskStatus.WAITING_LOCK == "waiting_lock"

    def test_executing_value(self):
        assert TaskStatus.EXECUTING == "executing"

    def test_waiting_permission_value(self):
        assert TaskStatus.WAITING_PERMISSION == "waiting_permission"

    def test_done_value(self):
        assert TaskStatus.DONE == "done"

    def test_canceled_value(self):
        assert TaskStatus.CANCELED == "canceled"

    def test_all_members(self):
        members = {s.name for s in TaskStatus}
        assert members == {
            "IDLE",
            "QUEUED",
            "WAITING_LOCK",
            "EXECUTING",
            "WAITING_PERMISSION",
            "DONE",
            "CANCELED",
        }

    def test_is_str_subclass(self):
        assert isinstance(TaskStatus.IDLE, str)


# ---------------------------------------------------------------------------
# ReplyType enum
# ---------------------------------------------------------------------------


class TestReplyType:
    def test_markdown_value(self):
        assert ReplyType.MARKDOWN == "markdown"

    def test_card_value(self):
        assert ReplyType.CARD == "card"

    def test_reaction_value(self):
        assert ReplyType.REACTION == "reaction"

    def test_file_value(self):
        assert ReplyType.FILE == "file"

    def test_all_members(self):
        members = {r.name for r in ReplyType}
        assert members == {"MARKDOWN", "CARD", "REACTION", "FILE"}

    def test_is_str_subclass(self):
        assert isinstance(ReplyType.MARKDOWN, str)


# ---------------------------------------------------------------------------
# Reply dataclass
# ---------------------------------------------------------------------------


class TestReply:
    def test_creation_minimal(self):
        r = Reply(type=ReplyType.MARKDOWN, content="hello")
        assert r.type == ReplyType.MARKDOWN
        assert r.content == "hello"

    def test_default_title(self):
        r = Reply(type=ReplyType.CARD, content="body")
        assert r.title == ""

    def test_default_template(self):
        r = Reply(type=ReplyType.CARD, content="body")
        assert r.template == "blue"

    def test_default_reasoning(self):
        r = Reply(type=ReplyType.MARKDOWN, content="x")
        assert r.reasoning == ""

    def test_default_is_intermediate(self):
        r = Reply(type=ReplyType.MARKDOWN, content="x")
        assert r.is_intermediate is False

    def test_default_debug_session_id(self):
        r = Reply(type=ReplyType.MARKDOWN, content="x")
        assert r.debug_session_id == ""

    def test_default_file_path(self):
        r = Reply(type=ReplyType.FILE, content="x")
        assert r.file_path == ""

    def test_creation_all_fields(self):
        r = Reply(
            type=ReplyType.CARD,
            content="body text",
            title="My Title",
            template="red",
            reasoning="some reasoning",
            is_intermediate=True,
            debug_session_id="sess-001",
            file_path="/tmp/out.txt",
        )
        assert r.type == ReplyType.CARD
        assert r.content == "body text"
        assert r.title == "My Title"
        assert r.template == "red"
        assert r.reasoning == "some reasoning"
        assert r.is_intermediate is True
        assert r.debug_session_id == "sess-001"
        assert r.file_path == "/tmp/out.txt"


# ---------------------------------------------------------------------------
# PermOption dataclass
# ---------------------------------------------------------------------------


class TestPermOption:
    def test_creation_minimal(self):
        opt = PermOption(index=1, label="Allow")
        assert opt.index == 1
        assert opt.label == "Allow"
        assert opt.description == ""

    def test_creation_with_description(self):
        opt = PermOption(index=2, label="Deny", description="Deny all access")
        assert opt.description == "Deny all access"


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------


class TestTask:
    def _make_reply_fn(self):
        async def reply_fn(r):
            pass

        return reply_fn

    def test_creation_minimal(self):
        fn = self._make_reply_fn()
        t = Task(id="abc", content="do stuff", session_id="chat1:user1", reply_fn=fn)
        assert t.id == "abc"
        assert t.content == "do stuff"
        assert t.session_id == "chat1:user1"
        assert t.reply_fn is fn

    def test_default_created_at_is_datetime(self):
        fn = self._make_reply_fn()
        before = datetime.now()
        t = Task(id="x", content="y", session_id="s", reply_fn=fn)
        after = datetime.now()
        assert before <= t.created_at <= after

    def test_default_timeout_is_8_hours(self):
        fn = self._make_reply_fn()
        t = Task(id="x", content="y", session_id="s", reply_fn=fn)
        assert t.timeout == timedelta(hours=8)

    def test_default_canceled_is_false(self):
        fn = self._make_reply_fn()
        t = Task(id="x", content="y", session_id="s", reply_fn=fn)
        assert t.canceled is False

    def test_default_was_queued_is_false(self):
        fn = self._make_reply_fn()
        t = Task(id="x", content="y", session_id="s", reply_fn=fn)
        assert t.was_queued is False

    def test_creation_with_overrides(self):
        fn = self._make_reply_fn()
        ts = datetime(2024, 1, 1)
        t = Task(
            id="uuid-1",
            content="message",
            session_id="c:u",
            reply_fn=fn,
            created_at=ts,
            timeout=timedelta(minutes=30),
            canceled=True,
            was_queued=True,
        )
        assert t.created_at == ts
        assert t.timeout == timedelta(minutes=30)
        assert t.canceled is True
        assert t.was_queued is True


# ---------------------------------------------------------------------------
# ProgressEvent dataclass
# ---------------------------------------------------------------------------


class TestProgressEvent:
    def test_creation_minimal(self):
        ev = ProgressEvent(session_id="chat:user", delta="some text")
        assert ev.session_id == "chat:user"
        assert ev.delta == "some text"
        assert ev.tool_name == ""

    def test_creation_with_tool_name(self):
        ev = ProgressEvent(session_id="s", delta="d", tool_name="bash")
        assert ev.tool_name == "bash"


# ---------------------------------------------------------------------------
# PermissionRequest dataclass
# ---------------------------------------------------------------------------


class TestPermissionRequest:
    def test_creation(self):
        opts = [PermOption(index=1, label="Yes"), PermOption(index=2, label="No")]
        req = PermissionRequest(
            session_id="s",
            request_id="req-1",
            description="Allow file write?",
            options=opts,
        )
        assert req.session_id == "s"
        assert req.request_id == "req-1"
        assert req.description == "Allow file write?"
        assert len(req.options) == 2
        assert req.options[0].label == "Yes"
        assert req.options[1].label == "No"


# ---------------------------------------------------------------------------
# PermissionChoice dataclass
# ---------------------------------------------------------------------------


class TestPermissionChoice:
    def test_creation_minimal(self):
        choice = PermissionChoice(request_id="req-1", option_index=1)
        assert choice.request_id == "req-1"
        assert choice.option_index == 1
        assert choice.option_label == ""

    def test_creation_with_label(self):
        choice = PermissionChoice(request_id="req-2", option_index=2, option_label="Deny")
        assert choice.option_label == "Deny"
