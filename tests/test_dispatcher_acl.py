"""Tests for ACL gate and command routing in TaskDispatcher."""
from __future__ import annotations

import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from nextme.acl.schema import Role
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.session import SessionRegistry
from nextme.core.path_lock import PathLockRegistry
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.protocol.types import Task


def make_task(user_id="ou_user", chat_id="oc_chat", content="hello"):
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=f"{chat_id}:{user_id}",
        reply_fn=AsyncMock(),
        message_id="msg_1",
        chat_type="p2p",
    )


@pytest.fixture
def acl_manager():
    m = MagicMock()
    m.get_role = AsyncMock(return_value=None)  # unauthorized by default
    m.get_admin_ids = MagicMock(return_value=["ou_admin"])
    return m


@pytest.fixture
def dispatcher(acl_manager):
    config = MagicMock()
    config.projects = []
    config.default_project = None
    config.get_binding = MagicMock(return_value=None)
    settings = MagicMock()
    settings.task_queue_capacity = 10

    replier = MagicMock()
    replier.send_card = AsyncMock()
    replier.send_text = AsyncMock()
    replier.reply_card = AsyncMock()
    replier.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)

    d = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=SessionRegistry(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=feishu_client,
        acl_manager=acl_manager,
    )
    d._feishu_client = feishu_client
    return d, replier, acl_manager


async def test_unauthorized_user_gets_denied_card(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None
    task = make_task(user_id="ou_stranger", content="do something")
    await d.dispatch(task)
    replier.build_access_denied_card.assert_called_once_with("ou_stranger")


async def test_whoami_bypasses_acl_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None  # unauthorized
    with patch.object(d, '_handle_meta_command', new=AsyncMock()) as mock_cmd:
        task = make_task(content="/whoami")
        await d.dispatch(task)
        mock_cmd.assert_called_once()
    replier.build_access_denied_card.assert_not_called()


async def test_help_bypasses_acl_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None
    with patch.object(d, '_handle_meta_command', new=AsyncMock()) as mock_cmd:
        task = make_task(content="/help")
        await d.dispatch(task)
        mock_cmd.assert_called_once()
    replier.build_access_denied_card.assert_not_called()


async def test_authorized_user_passes_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = Role.COLLABORATOR
    d._config.default_project = MagicMock()
    d._config.default_project.name = "proj"
    task = make_task(content="/help")
    with patch.object(d, '_handle_meta_command', new=AsyncMock()):
        await d.dispatch(task)
    replier.build_access_denied_card.assert_not_called()


async def test_no_acl_manager_allows_all(dispatcher):
    d, replier, acl_manager = dispatcher
    d._acl_manager = None  # Remove ACL manager
    d._config.default_project = MagicMock()
    d._config.default_project.name = "proj"
    task = make_task(content="/help")
    with patch.object(d, '_handle_meta_command', new=AsyncMock()):
        await d.dispatch(task)
    replier.build_access_denied_card.assert_not_called()


async def test_unauthorized_group_chat_sends_prompt_and_dm(dispatcher):
    """Group chat: unauthorized user gets a text prompt in thread + apply card via DM."""
    d, replier, acl_manager = dispatcher
    replier.reply_text = AsyncMock()
    replier.send_to_user = AsyncMock()
    acl_manager.get_role.return_value = None

    task = Task(
        id=str(uuid.uuid4()),
        content="hello",
        session_id="oc_group:ou_stranger",
        reply_fn=AsyncMock(),
        message_id="msg_grp",
        chat_type="group",
    )
    await d.dispatch(task)

    # Thread gets a plain text prompt (no apply button)
    replier.reply_text.assert_called_once()
    args = replier.reply_text.call_args
    assert args.kwargs.get("in_thread") is True
    assert "私信" in args.args[1]

    # Apply card sent as DM to the user
    replier.send_to_user.assert_called_once()
    assert replier.send_to_user.call_args.args[0] == "ou_stranger"

    # No card posted to the group thread
    replier.reply_card.assert_not_called()


async def test_apply_by_different_operator_is_ignored(dispatcher):
    """A group member clicking someone else's apply button is silently ignored."""
    d, replier, acl_manager = dispatcher
    # data contains open_id of the applicant but operator_id of a different user
    action_data = {
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "operator_id": "ou_other_person",
        "role": "collaborator",
    }
    replier.send_to_user = AsyncMock()
    acl_manager.create_application = AsyncMock(return_value=(1, None))

    await d.handle_acl_card_action(action_data)

    # Application must NOT be created
    acl_manager.create_application.assert_not_called()
    replier.send_to_user.assert_not_called()
