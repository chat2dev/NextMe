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


def test_task_mentions_instances_are_independent():
    task1 = Task(id="1", content="hi", session_id="s", reply_fn=AsyncMock())
    task2 = Task(id="2", content="hi", session_id="s", reply_fn=AsyncMock())
    task1.mentions.append({"name": "x", "open_id": "y"})
    assert task2.mentions == []


def test_task_has_user_id_field():
    task = Task(id="t1", content="hi", session_id="oc_x:ou_y", reply_fn=lambda r: None)
    assert task.user_id == ""


def test_task_has_thread_root_id_field():
    task = Task(id="t1", content="hi", session_id="oc_x:ou_y", reply_fn=lambda r: None)
    assert task.thread_root_id == ""


def test_task_user_id_and_thread_root_id_accept_values():
    task = Task(
        id="t1", content="hi", session_id="oc_x:ou_y", reply_fn=lambda r: None,
        user_id="ou_abc", thread_root_id="om_root123",
    )
    assert task.user_id == "ou_abc"
    assert task.thread_root_id == "om_root123"
