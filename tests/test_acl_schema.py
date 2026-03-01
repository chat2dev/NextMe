# tests/test_acl_schema.py
import pytest
from nextme.acl.schema import Role, AclUser, AclApplication
from nextme.config.schema import Settings
from datetime import datetime


def test_role_enum_values():
    assert Role.ADMIN.value == "admin"
    assert Role.OWNER.value == "owner"
    assert Role.COLLABORATOR.value == "collaborator"


def test_role_is_string_enum():
    # Role values work as strings in SQLite comparisons
    assert Role.OWNER == "owner"


def test_acl_user_model():
    user = AclUser(
        open_id="ou_abc",
        role=Role.OWNER,
        display_name="Alice",
        added_by="ou_admin",
        added_at=datetime(2026, 3, 1, 10, 0, 0),
    )
    assert user.open_id == "ou_abc"
    assert user.role == Role.OWNER
    assert user.display_name == "Alice"


def test_acl_application_model():
    app = AclApplication(
        id=1,
        applicant_id="ou_xyz",
        applicant_name="Bob",
        requested_role=Role.COLLABORATOR,
        status="pending",
        requested_at=datetime(2026, 3, 1, 11, 0, 0),
    )
    assert app.id == 1
    assert app.status == "pending"
    assert app.processed_at is None


def test_acl_application_invalid_status():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AclApplication(
            id=1,
            applicant_id="ou_xyz",
            requested_role=Role.COLLABORATOR,
            status="INVALID",
            requested_at=datetime(2026, 3, 1, 11, 0, 0),
        )


def test_settings_admin_users_default():
    s = Settings()
    assert s.admin_users == []


def test_settings_admin_users_set():
    s = Settings(admin_users=["ou_abc", "ou_def"])
    assert "ou_abc" in s.admin_users
    assert len(s.admin_users) == 2
