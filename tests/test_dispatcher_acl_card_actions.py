"""Tests for ACL card action handling methods in TaskDispatcher.

Covers:
  - handle_acl_card_action (lines 717-739)
  - _handle_acl_apply_action (lines 741-801)
  - _handle_acl_review_action (lines 803-883)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.acl.db import AclDb
from nextme.acl.manager import AclManager
from nextme.acl.schema import Role
from nextme.acp.janitor import ACPRuntimeRegistry
from nextme.config.schema import Settings
from nextme.core.dispatcher import TaskDispatcher
from nextme.core.path_lock import PathLockRegistry
from nextme.core.session import SessionRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_session_registry():
    SessionRegistry._instance = None
    yield
    SessionRegistry._instance = None


@pytest.fixture
async def db(tmp_path):
    d = AclDb(db_path=tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def acl_manager(db):
    return AclManager(db=db, admin_users=["ou_admin"])


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def make_replier():
    r = MagicMock()
    r.send_text = AsyncMock()
    r.send_card = AsyncMock()
    r.send_to_user = AsyncMock()
    r.build_acl_review_notification_card = MagicMock(return_value='{"card":"notify"}')
    return r


def make_dispatcher(replier, acl_manager=None):
    config = MagicMock()
    config.projects = []
    config.default_project = None
    config.get_binding = MagicMock(return_value=None)
    settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)
    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=replier)
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=SessionRegistry(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=fc,
        acl_manager=acl_manager,
    )


# ===========================================================================
# Group 1: handle_acl_card_action dispatch
# ===========================================================================


async def test_no_acl_manager_returns_early():
    """When no ACL manager is configured, returns immediately without error."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=None)
    # Should not raise; returns early with a warning log
    await d.handle_acl_card_action({"action": "acl_apply", "open_id": "ou_x", "role": "collaborator"})
    replier.send_to_user.assert_not_called()


async def test_unknown_action_logs_warning(acl_manager):
    """An unknown action is safely ignored with a warning log."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # Should not raise; just logs a warning
    await d.handle_acl_card_action({"action": "mystery_action"})
    replier.send_to_user.assert_not_called()


async def test_unknown_action_none_logs_warning(acl_manager):
    """An action of None is safely ignored with a warning log."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({})  # no "action" key → None
    replier.send_to_user.assert_not_called()


# ===========================================================================
# Group 2: _handle_acl_apply_action early-exit paths
# ===========================================================================


async def test_apply_missing_open_id_returns_early(acl_manager):
    """Missing open_id → returns early, no application created."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({"action": "acl_apply", "role": "collaborator"})
    # No application was created
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []
    replier.send_to_user.assert_not_called()


async def test_apply_empty_open_id_returns_early(acl_manager):
    """Empty string open_id → returns early, no application created."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({"action": "acl_apply", "open_id": "", "role": "collaborator"})
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []
    replier.send_to_user.assert_not_called()


async def test_apply_invalid_role_returns_early(acl_manager):
    """Invalid role string → ValueError caught, returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({"action": "acl_apply", "open_id": "ou_x", "role": "superuser"})
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []


async def test_apply_admin_role_denied(acl_manager):
    """Requesting admin role is explicitly blocked."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({"action": "acl_apply", "open_id": "ou_x", "role": "admin"})
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []
    replier.send_to_user.assert_not_called()


async def test_apply_already_authorized_user_skipped(acl_manager, db):
    """User already has a role → no new application is created."""
    # Pre-authorize the user
    await db.add_user("ou_existing", Role.COLLABORATOR, "Existing User", "ou_admin")
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_existing",
        "role": "collaborator",
    })
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []
    replier.send_to_user.assert_not_called()


