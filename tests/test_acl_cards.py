"""Tests for ACL-related card builders and send_to_user in FeishuReplier."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from nextme.feishu.reply import FeishuReplier
from nextme.acl.schema import AclUser, Role, AclApplication


@pytest.fixture
def replier():
    client = MagicMock()
    client.im = MagicMock()
    client.im.v1 = MagicMock()
    client.im.v1.message = MagicMock()
    client.im.v1.message.acreate = AsyncMock(
        return_value=MagicMock(
            success=MagicMock(return_value=True),
            data=MagicMock(message_id="msg_123"),
        )
    )
    return FeishuReplier(client)


async def test_send_to_user_returns_message_id(replier):
    msg_id = await replier.send_to_user("ou_abc", '{"text":"hello"}', "text")
    assert msg_id == "msg_123"
    assert replier._client.im.v1.message.acreate.called


def test_build_access_denied_card_contains_open_id(replier):
    card_json = replier.build_access_denied_card("ou_xyz")
    card = json.loads(card_json)
    body_text = json.dumps(card["body"])
    assert "ou_xyz" in body_text


def test_build_access_denied_card_has_apply_buttons(replier):
    card_json = replier.build_access_denied_card("ou_xyz")
    card = json.loads(card_json)
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    assert len(buttons) == 2
    roles = {b["value"]["role"] for b in buttons}
    assert "owner" in roles
    assert "collaborator" in roles
    for b in buttons:
        assert b["value"]["open_id"] == "ou_xyz"
        assert b["value"]["action"] == "acl_apply"


def test_build_acl_review_notification_card(replier):
    card_json = replier.build_acl_review_notification_card(
        app_id=42,
        applicant_name="Bob",
        applicant_id="ou_bob",
        requested_role="collaborator",
    )
    card = json.loads(card_json)
    body_text = json.dumps(card["body"])
    assert "Bob" in body_text
    assert "ou_bob" in body_text
    assert "42" in body_text
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    assert len(buttons) == 2
    decisions = {b["value"]["decision"] for b in buttons}
    assert "approved" in decisions
    assert "rejected" in decisions
    for b in buttons:
        assert b["value"]["app_id"] == "42"
        assert b["value"]["action"] == "acl_review"


def test_build_whoami_card_authorized(replier):
    user = AclUser(
        open_id="ou_me",
        role=Role.OWNER,
        display_name="Me",
        added_by="ou_admin",
        added_at=datetime(2026, 3, 1),
    )
    card_json = replier.build_whoami_card("ou_me", Role.OWNER, user)
    card = json.loads(card_json)
    body_text = json.dumps(card["body"])
    assert "ou_me" in body_text
    assert "owner" in body_text.lower()


def test_build_whoami_card_unauthorized(replier):
    card_json = replier.build_whoami_card("ou_guest", None, None)
    card = json.loads(card_json)
    body_text = json.dumps(card["body"])
    assert "ou_guest" in body_text
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    assert len(buttons) == 2


def test_build_acl_list_card(replier):
    owners = [AclUser(open_id="ou_o", role=Role.OWNER, display_name="Owner", added_by="sys", added_at=datetime(2026,1,1))]
    collabs = [AclUser(open_id="ou_c", role=Role.COLLABORATOR, display_name="Collab", added_by="ou_o", added_at=datetime(2026,2,1))]
    card_json = replier.build_acl_list_card(["ou_admin"], owners, collabs)
    card = json.loads(card_json)
    body_text = json.dumps(card["body"])
    assert "ou_admin" in body_text
    assert "ou_o" in body_text
    assert "ou_c" in body_text


def test_build_acl_pending_card_empty(replier):
    card_json = replier.build_acl_pending_card([], Role.ADMIN)
    card = json.loads(card_json)
    body_text = json.dumps(card["body"], ensure_ascii=False)
    assert "待审批" in body_text


def test_build_acl_pending_card_admin_sees_buttons(replier):
    """ADMIN viewer should see approve/reject buttons for all applications."""
    app = AclApplication(
        id=5,
        applicant_id="ou_x",
        applicant_name="X",
        requested_role=Role.OWNER,
        status="pending",
        requested_at=datetime(2026, 3, 1, 10, 0),
    )
    card_json = replier.build_acl_pending_card([app], Role.ADMIN)
    card = json.loads(card_json)
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    assert len(buttons) == 2
    decisions = {b["value"]["decision"] for b in buttons}
    assert "approved" in decisions
    assert "rejected" in decisions


def test_build_acl_pending_card_owner_sees_collab_buttons_only(replier):
    """OWNER viewer should see buttons for COLLABORATOR apps but NOT OWNER apps."""
    collab_app = AclApplication(
        id=1,
        applicant_id="ou_c",
        applicant_name="C",
        requested_role=Role.COLLABORATOR,
        status="pending",
        requested_at=datetime(2026, 3, 1, 10, 0),
    )
    owner_app = AclApplication(
        id=2,
        applicant_id="ou_o",
        applicant_name="O",
        requested_role=Role.OWNER,
        status="pending",
        requested_at=datetime(2026, 3, 1, 11, 0),
    )
    card_json = replier.build_acl_pending_card([collab_app, owner_app], Role.OWNER)
    card = json.loads(card_json)
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    # Should have buttons for collab_app (id=1) but not for owner_app (id=2)
    assert len(buttons) == 2
    app_ids = {b["value"]["app_id"] for b in buttons}
    assert "1" in app_ids
    assert "2" not in app_ids
