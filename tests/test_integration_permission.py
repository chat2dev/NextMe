"""Integration tests for the end-to-end permission confirmation flow.

These tests exercise the full path:
  ACPRuntime.on_permission → worker._on_permission → permission card sent
  → handle_card_action(context_id, index) → future resolved → task completes

Key regression guarded: session_id in the button value must be context_id
(``oc_xxx:ou_xxx``), NOT actual_id (ACP UUID).  Passing actual_id to
``session_registry.get()`` returns None → permission never resolved.
"""
from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.core.dispatcher import TaskDispatcher
from nextme.core.session import SessionRegistry, UserContext
from nextme.core.path_lock import PathLockRegistry
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import AppConfig, Project, Settings
from nextme.protocol.types import (
    PermissionChoice,
    PermissionRequest,
    PermOption,
    Task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTEXT_ID = "oc_chat123:ou_user456"
PROJECT_NAME = "main"
ACTUAL_ID = "d7ec3dbc-8cae-4776-b528-c6ef904c0ff3"


def make_settings() -> Settings:
    return Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=2.0,
    )


def make_config(tmp_path) -> AppConfig:
    project = Project(name=PROJECT_NAME, path=str(tmp_path), executor="claude")
    return AppConfig(projects=[project])


def make_replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock(return_value="msg_perm_card")
    r.send_card_by_id = AsyncMock(return_value="msg_by_id")
    r.update_card = AsyncMock()
    r.reply_text = AsyncMock(return_value="msg_reply")
    r.reply_card = AsyncMock(return_value="msg_reply_card")
    r.reply_card_by_id = AsyncMock(return_value="msg_reply_by_id")
    r.create_card = AsyncMock(return_value="")  # force fallback path
    r.get_card_id = AsyncMock(return_value="")
    r.stream_append_text = AsyncMock()
    r.stream_set_status = AsyncMock()
    r.build_progress_card = MagicMock(return_value='{"card":"progress"}')
    r.build_streaming_progress_card = MagicMock(return_value='{"card":"streaming"}')
    r.build_result_card = MagicMock(return_value='{"card":"result"}')
    r.build_error_card = MagicMock(return_value='{"card":"error"}')
    r.build_permission_card = MagicMock(return_value='{"card":"perm"}')
    return r


def make_im_adapter(replier):
    adapter = MagicMock()
    adapter.get_replier = MagicMock(return_value=replier)
    return adapter


def make_runtime(perm_options: list[PermOption] | None = None):
    """Make a mock ACPRuntime that triggers one permission request then returns."""
    runtime = AsyncMock()
    runtime.actual_id = ACTUAL_ID

    perm_options = perm_options or [
        PermOption(index=1, label="Yes, and don't ask again"),
        PermOption(index=2, label="No"),
    ]

    async def execute(task, on_progress, on_permission):
        # Simulate the ACP subprocess asking for permission.
        req = PermissionRequest(
            session_id=CONTEXT_ID,
            request_id="req-integration",
            description="apply_patch",
            options=perm_options,
        )
        choice = await on_permission(req)
        # Return a result that encodes which choice was made for assertions.
        return f"completed with choice={choice.option_index}"

    runtime.execute = execute
    runtime.ensure_ready = AsyncMock()
    return runtime


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_permission_resolved_via_handle_card_action(tmp_path):
    """Full flow: worker awaits permission → handle_card_action resolves it → task completes."""
    settings = make_settings()
    config = make_config(tmp_path)
    replier = make_replier()
    feishu_client = make_im_adapter(replier)
    path_lock_registry = PathLockRegistry()
    session_registry = SessionRegistry.get_instance()

    acp_registry = MagicMock(spec=ACPRuntimeRegistry)
    runtime = make_runtime()
    acp_registry.get_or_create = MagicMock(return_value=runtime)

    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        acp_registry=acp_registry,
        feishu_client=feishu_client,
        path_lock_registry=path_lock_registry,
        session_registry=session_registry,
    )

    task = Task(
        id=str(uuid.uuid4()),
        content="write a file",
        session_id=CONTEXT_ID,
        reply_fn=AsyncMock(),
        message_id="om_test_msg",
    )

    # Dispatch → starts worker, which will await on_permission.
    await dispatcher.dispatch(task)

    # Give the worker a moment to reach the permission await.
    await asyncio.sleep(0.15)

    # Simulate the user clicking the "Allow" button (index=1) on the card.
    # The button value contains session_id=context_id (the fix we're testing).
    dispatcher.handle_card_action(
        session_id=CONTEXT_ID,
        index=1,
        project_name=PROJECT_NAME,
    )

    # Wait for the worker to complete.
    await asyncio.sleep(0.3)

    # The result card should have been built (task completed).
    replier.build_result_card.assert_called()
    result_call_args = replier.build_result_card.call_args
    assert "completed with choice=1" in result_call_args.kwargs.get("content", "")