async def test_apply_admin_user_already_authorized_skipped(acl_manager):
    """Admin users (from admin_users list) are already authorized → skipped."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # ou_admin is in admin_users → get_role returns Role.ADMIN
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_admin",
        "role": "collaborator",
    })
    pending = await acl_manager._db.list_pending_applications()
    assert pending == []


async def test_apply_duplicate_pending_application_returns_early(acl_manager):
    """A second apply for the same user returns early without creating a duplicate."""
    # Create the first application
    await acl_manager.create_application("ou_new", "", Role.COLLABORATOR)

    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_new",
        "role": "collaborator",
    })
    # Still only one pending application
    pending = await acl_manager._db.list_pending_applications()
    assert len(pending) == 1


async def test_apply_success_notifies_reviewers(acl_manager):
    """Successful apply → reviewer notification card is built and sent."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_new",
        "role": "collaborator",
    })
    # build_acl_review_notification_card should have been called
    replier.build_acl_review_notification_card.assert_called_once()
    # send_to_user should have been called for ou_admin (the admin reviewer)
    replier.send_to_user.assert_called()


async def test_apply_success_owner_role_notifies_admin_only(acl_manager):
    """Owner role application → only admin reviewers are notified (not owners)."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_new_owner",
        "role": "owner",
    })
    replier.build_acl_review_notification_card.assert_called_once()
    # Exactly one notification to ou_admin
    assert replier.send_to_user.call_count == 1
    call_args = replier.send_to_user.call_args_list[0]
    assert call_args[0][0] == "ou_admin"


async def test_apply_notification_failure_is_swallowed(acl_manager):
    """Exception when notifying reviewers is caught and does not propagate."""
    replier = make_replier()
    replier.send_to_user = AsyncMock(side_effect=Exception("network error"))
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # Should NOT raise
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_new",
        "role": "collaborator",
    })
    # Application was still created despite notification failure
    pending = await acl_manager._db.list_pending_applications()
    assert len(pending) == 1


# ===========================================================================
# Group 3: _handle_acl_review_action early-exit paths
# ===========================================================================


async def test_review_missing_all_fields_returns_early(acl_manager):
    """All required fields missing → returns early without processing."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({"action": "acl_review"})
    replier.send_to_user.assert_not_called()


async def test_review_missing_decision_returns_early(acl_manager):
    """Missing decision field → returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": "1",
        "operator_id": "ou_admin",
        # "decision" missing
    })
    replier.send_to_user.assert_not_called()


async def test_review_missing_operator_id_returns_early(acl_manager):
    """Missing operator_id field → returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": "1",
        "decision": "approved",
        # "operator_id" missing
    })
    replier.send_to_user.assert_not_called()


async def test_review_invalid_app_id_returns_early(acl_manager):
    """Non-integer app_id → ValueError caught, returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": "not_a_number",
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    replier.send_to_user.assert_not_called()


async def test_review_unauthorized_reviewer_returns_early(acl_manager):
    """Reviewer with no role (not in DB, not admin) → returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # Create an application first
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_stranger",  # not admin, not in DB
    })
    # Application should still be pending
    app = await acl_manager.get_application(app_id)
    assert app.status == "pending"
    replier.send_to_user.assert_not_called()


async def test_review_app_not_found_returns_early(acl_manager):
    """Non-existent app_id → returns early without processing."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": "9999",  # doesn't exist
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    replier.send_to_user.assert_not_called()


async def test_review_already_processed_app_returns_early(acl_manager):
    """Application that is already approved/rejected → returns early."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # Create and approve application directly
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    await acl_manager.approve(app_id, "ou_admin")
    # Try to review again
    replier.send_to_user.reset_mock()
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })
    # send_to_user should NOT have been called for the second review
    replier.send_to_user.assert_not_called()


