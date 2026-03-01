"""SQLite CRUD for ACL tables in ~/.nextme/nextme.db."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from .schema import AclApplication, AclUser, Role

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path("~/.nextme").expanduser() / "nextme.db"

_CREATE_ACL_USERS = """
CREATE TABLE IF NOT EXISTS acl_users (
    open_id      TEXT PRIMARY KEY,
    role         TEXT NOT NULL CHECK(role IN ('owner', 'collaborator')),
    display_name TEXT NOT NULL DEFAULT '',
    added_by     TEXT NOT NULL,
    added_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_ACL_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS acl_applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_id    TEXT NOT NULL,
    applicant_name  TEXT NOT NULL DEFAULT '',
    requested_role  TEXT NOT NULL CHECK(requested_role IN ('owner', 'collaborator')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected')),
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at    TEXT,
    processed_by    TEXT
)
"""

_CREATE_PENDING_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_pending_per_applicant
    ON acl_applications(applicant_id)
    WHERE status = 'pending'
"""


def _row_to_user(row: aiosqlite.Row) -> AclUser:
    return AclUser(
        open_id=row["open_id"],
        role=Role(row["role"]),
        display_name=row["display_name"],
        added_by=row["added_by"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )


def _row_to_application(row: aiosqlite.Row) -> AclApplication:
    return AclApplication(
        id=row["id"],
        applicant_id=row["applicant_id"],
        applicant_name=row["applicant_name"],
        requested_role=Role(row["requested_role"]),
        status=row["status"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        processed_at=datetime.fromisoformat(row["processed_at"]) if row["processed_at"] else None,
        processed_by=row["processed_by"],
    )


class AclDb:
    """Async SQLite data layer for ACL tables."""

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database and create tables if they don't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_CREATE_ACL_USERS)
        await self._conn.execute(_CREATE_ACL_APPLICATIONS)
        await self._conn.execute(_CREATE_PENDING_INDEX)
        await self._conn.commit()
        logger.debug("AclDb: opened %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_user(self, open_id: str) -> AclUser | None:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_users WHERE open_id = ?", (open_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def add_user(
        self, open_id: str, role: Role, display_name: str, added_by: str
    ) -> None:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO acl_users (open_id, role, display_name, added_by) "
                "VALUES (?, ?, ?, ?)",
                (open_id, role.value, display_name, added_by),
            )
            await self._conn.commit()

    async def remove_user(self, open_id: str) -> bool:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM acl_users WHERE open_id = ?", (open_id,)
            )
            await self._conn.commit()
        return cur.rowcount > 0

    async def list_users(self, role: Role | None = None) -> list[AclUser]:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            if role is None:
                async with self._conn.execute(
                    "SELECT * FROM acl_users ORDER BY added_at"
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._conn.execute(
                    "SELECT * FROM acl_users WHERE role = ? ORDER BY added_at",
                    (role.value,),
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    async def create_application(
        self, applicant_id: str, applicant_name: str, requested_role: Role
    ) -> int | None:
        """Insert a new pending application.

        Returns the new row id, or ``None`` if a pending application already
        exists for this ``applicant_id`` (UNIQUE constraint on (applicant_id, status='pending')).
        """
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            # Check for existing pending application first
            async with self._conn.execute(
                "SELECT id FROM acl_applications WHERE applicant_id = ? AND status = 'pending'",
                (applicant_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                return None
            cur = await self._conn.execute(
                "INSERT INTO acl_applications (applicant_id, applicant_name, requested_role) "
                "VALUES (?, ?, ?)",
                (applicant_id, applicant_name, requested_role.value),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def get_application(self, app_id: int) -> AclApplication | None:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_applications WHERE id = ?", (app_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_application(row) if row else None

    async def get_pending_application(self, applicant_id: str) -> AclApplication | None:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_applications WHERE applicant_id = ? AND status = 'pending'",
                (applicant_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_application(row) if row else None

    async def list_pending_applications(
        self, role: Role | None = None
    ) -> list[AclApplication]:
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            if role is None:
                async with self._conn.execute(
                    "SELECT * FROM acl_applications WHERE status = 'pending' ORDER BY requested_at"
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._conn.execute(
                    "SELECT * FROM acl_applications WHERE status = 'pending' "
                    "AND requested_role = ? ORDER BY requested_at",
                    (role.value,),
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_application(r) for r in rows]

    async def update_application_status(
        self, app_id: int, status: Literal["approved", "rejected"], processed_by: str
    ) -> bool:
        """Update status of a *pending* application.

        Returns ``True`` if a row was updated (i.e. the application was pending),
        ``False`` if the application was already processed or doesn't exist.
        """
        if self._conn is None:
            raise RuntimeError("AclDb.open() must be called before use")
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE acl_applications SET status = ?, processed_by = ?, "
                "processed_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (status, processed_by, app_id),
            )
            await self._conn.commit()
        return cur.rowcount > 0
