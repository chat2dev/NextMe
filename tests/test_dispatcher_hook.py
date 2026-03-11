"""Tests for TaskDispatcher.dispatch_hook_task (custom card hooks)."""
from __future__ import annotations

import pytest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.core.dispatcher import TaskDispatcher
from nextme.core.session import SessionRegistry
from nextme.core.path_lock import PathLockRegistry
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import Project, Settings


def _make_dispatcher(tmp_path: Path) -> tuple[TaskDispatcher, MagicMock]:
    """Return (dispatcher, replier) with a project rooted at tmp_path."""
    project = Project(name="proj", path=str(tmp_path), executor="claude")
    config = MagicMock()
    config.projects = [project]
    config.default_project = project
    config.get_binding = MagicMock(return_value=None)
    config.get_project = MagicMock(return_value=project)

    settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)

    replier = MagicMock()
    replier.send_text = AsyncMock()
    replier.send_card = AsyncMock()
    replier.reply_card = AsyncMock()
    replier.build_progress_card = MagicMock(return_value='{}')
    replier.build_result_card = MagicMock(return_value='{}')
    replier.build_error_card = MagicMock(return_value='{}')

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)

    d = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=SessionRegistry(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
    )
    return d, replier


async def test_dispatch_hook_task_missing_hook_name(tmp_path):
    """dispatch_hook_task silently returns when hook name is missing."""
    d, _ = _make_dispatcher(tmp_path)
    # Should not raise
    await d.dispatch_hook_task({"action": "nextme_hook"})


async def test_dispatch_hook_task_unsafe_hook_name(tmp_path):
    """dispatch_hook_task rejects hook names containing path separators."""
    d, _ = _make_dispatcher(tmp_path)
    # These names should all be silently dropped
    for bad in ["../evil", "sub/hook", ".hidden", "back\\slash"]:
        await d.dispatch_hook_task({"hook": bad})


async def test_dispatch_hook_task_file_not_found(tmp_path):
    """dispatch_hook_task silently returns when the hook file does not exist."""
    d, _ = _make_dispatcher(tmp_path)
    await d.dispatch_hook_task({
        "hook": "nonexistent",
        "session_id": "oc_chat:ou_user",
        "chat_id": "oc_chat",
        "operator_id": "ou_user",
    })
    # No exception, no task enqueued → no further interactions expected


async def test_dispatch_hook_task_loads_file_and_dispatches(tmp_path):
    """dispatch_hook_task reads the hook file and calls self.dispatch."""
    # Create hook file
    hooks_dir = tmp_path / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "greet.md").write_text("Say hello warmly.")

    d, _ = _make_dispatcher(tmp_path)

    dispatched: list = []

    async def _fake_dispatch(task):
        dispatched.append(task)

    d.dispatch = _fake_dispatch  # type: ignore[method-assign]

    await d.dispatch_hook_task({
        "action": "nextme_hook",
        "hook": "greet",
        "session_id": "oc_chat:ou_user",
        "operator_id": "ou_user",
        "chat_id": "oc_chat",
        "message_id": "om_msg1",
        "chat_type": "p2p",
    })

    assert len(dispatched) == 1
    task = dispatched[0]
    assert "Say hello warmly." in task.content
    assert task.session_id == "oc_chat:ou_user"
    assert task.message_id == "om_msg1"
    assert task.chat_type == "p2p"
    assert task.user_id == "ou_user"


async def test_dispatch_hook_task_context_appended(tmp_path):
    """Context metadata (operator, chat_id, etc.) is appended to hook content."""
    hooks_dir = tmp_path / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "ctx.md").write_text("Base prompt.")

    d, _ = _make_dispatcher(tmp_path)

    dispatched: list = []
    d.dispatch = AsyncMock(side_effect=lambda t: dispatched.append(t))  # type: ignore[method-assign]

    await d.dispatch_hook_task({
        "hook": "ctx",
        "session_id": "oc_chat:ou_user",
        "operator_id": "ou_op",
        "chat_id": "oc_some_chat",
        "message_id": "om_abc",
        "chat_type": "group",
        "extra_field": "my_value",
    })

    assert dispatched
    content = dispatched[0].content
    assert "Base prompt." in content
    assert "ou_op" in content
    assert "oc_some_chat" in content
    assert "om_abc" in content
    assert "my_value" in content


async def test_dispatch_hook_task_derives_session_id_from_chat_and_operator(tmp_path):
    """When session_id is absent, it is derived from chat_id:operator_id."""
    hooks_dir = tmp_path / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "auto.md").write_text("Auto session.")

    d, _ = _make_dispatcher(tmp_path)

    dispatched: list = []
    d.dispatch = AsyncMock(side_effect=lambda t: dispatched.append(t))  # type: ignore[method-assign]

    await d.dispatch_hook_task({
        "hook": "auto",
        "operator_id": "ou_op",
        "chat_id": "oc_chat",
        "message_id": "om_x",
        "chat_type": "p2p",
        # No session_id
    })

    assert dispatched
    assert dispatched[0].session_id == "oc_chat:ou_op"


async def test_dispatch_hook_task_no_session_no_default_project(tmp_path):
    """dispatch_hook_task silently returns when there's no session and no default project."""
    config = MagicMock()
    config.projects = []
    config.default_project = None
    config.get_binding = MagicMock(return_value=None)

    settings = Settings(task_queue_capacity=10)
    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=MagicMock())

    d = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=SessionRegistry(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
    )

    # Create hooks dir under tmp_path (won't matter since session can't be created)
    hooks_dir = tmp_path / ".nextme" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "test.md").write_text("Test.")

    # Should not raise
    await d.dispatch_hook_task({
        "hook": "test",
        "session_id": "oc_chat:ou_user",
        "chat_id": "oc_chat",
        "operator_id": "ou_user",
    })
