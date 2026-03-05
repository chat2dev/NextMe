"""Tests for nextme.protocol.types."""

from unittest.mock import AsyncMock

from nextme.protocol.types import Task


def test_task_has_empty_mentions_by_default():
    task = Task(
        id="1", content="hi", session_id="s",
        reply_fn=AsyncMock(), message_id="m",
    )
    assert task.mentions == []


def test_task_accepts_mentions_list():
    task = Task(
        id="1", content="hi", session_id="s",
        reply_fn=AsyncMock(), message_id="m",
        mentions=[{"name": "小明", "open_id": "ou_abc"}],
    )
    assert task.mentions == [{"name": "小明", "open_id": "ou_abc"}]