async def test_review_cannot_review_same_level_returns_early(acl_manager, db):
    """Reviewer without sufficient rank → can_review returns False, returns early."""
    # Add an owner user who can only review collaborator apps
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    # Create an OWNER application
    app_id, _ = await acl_manager.create_application("ou_owner_applicant", "", Role.OWNER)
    # ou_owner is OWNER but cannot review OWNER applications (only ADMIN can)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_owner",
    })
    # Application should still be pending
    app = await acl_manager.get_application(app_id)
    assert app.status == "pending"
    replier.send_to_user.assert_not_called()


async def test_review_approved_notifies_applicant(acl_manager):
    """Successful approval → applicant is notified via send_to_user."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    # Applicant should be notified
    replier.send_to_user.assert_called()
    call_args = replier.send_to_user.call_args_list[-1]
    assert call_args[0][0] == "ou_applicant"
    # Application should be approved in DB
    app = await acl_manager.get_application(app_id)
    assert app.status == "approved"


async def test_review_approved_owner_role_uses_owner_label(acl_manager):
    """Approval of OWNER role → notification message contains 'Owner'."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_owner_applicant", "", Role.OWNER)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    replier.send_to_user.assert_called()
    call_args = replier.send_to_user.call_args_list[-1]
    # The message content (second arg) should contain "Owner"
    notification_text = call_args[0][1]
    assert "Owner" in notification_text


async def test_review_rejected_notifies_applicant(acl_manager):
    """Successful rejection → applicant is notified via send_to_user."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })
    replier.send_to_user.assert_called()
    call_args = replier.send_to_user.call_args_list[-1]
    assert call_args[0][0] == "ou_applicant"
    # Application should be rejected in DB
    app = await acl_manager.get_application(app_id)
    assert app.status == "rejected"


async def test_review_unknown_decision_logs_warning(acl_manager):
    """An unknown decision string → logged as warning, no error raised."""
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    # Should not raise
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "maybe",  # unknown decision
        "operator_id": "ou_admin",
    })
    # Application should remain pending
    app = await acl_manager.get_application(app_id)
    assert app.status == "pending"
    replier.send_to_user.assert_not_called()


async def test_review_approved_notification_failure_swallowed(acl_manager):
    """Exception when notifying approved applicant is caught and does not propagate."""
    replier = make_replier()
    replier.send_to_user = AsyncMock(side_effect=Exception("network error"))
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    # Should NOT raise
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_admin",
    })
    # Application was still approved despite notification failure
    app = await acl_manager.get_application(app_id)
    assert app.status == "approved"


async def test_review_rejected_notification_failure_swallowed(acl_manager):
    """Exception when notifying rejected applicant is caught and does not propagate."""
    replier = make_replier()
    replier.send_to_user = AsyncMock(side_effect=Exception("network error"))
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_applicant", "", Role.COLLABORATOR)
    # Should NOT raise
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })
    # Application was still rejected despite notification failure
    app = await acl_manager.get_application(app_id)
    assert app.status == "rejected"


async def test_review_owner_reviewer_can_approve_collaborator(acl_manager, db):
    """OWNER reviewer can approve COLLABORATOR applications."""
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    replier = make_replier()
    d = make_dispatcher(replier, acl_manager=acl_manager)
    app_id, _ = await acl_manager.create_application("ou_collab", "", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "approved",
        "operator_id": "ou_owner",
    })
    app = await acl_manager.get_application(app_id)
    assert app.status == "approved"
    replier.send_to_user.assert_called()


# ===========================================================================
# Group 4: dispatch() paths involving ACL + reply_fn
# ===========================================================================


import uuid
from unittest.mock import patch
from nextme.config.schema import AppConfig, Project
from nextme.protocol.types import Task, Reply, ReplyType


def make_task_for_dispatch(
    content="hello",
    session_id="oc_chat:ou_user",
    message_id="",
    chat_type="p2p",
):
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=session_id,
        reply_fn=AsyncMock(),
        message_id=message_id,
        chat_type=chat_type,
    )


def make_dispatcher_with_project(replier, acl_manager=None, tmp_path=None):
    """Create a dispatcher with a real project config for full dispatch tests."""
    import tempfile
    proj_path = tmp_path or tempfile.mkdtemp()
    project = Project(name="testproj", path=str(proj_path), executor="claude")
    config = AppConfig(app_id="x", app_secret="y", projects=[project])
    settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)
    fc = MagicMock()
    fc.get_replier = MagicMock(return_value=replier)
    return TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=SessionRegistry(),
        acp_registry=ACPRuntimeRegistry(),
        path_lock_registry=PathLockRegistry(),
        feishu_client=fc,
        acl_manager=acl_manager,
    )


async def test_dispatch_reply_fn_markdown_no_message_id(acl_manager, tmp_path):
    """reply_fn with MARKDOWN type and no message_id calls send_text."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    replier.reply_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="hello", message_id="", session_id="oc_chat:ou_admin")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    # Invoke the reply_fn with MARKDOWN type and no message_id
    reply = Reply(type=ReplyType.MARKDOWN, content="some markdown")
    await task.reply_fn(reply)
    replier.send_text.assert_awaited_with("oc_chat", "some markdown")
    replier.reply_text.assert_not_awaited()


