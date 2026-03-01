"""Pydantic models and enums for ACL."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class Role(str, Enum):
    """User role. ADMIN is stored in settings.json; OWNER/COLLABORATOR in nextme.db."""

    ADMIN = "admin"
    OWNER = "owner"
    COLLABORATOR = "collaborator"


class AclUser(BaseModel):
    """A row from acl_users table."""

    open_id: str
    role: Role
    display_name: str = ""
    added_by: str
    added_at: datetime


class AclApplication(BaseModel):
    """A row from acl_applications table."""

    id: int | None = None
    applicant_id: str
    applicant_name: str = ""
    requested_role: Role
    status: Literal["pending", "approved", "rejected"]
    requested_at: datetime
    processed_at: datetime | None = None
    processed_by: str | None = None
