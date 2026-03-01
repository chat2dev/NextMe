"""High-level ACL business logic layer."""
from __future__ import annotations

import logging

from .db import AclDb
from .schema import AclApplication, AclUser, Role

logger = logging.getLogger(__name__)


class AclManager:
    """Orchestrates ACL checks, user management, and application workflow."""

    def __init__(self, db: AclDb, admin_users: list[str]) -> None:
        self._db = db
        self._admin_users: set[str] = set(admin_users)

    def get_admin_ids(self) -> list[str]:
        return list(self._admin_users)

    # ------------------------------------------------------------------
    # Role resolution
    # ------------------------------------------------------------------

    async def get_role(self, open_id: str) -> Role | None:
        """Return the role for *open_id*, or None if not authorized."""
        if open_id in self._admin_users:
            return Role.ADMIN
        user = await self._db.get_user(open_id)
        return user.role if user else None

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    async def get_user(self, open_id: str) -> AclUser | None:
        return await self._db.get_user(open_id)

    async def add_user(
        self,
        open_id: str,
        role: Role,
        display_name: str = "",
        added_by: str = "",
    ) -> None:
        if role == Role.ADMIN:
            raise ValueError("Cannot add admin via command; edit settings.json")
        await self._db.add_user(open_id, role, display_name, added_by)

    async def remove_user(self, open_id: str) -> bool:
        if open_id in self._admin_users:
            raise ValueError("Cannot remove admin via command; edit settings.json")
        return await self._db.remove_user(open_id)

    async def list_users(self, role: Role | None = None) -> list[AclUser]:
        return await self._db.list_users(role)

    # ------------------------------------------------------------------
    # Application workflow
    # ------------------------------------------------------------------

    async def create_application(
        self,
        applicant_id: str,
        applicant_name: str,
        requested_role: Role,
    ) -> tuple[int | None, AclApplication | None]:
        """Create a new pending application.

        Returns:
            (new_id, None) on success.
            (None, existing) when a pending application already exists.
        """
        existing = await self._db.get_pending_application(applicant_id)
        if existing is not None:
            return None, existing
        new_id = await self._db.create_application(
            applicant_id, applicant_name, requested_role
        )
        return new_id, None

    async def approve(
        self, app_id: int, reviewer_id: str
    ) -> AclApplication | None:
        """Approve application and add user to acl_users. Returns updated app or None."""
        app = await self._db.get_application(app_id)
        if app is None or app.status != "pending":
            return None
        updated = await self._db.update_application_status(app_id, "approved", reviewer_id)
        if not updated:
            return None
        await self._db.add_user(
            open_id=app.applicant_id,
            role=app.requested_role,
            display_name=app.applicant_name,
            added_by=reviewer_id,
        )
        return await self._db.get_application(app_id)

    async def reject(
        self, app_id: int, reviewer_id: str
    ) -> AclApplication | None:
        """Reject application. Returns updated app or None."""
        app = await self._db.get_application(app_id)
        if app is None or app.status != "pending":
            return None
        updated = await self._db.update_application_status(app_id, "rejected", reviewer_id)
        if not updated:
            return None
        return await self._db.get_application(app_id)

    async def list_pending(self, reviewer_role: Role) -> list[AclApplication]:
        """Return pending applications visible to reviewer_role."""
        if reviewer_role == Role.ADMIN:
            return await self._db.list_pending_applications()
        if reviewer_role == Role.OWNER:
            return await self._db.list_pending_applications(Role.COLLABORATOR)
        return []

    async def get_reviewers_for_role(self, requested_role: Role) -> list[str]:
        """Return open_ids of users who can review an application for requested_role."""
        if requested_role == Role.OWNER:
            return list(self._admin_users)
        # COLLABORATOR: owners + admins
        owners = await self._db.list_users(Role.OWNER)
        return [u.open_id for u in owners] + list(self._admin_users)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def can_review(self, reviewer_role: Role, requested_role: Role) -> bool:
        if reviewer_role == Role.ADMIN:
            return True
        if reviewer_role == Role.OWNER and requested_role == Role.COLLABORATOR:
            return True
        return False

    def can_add(self, actor_role: Role, target_role: Role) -> bool:
        if actor_role == Role.ADMIN:
            return target_role in (Role.OWNER, Role.COLLABORATOR)
        if actor_role == Role.OWNER:
            return target_role == Role.COLLABORATOR
        return False

    def can_remove(self, actor_role: Role, target: AclUser) -> bool:
        if target.open_id in self._admin_users:
            return False
        if actor_role == Role.ADMIN:
            return True
        if actor_role == Role.OWNER:
            return target.role == Role.COLLABORATOR
        return False
