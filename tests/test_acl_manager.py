"""Tests for AclManager business logic."""
from __future__ import annotations

import pytest
from nextme.acl.db import AclDb
from nextme.acl.manager import AclManager
from nextme.acl.schema import Role


@pytest.fixture
async def db(tmp_path):
    d = AclDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def manager(db):
    return AclManager(db=db, admin_users=["ou_admin"])


async def test_get_role_admin(manager):
    role = await manager.get_role("ou_admin")
    assert role == Role.ADMIN


async def test_get_role_owner(manager, db):
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    role = await manager.get_role("ou_owner")
    assert role == Role.OWNER


async def test_get_role_collaborator(manager, db):
    await db.add_user("ou_collab", Role.COLLABORATOR, "Collab", "ou_admin")
    role = await manager.get_role("ou_collab")
    assert role == Role.COLLABORATOR


async def test_get_role_unauthorized(manager):
    role = await manager.get_role("ou_nobody")
    assert role is None


async def test_add_user_owner(manager):
    await manager.add_user("ou_new", Role.OWNER, "New Owner", added_by="ou_admin")
    role = await manager.get_role("ou_new")
    assert role == Role.OWNER


async def test_add_user_cannot_add_admin(manager):
    with pytest.raises(ValueError, match="Cannot add admin"):
        await manager.add_user("ou_x", Role.ADMIN, "X", added_by="ou_admin")


async def test_remove_user(manager, db):
    await db.add_user("ou_bye", Role.COLLABORATOR, "Bye", "ou_admin")
    removed = await manager.remove_user("ou_bye")
    assert removed is True


async def test_remove_admin_raises(manager):
    with pytest.raises(ValueError, match="Cannot remove admin"):
        await manager.remove_user("ou_admin")


async def test_create_application_new(manager):
    app_id, existing = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    assert app_id is not None
    assert existing is None


async def test_create_application_duplicate(manager):
    await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    app_id, existing = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    assert app_id is None
    assert existing is not None
    assert existing.status == "pending"


async def test_approve(manager):
    app_id, _ = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    result = await manager.approve(app_id, reviewer_id="ou_admin")
    assert result is not None
    assert result.status == "approved"
    # User should now be in acl_users
    role = await manager.get_role("ou_x")
    assert role == Role.COLLABORATOR


async def test_reject(manager):
    app_id, _ = await manager.create_application("ou_x", "X", Role.OWNER)
    result = await manager.reject(app_id, reviewer_id="ou_admin")
    assert result is not None
    assert result.status == "rejected"
    # User should NOT be in acl_users
    role = await manager.get_role("ou_x")
    assert role is None


async def test_list_pending_as_admin(manager):
    await manager.create_application("ou_a", "A", Role.COLLABORATOR)
    await manager.create_application("ou_b", "B", Role.OWNER)
    pending = await manager.list_pending(Role.ADMIN)
    assert len(pending) == 2


async def test_list_pending_as_owner(manager):
    await manager.create_application("ou_a", "A", Role.COLLABORATOR)
    await manager.create_application("ou_b", "B", Role.OWNER)
    pending = await manager.list_pending(Role.OWNER)
    assert len(pending) == 1
    assert pending[0].applicant_id == "ou_a"


async def test_can_review():
    mgr = AclManager(db=None, admin_users=[])  # type: ignore
    assert mgr.can_review(Role.ADMIN, Role.OWNER) is True
    assert mgr.can_review(Role.ADMIN, Role.COLLABORATOR) is True
    assert mgr.can_review(Role.OWNER, Role.COLLABORATOR) is True
    assert mgr.can_review(Role.OWNER, Role.OWNER) is False
    assert mgr.can_review(Role.COLLABORATOR, Role.COLLABORATOR) is False


async def test_get_reviewers_for_owner_role(manager, db):
    await db.add_user("ou_owner1", Role.OWNER, "O1", "ou_admin")
    reviewers = await manager.get_reviewers_for_role(Role.OWNER)
    assert "ou_admin" in reviewers
    assert "ou_owner1" not in reviewers  # owner apps only go to admins


async def test_get_reviewers_for_collaborator_role(manager, db):
    await db.add_user("ou_owner1", Role.OWNER, "O1", "ou_admin")
    reviewers = await manager.get_reviewers_for_role(Role.COLLABORATOR)
    assert "ou_admin" in reviewers
    assert "ou_owner1" in reviewers


def test_can_add_admin_can_add_owner_and_collab():
    mgr = AclManager(db=None, admin_users=[])  # type: ignore
    assert mgr.can_add(Role.ADMIN, Role.OWNER) is True
    assert mgr.can_add(Role.ADMIN, Role.COLLABORATOR) is True


def test_can_add_owner_can_add_collab_only():
    mgr = AclManager(db=None, admin_users=[])  # type: ignore
    assert mgr.can_add(Role.OWNER, Role.COLLABORATOR) is True
    assert mgr.can_add(Role.OWNER, Role.OWNER) is False


def test_can_add_collaborator_cannot_add():
    mgr = AclManager(db=None, admin_users=[])  # type: ignore
    assert mgr.can_add(Role.COLLABORATOR, Role.COLLABORATOR) is False


async def test_can_remove_admin_cannot_remove_admin_users(manager):
    mgr = AclManager(db=manager._db, admin_users=["ou_admin"])
    from nextme.acl.schema import AclUser
    from datetime import datetime
    admin_user = AclUser(open_id="ou_admin", role=Role.ADMIN, added_by="sys", added_at=datetime.now())
    assert mgr.can_remove(Role.ADMIN, admin_user) is False


async def test_can_remove_admin_can_remove_owner(manager, db):
    await db.add_user("ou_o", Role.OWNER, "O", "ou_admin")
    user = await db.get_user("ou_o")
    assert manager.can_remove(Role.ADMIN, user) is True


async def test_can_remove_owner_can_remove_collab(manager, db):
    await db.add_user("ou_c", Role.COLLABORATOR, "C", "ou_admin")
    user = await db.get_user("ou_c")
    assert manager.can_remove(Role.OWNER, user) is True


async def test_can_remove_owner_cannot_remove_owner(manager, db):
    await db.add_user("ou_o", Role.OWNER, "O", "ou_admin")
    user = await db.get_user("ou_o")
    assert manager.can_remove(Role.OWNER, user) is False


async def test_list_pending_as_collaborator(manager):
    await manager.create_application("ou_a", "A", Role.COLLABORATOR)
    pending = await manager.list_pending(Role.COLLABORATOR)
    assert pending == []


async def test_approve_nonexistent_returns_none(manager):
    result = await manager.approve(9999, "ou_admin")
    assert result is None


async def test_reject_already_approved_returns_none(manager):
    app_id, _ = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await manager.approve(app_id, "ou_admin")
    result = await manager.reject(app_id, "ou_admin")
    assert result is None


async def test_get_application(manager):
    app_id, _ = await manager.create_application("ou_x", "X", Role.COLLABORATOR)
    app = await manager.get_application(app_id)
    assert app is not None
    assert app.id == app_id


async def test_get_application_not_found(manager):
    app = await manager.get_application(9999)
    assert app is None