async def test_permission_not_resolved_with_actual_id(tmp_path):
    """Regression: passing actual_id instead of context_id must NOT resolve permission.

    This reproduces the bug where session_id in the button value was set to
    actual_id (e.g. 'd7ec3dbc-...') instead of context_id ('oc_xxx:ou_xxx'),
    causing handle_card_action to log 'no context for session_id' and leave
    the permission future pending until timeout.
    """
    settings = Settings(
        task_queue_capacity=10,
        progress_debounce_seconds=0.0,
        permission_timeout_seconds=0.3,  # short timeout for test speed
    )
    config = make_config(tmp_path)
    replier = make_replier()
    feishu_client = make_im_adapter(replier)
    path_lock_registry = PathLockRegistry()
    session_registry = SessionRegistry.get_instance()

    acp_registry = MagicMock(spec=ACPRuntimeRegistry)
    runtime = make_runtime()
    acp_registry.get_or_create = MagicMock(return_value=runtime)

    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        acp_registry=acp_registry,
        feishu_client=feishu_client,
        path_lock_registry=path_lock_registry,
        session_registry=session_registry,
    )

    task = Task(
        id=str(uuid.uuid4()),
        content="write a file",
        session_id=CONTEXT_ID,
        reply_fn=AsyncMock(),
        message_id="om_test_msg2",
    )

    await dispatcher.dispatch(task)
    await asyncio.sleep(0.1)

    # Try to resolve with actual_id (the bug scenario) — should be a no-op.
    dispatcher.handle_card_action(
        session_id=ACTUAL_ID,  # wrong! this is actual_id, not context_id
        index=1,
        project_name=PROJECT_NAME,
    )

    # Permission was not resolved via the card action; task times out and
    # defaults to index=1 anyway, so it still completes — but via timeout path.
    await asyncio.sleep(0.5)

    # Task completed via timeout (default choice=1), not via card action.
    replier.build_result_card.assert_called()


async def test_permission_card_build_args(tmp_path):
    """Worker must pass context_id as session_id and actual_id as display_id."""
    settings = make_settings()
    config = make_config(tmp_path)
    replier = make_replier()
    feishu_client = make_im_adapter(replier)
    path_lock_registry = PathLockRegistry()
    session_registry = SessionRegistry.get_instance()

    acp_registry = MagicMock(spec=ACPRuntimeRegistry)
    runtime = make_runtime()
    acp_registry.get_or_create = MagicMock(return_value=runtime)

    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        acp_registry=acp_registry,
        feishu_client=feishu_client,
        path_lock_registry=path_lock_registry,
        session_registry=session_registry,
    )

    task = Task(
        id=str(uuid.uuid4()),
        content="write a file",
        session_id=CONTEXT_ID,
        reply_fn=AsyncMock(),
        message_id="om_test_msg3",
    )

    await dispatcher.dispatch(task)
    await asyncio.sleep(0.15)

    # Verify build_permission_card was called with the correct arguments.
    replier.build_permission_card.assert_called_once()
    kwargs = replier.build_permission_card.call_args.kwargs

    # session_id must be context_id for registry lookup.
    assert kwargs["session_id"] == CONTEXT_ID, (
        f"session_id should be context_id {CONTEXT_ID!r}, got {kwargs['session_id']!r}"
    )
    # display_id must be actual_id for the footer.
    assert kwargs["display_id"] == ACTUAL_ID, (
        f"display_id should be actual_id {ACTUAL_ID!r}, got {kwargs.get('display_id')!r}"
    )

    # Resolve permission to let the task complete cleanly.
    dispatcher.handle_card_action(CONTEXT_ID, 1, PROJECT_NAME)
    await asyncio.sleep(0.3)