async def test_dispatch_reply_fn_markdown_with_message_id(acl_manager, tmp_path):
    """reply_fn with MARKDOWN type and message_id calls reply_text."""
    replier = make_replier()
    replier.reply_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(
        content="hello",
        message_id="msg_123",
        session_id="oc_chat:ou_admin",
        chat_type="p2p",
    )
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    reply = Reply(type=ReplyType.MARKDOWN, content="some markdown")
    await task.reply_fn(reply)
    replier.reply_text.assert_awaited_with("msg_123", "some markdown", in_thread=False)


async def test_dispatch_reply_fn_unhandled_type_logs_warning(acl_manager, tmp_path):
    """reply_fn with unhandled ReplyType (REACTION) logs a warning without raising."""
    replier = make_replier()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="hello", session_id="oc_chat:ou_admin")
    with patch("nextme.core.dispatcher.SessionWorker") as MockWorker:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockWorker.return_value = mock_instance
        await d.dispatch(task)

    # REACTION is an unhandled reply type
    reply = Reply(type=ReplyType.REACTION, content="👍")
    # Should not raise
    await task.reply_fn(reply)
    # No send calls for unhandled type
    replier.send_text.assert_not_awaited()


async def test_dispatch_denied_card_exception_swallowed(acl_manager, tmp_path):
    """If build_access_denied_card raises, the exception is caught and doesn't propagate."""
    replier = make_replier()
    replier.build_access_denied_card = MagicMock(side_effect=Exception("build error"))
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    # Unauthorized user (not admin, not in DB)
    task = make_task_for_dispatch(
        content="hello",
        message_id="msg_x",
        session_id="oc_chat:ou_stranger",
    )
    # Should NOT raise
    await d.dispatch(task)
    replier.build_access_denied_card.assert_called_once_with("ou_stranger")


async def test_dispatch_denied_card_no_message_id_uses_send_card(acl_manager, tmp_path):
    """Unauthorized user with no message_id → send_card used for denied card."""
    replier = make_replier()
    replier.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')
    replier.send_card = AsyncMock()
    replier.reply_card = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(
        content="hello",
        message_id="",  # no message_id
        session_id="oc_chat:ou_stranger",
    )
    await d.dispatch(task)
    # send_card used since no message_id
    replier.send_card.assert_awaited()
    replier.reply_card.assert_not_awaited()


