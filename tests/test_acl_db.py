# tests/test_acl_db.py
import pytest
from pathlib import Path
from nextme.acl.db import AclDb
from nextme.acl.schema import Role


@pytest.fixture
async def db(tmp_path):
    d = AclDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


async def test_add_and_get_user(db):
    await db.add_user("ou_alice", Role.OWNER, "Alice", "ou_admin")
    user = await db.get_user("ou_alice")
    assert user is not None
    assert user.open_id == "ou_alice"
    assert user.role == Role.OWNER
    assert user.display_name == "Alice"
    assert user.added_by == "ou_admin"


async def test_get_user_not_found(db):
    user = await db.get_user("ou_nobody")
    assert user is None


async def test_remove_user(db):
    await db.add_user("ou_bob", Role.COLLABORATOR, "Bob", "ou_admin")
    removed = await db.remove_user("ou_bob")
    assert removed is True
    assert await db.get_user("ou_bob") is None


async def test_remove_nonexistent_user(db):
    removed = await db.remove_user("ou_ghost")
    assert removed is False


async def test_list_users_all(db):
    await db.add_user("ou_a", Role.OWNER, "A", "sys")
    await db.add_user("ou_b", Role.COLLABORATOR, "B", "sys")
    users = await db.list_users()
    assert len(users) == 2


async def test_list_users_by_role(db):
    await db.add_user("ou_a", Role.OWNER, "A", "sys")
    await db.add_user("ou_b", Role.COLLABORATOR, "B", "sys")
    owners = await db.list_users(Role.OWNER)
    assert len(owners) == 1
    assert owners[0].open_id == "ou_a"


async def test_create_application(db):
    app_id = await db.create_application("ou_x", "X User", Role.COLLABORATOR)
    assert app_id is not None
    app = await db.get_application(app_id)
    assert app is not None
    assert app.applicant_id == "ou_x"
    assert app.status == "pending"
    assert app.requested_role == Role.COLLABORATOR


async def test_create_duplicate_pending_returns_none(db):
    await db.create_application("ou_x", "X", Role.COLLABORATOR)
    second = await db.create_application("ou_x", "X", Role.COLLABORATOR)
    assert second is None


async def test_get_pending_application(db):
    await db.create_application("ou_x", "X", Role.OWNER)
    app = await db.get_pending_application("ou_x")
    assert app is not None
    assert app.status == "pending"


async def test_get_pending_application_not_found(db):
    app = await db.get_pending_application("ou_nobody")
    assert app is None


async def test_update_application_status_approve(db):
    app_id = await db.create_application("ou_x", "X", Role.COLLABORATOR)
    updated = await db.update_application_status(app_id, "approved", "ou_reviewer")
    assert updated is True
    app = await db.get_application(app_id)
    assert app.status == "approved"
    assert app.processed_by == "ou_reviewer"
    assert app.processed_at is not None


async def test_update_already_processed_returns_false(db):
    app_id = await db.create_application("ou_x", "X", Role.COLLABORATOR)
    await db.update_application_status(app_id, "approved", "ou_r")
    # Try to update again (only pending rows can be updated)
    updated = await db.update_application_status(app_id, "rejected", "ou_r")
    assert updated is False


async def test_list_pending_applications(db):
    await db.create_application("ou_a", "A", Role.COLLABORATOR)
    await db.create_application("ou_b", "B", Role.OWNER)
    all_pending = await db.list_pending_applications()
    assert len(all_pending) == 2


async def test_list_pending_applications_by_role(db):
    await db.create_application("ou_a", "A", Role.COLLABORATOR)
    await db.create_application("ou_b", "B", Role.OWNER)
    collab_pending = await db.list_pending_applications(Role.COLLABORATOR)
    assert len(collab_pending) == 1
    assert collab_pending[0].applicant_id == "ou_a"


async def test_add_user_updates_existing(db):
    await db.add_user("ou_bob", Role.COLLABORATOR, "Bob", "ou_admin")
    await db.add_user("ou_bob", Role.OWNER, "Bob Updated", "ou_admin2")
    user = await db.get_user("ou_bob")
    assert user is not None
    assert user.role == Role.OWNER
    assert user.display_name == "Bob Updated"
    assert user.added_by == "ou_admin2"
