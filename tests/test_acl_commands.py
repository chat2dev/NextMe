"""Tests for ACL command handlers."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from nextme.acl.db import AclDb
from nextme.acl.manager import AclManager
from nextme.acl.schema import AclUser, Role
from nextme.core.commands import (
    handle_whoami,
    handle_acl_list,
    handle_acl_add,
    handle_acl_remove,
    handle_acl_pending,
    handle_acl_approve,
    handle_acl_reject,
    handle_status,
)
from nextme.core.session import UserContext
from nextme.config.schema import Project, Settings


@pytest.fixture
def replier():
    r = MagicMock()
    r.send_text = AsyncMock(return_value="msg_1")
    r.send_card = AsyncMock(return_value="msg_2")
    r.build_whoami_card = MagicMock(return_value='{"card":"whoami"}')
    r.build_acl_list_card = MagicMock(return_value='{"card":"list"}')
    r.build_acl_pending_card = MagicMock(return_value='{"card":"pending"}')
    r.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')
    return r


@pytest.fixture
async def db(tmp_path):
    d = AclDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def manager(db):
    return AclManager(db=db, admin_users=["ou_admin"])


async def test_handle_whoami_unauthorized(manager, replier):
    await handle_whoami("ou_nobody", manager, replier, "chat_1")
    replier.build_whoami_card.assert_called_once()
    args = replier.build_whoami_card.call_args[0]
    assert args[0] == "ou_nobody"
    assert args[1] is None  # role
    replier.send_card.assert_called_once_with("chat_1", '{"card":"whoami"}')


async def test_handle_whoami_admin(manager, replier):
    await handle_whoami("ou_admin", manager, replier, "chat_1")
    args = replier.build_whoami_card.call_args[0]
    assert args[1] == Role.ADMIN


async def test_handle_whoami_owner(manager, db, replier):
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    await handle_whoami("ou_owner", manager, replier, "chat_1")
    args = replier.build_whoami_card.call_args[0]
    assert args[1] == Role.OWNER


async def test_handle_acl_list(manager, db, replier):
    await db.add_user("ou_o", Role.OWNER, "O", "sys")
    await handle_acl_list(manager, replier, "chat_1")
    replier.build_acl_list_card.assert_called_once()
    replier.send_card.assert_called_once()


async def test_handle_acl_add_collaborator_by_admin(manager, replier):
    await handle_acl_add(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_new",
        target_role_str="collaborator",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    replier.send_text.assert_called_once()
    call_text = replier.send_text.call_args[0][1]
    assert "collaborator" in call_text.lower() or "协作者" in call_text


async def test_handle_acl_add_owner_by_collaborator_denied(manager, replier):
    await handle_acl_add(
        actor_id="ou_collab",
        actor_role=Role.COLLABORATOR,
        target_id="ou_new",
        target_role_str="owner",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "权限不足" in call_text or "insufficient" in call_text.lower()


async def test_handle_acl_remove_by_admin(manager, db, replier):
    await db.add_user("ou_bye", Role.COLLABORATOR, "Bye", "sys")
    await handle_acl_remove(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_bye",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "移除" in call_text or "removed" in call_text.lower()


async def test_handle_acl_remove_admin_fails(manager, replier):
    await handle_acl_remove(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_admin",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "管理员" in call_text or "admin" in call_text.lower()


async def test_handle_acl_pending(manager, replier):
    await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await handle_acl_pending(
        viewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    replier.build_acl_pending_card.assert_called_once()
    replier.send_card.assert_called_once()


async def test_handle_acl_approve(manager, replier):
    app_id, _ = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await handle_acl_approve(
        app_id=app_id,
        reviewer_id="ou_admin",
        reviewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "批准" in call_text or "approved" in call_text.lower()


async def test_handle_acl_reject(manager, replier):
    app_id, _ = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await handle_acl_reject(
        app_id=app_id,
        reviewer_id="ou_admin",
        reviewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "拒绝" in call_text or "rejected" in call_text.lower()


async def test_handle_acl_add_invalid_role(manager, replier):
    await handle_acl_add(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_new",
        target_role_str="superuser",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "superuser" in call_text or "未知角色" in call_text


async def test_handle_acl_add_admin_role_denied(manager, replier):
    await handle_acl_add(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_new",
        target_role_str="admin",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "Admin" in call_text or "admin" in call_text.lower()


async def test_handle_acl_remove_user_not_found(manager, replier):
    await handle_acl_remove(
        actor_id="ou_admin",
        actor_role=Role.ADMIN,
        target_id="ou_nonexistent",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "未找到" in call_text or "not found" in call_text.lower()


async def test_handle_acl_approve_not_found(manager, replier):
    await handle_acl_approve(
        app_id=9999,
        reviewer_id="ou_admin",
        reviewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "未找到" in call_text or "not found" in call_text.lower()


async def test_handle_acl_approve_insufficient_permission(manager, replier):
    app_id, _ = await manager.create_application("ou_x", "X", Role.OWNER)
    await handle_acl_approve(
        app_id=app_id,
        reviewer_id="ou_owner",
        reviewer_role=Role.OWNER,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "权限不足" in call_text


async def test_handle_acl_reject_not_found(manager, replier):
    await handle_acl_reject(
        app_id=9999,
        reviewer_id="ou_admin",
        reviewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "未找到" in call_text or "not found" in call_text.lower()


def _make_user_ctx_with_session(tmp_path):
    project = Project(name="p", path=str(tmp_path), executor="mock")
    settings = Settings(task_queue_capacity=5, progress_debounce_seconds=0.0)
    ctx = UserContext("oc_chat:ou_user")
    ctx.get_or_create_session(project, settings)
    return ctx


async def test_handle_status_send_card_raises(replier, tmp_path):
    """handle_status must not re-raise when send_card fails."""
    replier.send_card = AsyncMock(side_effect=Exception("network error"))
    user_ctx = _make_user_ctx_with_session(tmp_path)
    # Should not raise even though send_card raises.
    await handle_status(user_ctx, replier, "chat_1")
    replier.send_card.assert_called_once()


async def test_handle_whoami_send_card_raises(manager, replier):
    """handle_whoami must not re-raise when send_card fails."""
    replier.send_card = AsyncMock(side_effect=Exception("network error"))
    # ou_nobody has no role (returns None), so it won't try to get_user.
    await handle_whoami("ou_nobody", manager, replier, "chat_1")
    replier.send_card.assert_called_once()


async def test_handle_acl_add_db_error(manager, replier):
    """handle_acl_add sends failure message when add_user raises."""
    with patch.object(manager, "add_user", new=AsyncMock(side_effect=Exception("DB error"))):
        await handle_acl_add(
            actor_id="ou_admin",
            actor_role=Role.ADMIN,
            target_id="ou_new",
            target_role_str="collaborator",
            acl_manager=manager,
            replier=replier,
            chat_id="chat_1",
        )
    call_text = replier.send_text.call_args[0][1]
    assert "失败" in call_text or "error" in call_text.lower()


async def test_handle_acl_remove_permission_denied(manager, db, replier):
    """A Collaborator cannot remove another user — permission denied."""
    await db.add_user("ou_victim", Role.COLLABORATOR, "Victim", "ou_admin")
    await handle_acl_remove(
        actor_id="ou_collab",
        actor_role=Role.COLLABORATOR,
        target_id="ou_victim",
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    call_text = replier.send_text.call_args[0][1]
    assert "权限不足" in call_text


async def test_handle_acl_pending_raises(manager, replier):
    """handle_acl_pending must not re-raise when send_card fails."""
    replier.send_card = AsyncMock(side_effect=Exception("network error"))
    await handle_acl_pending(
        viewer_role=Role.ADMIN,
        acl_manager=manager,
        replier=replier,
        chat_id="chat_1",
    )
    replier.send_card.assert_called_once()