async def test_dispatch_whoami_no_acl_manager(tmp_path):
    """'/whoami' with no ACL manager sends open_id and role message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=None, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/whoami", session_id="oc_chat:ou_user123")
    await d.dispatch(task)
    replier.send_text.assert_awaited()
    # Check the message contains the open_id
    all_calls = [str(c) for c in replier.send_text.call_args_list]
    assert any("ou_user123" in c for c in all_calls)


async def test_dispatch_remember_no_arg(acl_manager, tmp_path):
    """'/remember' with no arg sends usage message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    # ou_admin is authorized
    task = make_task_for_dispatch(content="/remember", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    all_calls = [str(c) for c in replier.send_text.call_args_list]
    assert any("remember" in c.lower() or "用法" in c for c in all_calls)


async def test_dispatch_remember_no_memory_manager(acl_manager, tmp_path):
    """'/remember <text>' with no memory manager sends 'not enabled' message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)
    # No memory_manager → defaults to None

    task = make_task_for_dispatch(content="/remember some fact", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    all_calls = [str(c) for c in replier.send_text.call_args_list]
    assert any("未启用" in c or "not" in c.lower() for c in all_calls)


async def test_dispatch_acl_command_no_acl_manager(tmp_path):
    """'/acl' with no ACL manager sends 'not enabled' message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=None, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl list", session_id="oc_chat:ou_user")
    await d.dispatch(task)
    all_calls = [str(c) for c in replier.send_text.call_args_list]
    assert any("未启用" in c or "ACL" in c for c in all_calls)


async def test_dispatch_acl_command_insufficient_role(acl_manager, db, tmp_path):
    """'/acl pending' with collaborator role sends 'insufficient permission' message."""
    # Add collaborator user
    await db.add_user("ou_collab", Role.COLLABORATOR, "Collab", "ou_admin")
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    # ou_collab is COLLABORATOR - not allowed to use /acl pending
    task = make_task_for_dispatch(content="/acl pending", session_id="oc_chat:ou_collab")
    await d.dispatch(task)
    # Should get a permission-denied message
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("权限不足" in c for c in calls)


async def test_dispatch_acl_subcommand_unknown(acl_manager, tmp_path):
    """'/acl unknowncmd' with valid role sends unknown subcommand message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    # ou_admin is authorized and has ADMIN role
    task = make_task_for_dispatch(content="/acl unknownsub", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("未知" in c or "unknown" in c.lower() or "子命令" in c for c in calls)


async def test_dispatch_acl_add_missing_open_id(acl_manager, tmp_path):
    """'/acl add' with no target sends usage message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl add", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("add" in c.lower() or "用法" in c for c in calls)


async def test_dispatch_acl_remove_missing_open_id(acl_manager, tmp_path):
    """'/acl remove' with no target sends usage message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl remove", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("remove" in c.lower() or "用法" in c for c in calls)


async def test_dispatch_acl_approve_missing_app_id(acl_manager, tmp_path):
    """'/acl approve' with no app_id sends usage message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl approve", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("approve" in c.lower() or "用法" in c for c in calls)


async def test_dispatch_acl_approve_invalid_app_id(acl_manager, tmp_path):
    """'/acl approve notanumber' sends 'app_id must be integer' message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl approve notanumber", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("数字" in c or "number" in c.lower() or "整数" in c for c in calls)


async def test_dispatch_acl_reject_missing_app_id(acl_manager, tmp_path):
    """'/acl reject' with no app_id sends usage message."""
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl reject", session_id="oc_chat:ou_admin")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("reject" in c.lower() or "用法" in c for c in calls)


async def test_dispatch_acl_pending_with_collaborator_role_denied(acl_manager, db, tmp_path):
    """'/acl pending' with collaborator role → 权限不足 message."""
    await db.add_user("ou_collab2", Role.COLLABORATOR, "Collab2", "ou_admin")
    replier = make_replier()
    replier.send_text = AsyncMock()
    d = make_dispatcher_with_project(replier, acl_manager=acl_manager, tmp_path=tmp_path)

    task = make_task_for_dispatch(content="/acl pending", session_id="oc_chat:ou_collab2")
    await d.dispatch(task)
    calls = [str(call) for call in replier.send_text.call_args_list]
    assert any("权限不足" in c for c in calls)
