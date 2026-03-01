"""Tests for ACL card action handlers (acl_apply and acl_review)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.acl.db import AclDb
from nextme.acl.manager import AclManager
from nextme.acl.schema import Role
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.session import SessionRegistry
from nextme.core.path_lock import PathLockRegistry
from nextme.acp.janitor import ACPRuntimeRegistry


@pytest.fixture
async def db(tmp_path):
    d = AclDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def acl_manager(db):
    return AclManager(db=db, admin_users=["ou_admin"])


@pytest.fixture
def dispatcher_with_acl(acl_manager):
    config = MagicMock()
    config.projects = []
    config.get_binding = MagicMock(return_value=None)
    settings = MagicMock()
    settings.task_queue_capacity = 10

    replier = MagicMock()
    replier.send_card = AsyncMock()
    replier.send_text = AsyncMock()
    replier.send_to_user = AsyncMock()
    replier.build_acl_review_notification_card = MagicMock(return_value='{"card":"review"}')

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


async def test_handle_acl_apply_creates_application(dispatcher_with_acl):
    d, replier, acl_manager = dispatcher_with_acl
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    app = await acl_manager._db.get_pending_application("ou_applicant")
    assert app is not None
    assert app.requested_role == Role.COLLABORATOR


async def test_handle_acl_apply_duplicate_does_not_create_second(dispatcher_with_acl):
    d, replier, acl_manager = dispatcher_with_acl
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    pending = await acl_manager._db.list_pending_applications()
    assert len(pending) == 1


async def test_handle_acl_apply_notifies_reviewers(dispatcher_with_acl, db):
    d, replier, acl_manager = dispatcher_with_acl
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    assert replier.send_to_user.call_count >= 1


async def test_handle_acl_review_approve(dispatcher_with_acl):
    d, replier, acl_manager = dispatcher_with_acl
    app_id, _ = await acl_manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    role = await acl_manager.get_role("ou_x")
    assert role == Role.COLLABORATOR


async def test_handle_acl_review_reject(dispatcher_with_acl):
    d, replier, acl_manager = dispatcher_with_acl
    app_id, _ = await acl_manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })
    role = await acl_manager.get_role("ou_x")
    assert role is None
