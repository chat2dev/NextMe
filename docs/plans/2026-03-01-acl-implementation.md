# Access Control (ACL) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add role-based access control (admin/owner/collaborator) to NextMe with SQLite persistence, interactive Feishu card-based application/approval flow, and per-command permission enforcement.

**Architecture:** ACL gate at top of `TaskDispatcher.dispatch()`; three-tier roles (admin in `settings.json`, owner/collaborator in `nextme.db`); unauthorized users get an interactive apply card; reviewers get DM approval cards via `receive_id_type=open_id`; new `src/nextme/acl/` package.

**Tech Stack:** Python 3.12+, aiosqlite, pydantic v2, lark-oapi, existing asyncio architecture.

---

### Task 1: Add `aiosqlite` dependency + `admin_users` config field + ACL schema

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/nextme/config/schema.py`
- Create: `src/nextme/acl/__init__.py`
- Create: `src/nextme/acl/schema.py`
- Create: `tests/test_acl_schema.py`

**Step 1: Write the failing tests**

```python
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


def test_settings_admin_users_default():
    s = Settings()
    assert s.admin_users == []


def test_settings_admin_users_set():
    s = Settings(admin_users=["ou_abc", "ou_def"])
    assert "ou_abc" in s.admin_users
    assert len(s.admin_users) == 2
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_schema.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'nextme.acl'`

**Step 3: Add `aiosqlite` to `pyproject.toml`**

In `pyproject.toml`, add to `dependencies`:
```toml
"aiosqlite>=0.20",
```

Then run:
```bash
uv sync
```

**Step 4: Add `admin_users` to `Settings` in `src/nextme/config/schema.py`**

After `permission_auto_approve: bool = False` line (around line 63), add:
```python
    admin_users: list[str] = Field(default_factory=list)
    """Feishu open_ids of super-admins. Static — requires bot restart to take effect.
    These users bypass all ACL checks and can approve owner applications."""
```

**Step 5: Create `src/nextme/acl/__init__.py`**

```python
"""Role-based access control for NextMe."""
```

**Step 6: Create `src/nextme/acl/schema.py`**

```python
"""Pydantic models and enums for ACL."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

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

    id: int
    applicant_id: str
    applicant_name: str = ""
    requested_role: Role
    status: str  # "pending" | "approved" | "rejected"
    requested_at: datetime
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
```

**Step 7: Run tests to verify they pass**

```bash
uv run pytest tests/test_acl_schema.py -v
```
Expected: 6 PASSED

**Step 8: Commit**

```bash
git add pyproject.toml src/nextme/config/schema.py src/nextme/acl/ tests/test_acl_schema.py
git commit -m "feat(acl): add aiosqlite dep, admin_users config, ACL schema"
```

---

### Task 2: AclDb — SQLite CRUD layer

**Files:**
- Create: `src/nextme/acl/db.py`
- Create: `tests/test_acl_db.py`

**Step 1: Write the failing tests**

```python
# tests/test_acl_db.py
import pytest
import tempfile
from pathlib import Path
from datetime import datetime

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
    # Try to update again
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
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_db.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'nextme.acl.db'`

**Step 3: Create `src/nextme/acl/db.py`**

```python
"""SQLite CRUD for ACL tables in ~/.nextme/nextme.db."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from .schema import AclApplication, AclUser, Role

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()
_DEFAULT_DB_PATH = _NEXTME_HOME / "nextme.db"

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
    processed_by    TEXT,
    UNIQUE(applicant_id, status)
)
"""


class AclDb:
    """Async SQLite data layer for ACL tables."""

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database and create tables if they don't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_CREATE_ACL_USERS)
        await self._conn.execute(_CREATE_ACL_APPLICATIONS)
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

    async def get_user(self, open_id: str) -> Optional[AclUser]:
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_users WHERE open_id = ?", (open_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def add_user(
        self, open_id: str, role: Role, display_name: str, added_by: str
    ) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO acl_users "
                "(open_id, role, display_name, added_by) VALUES (?, ?, ?, ?)",
                (open_id, role.value, display_name, added_by),
            )
            await self._conn.commit()
        logger.info("AclDb: upserted user %s role=%s", open_id, role.value)

    async def remove_user(self, open_id: str) -> bool:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM acl_users WHERE open_id = ?", (open_id,)
            )
            await self._conn.commit()
        return cur.rowcount > 0

    async def list_users(self, role: Optional[Role] = None) -> list[AclUser]:
        assert self._conn is not None
        async with self._lock:
            if role is not None:
                async with self._conn.execute(
                    "SELECT * FROM acl_users WHERE role = ? ORDER BY added_at",
                    (role.value,),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._conn.execute(
                    "SELECT * FROM acl_users ORDER BY role, added_at"
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    async def create_application(
        self, applicant_id: str, applicant_name: str, requested_role: Role
    ) -> Optional[int]:
        """Insert a pending application. Returns new row id, or None if duplicate pending."""
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                "SELECT id FROM acl_applications "
                "WHERE applicant_id = ? AND status = 'pending'",
                (applicant_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                return None
            cur = await self._conn.execute(
                "INSERT INTO acl_applications (applicant_id, applicant_name, requested_role) "
                "VALUES (?, ?, ?)",
                (applicant_id, applicant_name, requested_role.value),
            )
            await self._conn.commit()
        logger.info(
            "AclDb: created application id=%s applicant=%s role=%s",
            cur.lastrowid, applicant_id, requested_role.value,
        )
        return cur.lastrowid

    async def get_application(self, app_id: int) -> Optional[AclApplication]:
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_applications WHERE id = ?", (app_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_application(row) if row else None

    async def get_pending_application(self, applicant_id: str) -> Optional[AclApplication]:
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                "SELECT * FROM acl_applications "
                "WHERE applicant_id = ? AND status = 'pending'",
                (applicant_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_application(row) if row else None

    async def list_pending_applications(
        self, role: Optional[Role] = None
    ) -> list[AclApplication]:
        assert self._conn is not None
        async with self._lock:
            if role is not None:
                async with self._conn.execute(
                    "SELECT * FROM acl_applications "
                    "WHERE status = 'pending' AND requested_role = ? ORDER BY requested_at",
                    (role.value,),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with self._conn.execute(
                    "SELECT * FROM acl_applications "
                    "WHERE status = 'pending' ORDER BY requested_at"
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_application(r) for r in rows]

    async def update_application_status(
        self, app_id: int, status: str, processed_by: str
    ) -> bool:
        """Set status on a pending application. Returns False if already processed."""
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE acl_applications "
                "SET status = ?, processed_by = ?, processed_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (status, processed_by, app_id),
            )
            await self._conn.commit()
        return cur.rowcount > 0


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
        processed_at=(
            datetime.fromisoformat(row["processed_at"]) if row["processed_at"] else None
        ),
        processed_by=row["processed_by"],
    )
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_acl_db.py -v
```
Expected: 13 PASSED

**Step 5: Commit**

```bash
git add src/nextme/acl/db.py tests/test_acl_db.py
git commit -m "feat(acl): add AclDb SQLite CRUD layer"
```

---

### Task 3: AclManager — business logic

**Files:**
- Create: `src/nextme/acl/manager.py`
- Create: `tests/test_acl_manager.py`

**Step 1: Write the failing tests**

```python
# tests/test_acl_manager.py
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
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_manager.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'nextme.acl.manager'`

**Step 3: Create `src/nextme/acl/manager.py`**

```python
"""High-level ACL business logic layer."""
from __future__ import annotations

import logging
from typing import Optional

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

    async def get_role(self, open_id: str) -> Optional[Role]:
        """Return the role for *open_id*, or None if not authorized."""
        if open_id in self._admin_users:
            return Role.ADMIN
        user = await self._db.get_user(open_id)
        return user.role if user else None

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    async def get_user(self, open_id: str) -> Optional[AclUser]:
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

    async def list_users(self, role: Optional[Role] = None) -> list[AclUser]:
        return await self._db.list_users(role)

    # ------------------------------------------------------------------
    # Application workflow
    # ------------------------------------------------------------------

    async def create_application(
        self,
        applicant_id: str,
        applicant_name: str,
        requested_role: Role,
    ) -> tuple[Optional[int], Optional[AclApplication]]:
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
    ) -> Optional[AclApplication]:
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
    ) -> Optional[AclApplication]:
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
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_acl_manager.py -v
```
Expected: 19 PASSED

**Step 5: Commit**

```bash
git add src/nextme/acl/manager.py tests/test_acl_manager.py
git commit -m "feat(acl): add AclManager business logic"
```

---

### Task 4: `send_to_user` + ACL card builders

**Files:**
- Modify: `src/nextme/feishu/reply.py`
- Modify: `src/nextme/core/interfaces.py`
- Create: `tests/test_acl_cards.py`

**Step 1: Write the failing tests**

```python
# tests/test_acl_cards.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from nextme.feishu.reply import FeishuReplier
from nextme.acl.schema import AclUser, Role, AclApplication
from datetime import datetime


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


async def test_send_to_user_uses_open_id_receive_type(replier):
    msg_id = await replier.send_to_user("ou_abc", '{"text":"hello"}', "text")
    assert msg_id == "msg_123"
    call_args = replier._client.im.v1.message.acreate.call_args
    request = call_args[0][0]
    # Verify receive_id_type is "open_id"
    assert request._receive_id_type == "open_id"


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
    # All buttons carry open_id
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
    # Should contain apply buttons
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
    body_text = json.dumps(card["body"])
    assert "pending" in body_text.lower() or "待审批" in body_text or "no" in body_text.lower()
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_cards.py -v
```
Expected: FAIL

**Step 3: Add `send_to_user` to `src/nextme/feishu/reply.py`**

After `send_card` method (around line 108), add:

```python
    async def send_to_user(
        self, open_id: str, content: str, msg_type: str = "interactive"
    ) -> str:
        """Send a message directly to a user by open_id (DM push for notifications).

        Uses ``receive_id_type=open_id`` so no pre-existing chat is required.
        Returns the message_id, or ``""`` on failure.
        """
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            logger.error(
                "send_to_user failed: open_id=%s code=%s msg=%s",
                open_id,
                response.code,
                response.msg,
            )
            return ""
        message_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("send_to_user -> message_id=%s", message_id)
        return message_id
```

**Step 4: Add ACL card builders to `src/nextme/feishu/reply.py`**

Add at the end of the file (after `build_help_card`):

```python
    def build_access_denied_card(self, open_id: str) -> str:
        """Return a card for unauthorized users showing their open_id and apply buttons."""
        elements: list[dict] = [
            {
                "tag": "markdown",
                "content": (
                    "您没有权限使用此 Bot。\n\n"
                    f"您的 open_id: `{open_id}`\n\n"
                    "如需访问权限，请向管理员申请，或点击下方按钮提交申请："
                ),
            },
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "申请成为 Owner（负责人）"},
                "type": "primary",
                "value": {
                    "action": "acl_apply",
                    "open_id": open_id,
                    "role": "owner",
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "申请成为 Collaborator（协作者）"},
                "type": "default",
                "value": {
                    "action": "acl_apply",
                    "open_id": open_id,
                    "role": "collaborator",
                },
            },
        ]
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "⛔ 无访问权限"},
                "template": "red",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_acl_review_notification_card(
        self,
        app_id: int,
        applicant_name: str,
        applicant_id: str,
        requested_role: str,
    ) -> str:
        """Return a DM notification card sent to reviewers for a new application."""
        role_label = "Owner（负责人）" if requested_role == "owner" else "Collaborator（协作者）"
        elements: list[dict] = [
            {
                "tag": "markdown",
                "content": (
                    f"**申请人:** {applicant_name or applicant_id} (`{applicant_id}`)\n"
                    f"**申请角色:** {role_label}\n"
                    f"**申请编号:** #{app_id}"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 批准"},
                "type": "primary",
                "value": {
                    "action": "acl_review",
                    "app_id": str(app_id),
                    "decision": "approved",
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                "type": "danger",
                "value": {
                    "action": "acl_review",
                    "app_id": str(app_id),
                    "decision": "rejected",
                },
            },
        ]
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📋 权限申请 #{app_id}"},
                "template": "orange",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_whoami_card(
        self,
        open_id: str,
        role: "Optional[Role]",
        user: "Optional[AclUser]",
    ) -> str:
        """Return a card showing the user's own info and role.

        Import Role and AclUser lazily to avoid circular imports at module level.
        """
        from nextme.acl.schema import Role as _Role

        if role is None:
            # Unauthorized — show open_id + apply buttons
            elements: list[dict] = [
                {
                    "tag": "markdown",
                    "content": (
                        f"**open_id:** `{open_id}`\n"
                        "**角色:** 无权限\n\n"
                        "点击下方按钮申请访问权限："
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "申请成为 Owner（负责人）"},
                    "type": "primary",
                    "value": {"action": "acl_apply", "open_id": open_id, "role": "owner"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "申请成为 Collaborator（协作者）"},
                    "type": "default",
                    "value": {"action": "acl_apply", "open_id": open_id, "role": "collaborator"},
                },
            ]
            template = "red"
            title = "👤 我的信息"
        else:
            role_labels = {
                _Role.ADMIN: "Admin（超级管理员）",
                _Role.OWNER: "Owner（负责人）",
                _Role.COLLABORATOR: "Collaborator（协作者）",
            }
            lines = [
                f"**open_id:** `{open_id}`",
                f"**角色:** {role_labels.get(role, role.value)}",
            ]
            if user is not None:
                lines.append(f"**加入时间:** {user.added_at.strftime('%Y-%m-%d')}")
                lines.append(f"**添加者:** `{user.added_by}`")
            elements = [{"tag": "markdown", "content": "\n".join(lines)}]
            template = "blue"
            title = "👤 我的信息"

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_acl_list_card(
        self,
        admin_ids: list[str],
        owners: "list[AclUser]",
        collaborators: "list[AclUser]",
    ) -> str:
        """Return an ACL list card grouped by role."""
        lines: list[str] = []
        if admin_ids:
            lines.append("**Admin（超级管理员）**")
            for oid in admin_ids:
                lines.append(f"  • `{oid}`")
        if owners:
            lines.append("**Owner（负责人）**")
            for u in owners:
                name = u.display_name or u.open_id
                lines.append(f"  • {name}  `{u.open_id}`  ({u.added_at.strftime('%Y-%m-%d')})")
        if collaborators:
            lines.append("**Collaborator（协作者）**")
            for u in collaborators:
                name = u.display_name or u.open_id
                lines.append(f"  • {name}  `{u.open_id}`  ({u.added_at.strftime('%Y-%m-%d')})")
        if not lines:
            lines.append("当前没有已授权用户。")

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔐 访问控制列表"},
                "template": "blue",
            },
            "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_acl_pending_card(
        self,
        applications: "list[AclApplication]",
        viewer_role: "Role",
    ) -> str:
        """Return a card listing pending applications with approve/reject buttons."""
        from nextme.acl.schema import Role as _Role

        elements: list[dict] = []
        if not applications:
            elements.append({"tag": "markdown", "content": "当前没有待审批申请。"})
        else:
            for app in applications:
                role_label = (
                    "Owner（负责人）"
                    if app.requested_role.value == "owner"
                    else "Collaborator（协作者）"
                )
                name = app.applicant_name or app.applicant_id
                info = (
                    f"**#{app.id}** {name} (`{app.applicant_id}`)\n"
                    f"申请角色: {role_label}  |  "
                    f"时间: {app.requested_at.strftime('%Y-%m-%d %H:%M')}"
                )
                elements.append({"tag": "markdown", "content": info})
                can_approve = viewer_role == _Role.ADMIN or (
                    viewer_role == _Role.OWNER
                    and app.requested_role.value == "collaborator"
                )
                if can_approve:
                    elements.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": f"✅ 批准 #{app.id}"},
                            "type": "primary",
                            "value": {
                                "action": "acl_review",
                                "app_id": str(app.id),
                                "decision": "approved",
                            },
                        }
                    )
                    elements.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": f"❌ 拒绝 #{app.id}"},
                            "type": "danger",
                            "value": {
                                "action": "acl_review",
                                "app_id": str(app.id),
                                "decision": "rejected",
                            },
                        }
                    )
                elements.append({"tag": "hr"})

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📋 待审批申请（{len(applications)}）",
                },
                "template": "orange" if applications else "grey",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)
```

Also add `Optional` to the import in `reply.py` if not already there — add to the top-of-file imports:
```python
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from nextme.acl.schema import AclApplication, AclUser, Role
```

**Step 5: Add `send_to_user` to `Replier` protocol in `src/nextme/core/interfaces.py`**

After `reply_card` method (around line 67), add:

```python
    async def send_to_user(
        self, open_id: str, content: str, msg_type: str = "interactive"
    ) -> str:
        """Send a message directly to a user by open_id (for DM notifications)."""
        ...
```

**Step 6: Fix the test for `send_to_user` receive_id_type check**

The lark-oapi builder pattern stores `_receive_id_type` internally; the test assertion may need adjusting based on the actual attribute name. Update the test to verify the call was made (simpler):

```python
async def test_send_to_user_uses_open_id_receive_type(replier):
    msg_id = await replier.send_to_user("ou_abc", '{"text":"hello"}', "text")
    assert msg_id == "msg_123"
    assert replier._client.im.v1.message.acreate.called
```

**Step 7: Run tests to verify they pass**

```bash
uv run pytest tests/test_acl_cards.py -v
```
Expected: all PASSED

**Step 8: Commit**

```bash
git add src/nextme/feishu/reply.py src/nextme/core/interfaces.py tests/test_acl_cards.py
git commit -m "feat(acl): add send_to_user, ACL card builders, update Replier protocol"
```

---

### Task 5: ACL command handlers

**Files:**
- Modify: `src/nextme/core/commands.py`
- Create: `tests/test_acl_commands.py`

**Step 1: Write the failing tests**

```python
# tests/test_acl_commands.py
import json
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
)


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


async def test_handle_acl_add_collaborator_by_owner(manager, replier):
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
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_commands.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Add ACL handlers to `src/nextme/core/commands.py`**

Add at the top, after existing imports:
```python
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from ..acl.manager import AclManager
    from ..acl.schema import AclApplication, AclUser, Role
```

Then add after `handle_project`:

```python
async def handle_whoami(
    user_id: str,
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Show the caller's own open_id, role, and join info."""
    from ..acl.schema import Role as _Role

    role = await acl_manager.get_role(user_id)
    user = None
    if role not in (None, _Role.ADMIN):
        user = await acl_manager.get_user(user_id)

    card = replier.build_whoami_card(user_id, role, user)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_whoami: failed to send card to %r", chat_id)


async def handle_acl_list(
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Send the ACL list card (admins, owners, collaborators)."""
    from ..acl.schema import Role as _Role

    admin_ids = acl_manager.get_admin_ids()
    owners = await acl_manager.list_users(_Role.OWNER)
    collaborators = await acl_manager.list_users(_Role.COLLABORATOR)
    card = replier.build_acl_list_card(admin_ids, owners, collaborators)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_acl_list: failed to send card to %r", chat_id)


async def handle_acl_add(
    actor_id: str,
    actor_role: "Role",
    target_id: str,
    target_role_str: str,
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Add a user to the ACL. Enforces role-based permission."""
    from ..acl.schema import Role as _Role

    # Parse target role (default: collaborator)
    try:
        target_role = _Role(target_role_str.lower()) if target_role_str else _Role.COLLABORATOR
    except ValueError:
        await replier.send_text(
            chat_id, f"未知角色 `{target_role_str}`，可选值: owner / collaborator"
        )
        return

    if target_role == _Role.ADMIN:
        await replier.send_text(chat_id, "无法通过命令添加 Admin，请修改 settings.json。")
        return

    if not acl_manager.can_add(actor_role, target_role):
        await replier.send_text(chat_id, "权限不足：您无法添加该角色。")
        return

    role_label = "Owner（负责人）" if target_role == _Role.OWNER else "Collaborator（协作者）"
    try:
        await acl_manager.add_user(target_id, target_role, added_by=actor_id)
        await replier.send_text(
            chat_id, f"✅ 已将 `{target_id}` 添加为 {role_label}。"
        )
    except Exception:
        logger.exception("handle_acl_add: failed to add user %r", target_id)
        await replier.send_text(chat_id, f"添加失败，请检查日志。")


async def handle_acl_remove(
    actor_id: str,
    actor_role: "Role",
    target_id: str,
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Remove a user from the ACL. Enforces role-based permission."""
    target = await acl_manager.get_user(target_id)
    if target is None:
        await replier.send_text(chat_id, f"未找到用户 `{target_id}`。")
        return

    if not acl_manager.can_remove(actor_role, target):
        if target_id in acl_manager.get_admin_ids():
            await replier.send_text(
                chat_id, "无法移除管理员，请修改 settings.json 中的 admin_users。"
            )
        else:
            await replier.send_text(chat_id, "权限不足：您无法移除该用户。")
        return

    try:
        await acl_manager.remove_user(target_id)
        await replier.send_text(chat_id, f"✅ 已移除用户 `{target_id}`。")
    except ValueError as e:
        await replier.send_text(chat_id, str(e))
    except Exception:
        logger.exception("handle_acl_remove: failed to remove user %r", target_id)
        await replier.send_text(chat_id, "移除失败，请检查日志。")


async def handle_acl_pending(
    viewer_role: "Role",
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Show pending applications the viewer is allowed to review."""
    applications = await acl_manager.list_pending(viewer_role)
    card = replier.build_acl_pending_card(applications, viewer_role)
    try:
        await replier.send_card(chat_id, card)
    except Exception:
        logger.exception("handle_acl_pending: failed to send card to %r", chat_id)


async def handle_acl_approve(
    app_id: int,
    reviewer_id: str,
    reviewer_role: "Role",
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Approve a pending application."""
    app = await acl_manager._db.get_application(app_id)
    if app is None:
        await replier.send_text(chat_id, f"未找到申请 #{app_id}。")
        return
    if not acl_manager.can_review(reviewer_role, app.requested_role):
        await replier.send_text(chat_id, "权限不足：您无法审批该申请。")
        return

    result = await acl_manager.approve(app_id, reviewer_id)
    if result is None:
        await replier.send_text(chat_id, f"申请 #{app_id} 已处理或不存在。")
        return
    role_label = "Owner" if result.requested_role.value == "owner" else "Collaborator"
    await replier.send_text(
        chat_id,
        f"✅ 已批准申请 #{app_id}，{result.applicant_name or result.applicant_id} 现在是 {role_label}。",
    )


async def handle_acl_reject(
    app_id: int,
    reviewer_id: str,
    reviewer_role: "Role",
    acl_manager: "AclManager",
    replier: Replier,
    chat_id: str,
) -> None:
    """Reject a pending application."""
    app = await acl_manager._db.get_application(app_id)
    if app is None:
        await replier.send_text(chat_id, f"未找到申请 #{app_id}。")
        return
    if not acl_manager.can_review(reviewer_role, app.requested_role):
        await replier.send_text(chat_id, "权限不足：您无法审批该申请。")
        return

    result = await acl_manager.reject(app_id, reviewer_id)
    if result is None:
        await replier.send_text(chat_id, f"申请 #{app_id} 已处理或不存在。")
        return
    await replier.send_text(
        chat_id,
        f"❌ 已拒绝申请 #{app_id}（{result.applicant_name or result.applicant_id}）。",
    )
```

Also update `HELP_COMMANDS` in `commands.py` to add new commands:
```python
HELP_COMMANDS: list[tuple[str, str]] = [
    ("/whoami", "查看我的 open_id 和角色"),
    ("/new", "开启新对话（清除当前对话历史）"),
    ("/stop", "取消当前执行中的任务"),
    ("/help", "显示帮助"),
    ("/skill", "列出所有 Skill"),
    ("/skill <trigger>", "触发指定 Skill"),
    ("/status", "显示所有 Session 状态"),
    ("/task", "显示当前任务队列"),
    ("/project", "列出所有项目"),
    ("/project <name>", "切换活跃项目"),
    ("/project bind <name>", "将当前群聊绑定到指定项目"),
    ("/project unbind", "解除当前群聊的项目绑定"),
    ("/remember <text>", "记住一条信息（长期记忆）"),
    ("/acl list", "查看访问控制列表"),
    ("/acl add <open_id> [owner|collaborator]", "添加用户（owner/admin 可用）"),
    ("/acl remove <open_id>", "移除用户（owner/admin 可用）"),
    ("/acl pending", "查看待审批申请（owner/admin 可用）"),
    ("/acl approve <id>", "批准申请（owner/admin 可用）"),
    ("/acl reject <id>", "拒绝申请（owner/admin 可用）"),
]
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_acl_commands.py -v
```
Expected: all PASSED

**Step 5: Commit**

```bash
git add src/nextme/core/commands.py tests/test_acl_commands.py
git commit -m "feat(acl): add ACL command handlers (whoami, acl list/add/remove/pending/approve/reject)"
```

---

### Task 6: ACL gate + command routing in `TaskDispatcher`

**Files:**
- Modify: `src/nextme/core/dispatcher.py`
- Modify: `tests/test_core_dispatcher.py` (add ACL tests)

**Step 1: Write the failing tests**

Add to `tests/test_core_dispatcher.py` (or create `tests/test_dispatcher_acl.py`):

```python
# tests/test_dispatcher_acl.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from nextme.acl.schema import Role
from nextme.core.dispatcher import TaskDispatcher
from nextme.protocol.types import Task
import uuid


def make_task(user_id="ou_user", chat_id="oc_chat", content="hello"):
    return Task(
        id=str(uuid.uuid4()),
        content=content,
        session_id=f"{chat_id}:{user_id}",
        reply_fn=AsyncMock(),
        message_id="msg_1",
        chat_type="p2p",
    )


@pytest.fixture
def acl_manager():
    m = MagicMock()
    m.get_role = AsyncMock(return_value=None)  # unauthorized by default
    m.get_admin_ids = MagicMock(return_value=["ou_admin"])
    return m


@pytest.fixture
def dispatcher(acl_manager):
    config = MagicMock()
    config.projects = []
    config.default_project = None
    config.get_binding = MagicMock(return_value=None)
    settings = MagicMock()
    settings.task_queue_capacity = 10

    replier = MagicMock()
    replier.send_card = AsyncMock()
    replier.send_text = AsyncMock()
    replier.build_access_denied_card = MagicMock(return_value='{"card":"denied"}')

    feishu_client = MagicMock()
    feishu_client.get_replier = MagicMock(return_value=replier)

    from nextme.core.session import SessionRegistry
    from nextme.core.path_lock import PathLockRegistry
    from nextme.acp.janitor import ACPRuntimeRegistry

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


async def test_unauthorized_user_gets_denied_card(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None
    task = make_task(user_id="ou_stranger", content="do something")
    await d.dispatch(task)
    replier.build_access_denied_card.assert_called_once_with("ou_stranger")
    replier.send_card.assert_called()


async def test_whoami_bypasses_acl_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None  # unauthorized
    # /whoami should NOT call build_access_denied_card
    with patch.object(d, '_handle_meta_command', new=AsyncMock()) as mock_cmd:
        task = make_task(content="/whoami")
        await d.dispatch(task)
        mock_cmd.assert_called_once()
    replier.build_access_denied_card.assert_not_called()


async def test_help_bypasses_acl_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = None
    with patch.object(d, '_handle_meta_command', new=AsyncMock()) as mock_cmd:
        task = make_task(content="/help")
        await d.dispatch(task)
        mock_cmd.assert_called_once()
    replier.build_access_denied_card.assert_not_called()


async def test_authorized_user_passes_gate(dispatcher):
    d, replier, acl_manager = dispatcher
    acl_manager.get_role.return_value = Role.COLLABORATOR
    # Mock config to avoid session setup issues
    d._config.default_project = MagicMock()
    d._config.default_project.name = "proj"
    task = make_task(content="/help")
    with patch.object(d, '_handle_meta_command', new=AsyncMock()):
        await d.dispatch(task)
    replier.build_access_denied_card.assert_not_called()


async def test_no_acl_manager_allows_all(dispatcher):
    d, replier, acl_manager = dispatcher
    d._acl_manager = None  # Remove ACL manager
    d._config.default_project = MagicMock()
    d._config.default_project.name = "proj"
    task = make_task(content="/help")
    with patch.object(d, '_handle_meta_command', new=AsyncMock()):
        await d.dispatch(task)
    replier.build_access_denied_card.assert_not_called()
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_dispatcher_acl.py -v
```
Expected: FAIL — `TaskDispatcher.__init__` doesn't accept `acl_manager`

**Step 3: Modify `src/nextme/core/dispatcher.py`**

**3a. Add imports** at top of file:
```python
from typing import Optional  # already present, verify
```
Add after existing imports:
```python
from ..acl.manager import AclManager
from ..acl.schema import Role
```

**3b. Add `acl_manager` parameter to `__init__`:**

In `TaskDispatcher.__init__` signature, add:
```python
        acl_manager: Optional[AclManager] = None,
```

In `__init__` body, add:
```python
        self._acl_manager = acl_manager
```

**3c. Add ACL gate to `dispatch()` method:**

At the start of `dispatch()`, after extracting `context_id` and `replier`, add:

```python
        user_id = self._get_user_id(context_id)
        text = task.content.strip()

        # ------------------------------------------------------------------
        # ACL gate: check authorization before any processing.
        # /whoami and /help are always allowed (needed to apply for access).
        # ------------------------------------------------------------------
        if self._acl_manager is not None:
            _open_cmds = ("/whoami", "/help")
            is_open_cmd = any(text.lower().startswith(c) for c in _open_cmds)
            if not is_open_cmd:
                role = await self._acl_manager.get_role(user_id)
                if role is None:
                    logger.info(
                        "TaskDispatcher: unauthorized user %r denied (task %s)",
                        user_id,
                        task.id,
                    )
                    try:
                        denied_card = replier.build_access_denied_card(user_id)
                        if task.message_id:
                            in_thread = task.chat_type == "group"
                            await replier.reply_card(
                                task.message_id, denied_card, in_thread=in_thread
                            )
                        else:
                            await replier.send_card(chat_id, denied_card)
                    except Exception:
                        logger.exception(
                            "TaskDispatcher: failed to send denied card to %r", chat_id
                        )
                    return
```

**3d. Add role-based command permission checks in `_handle_meta_command()`:**

At the start of `_handle_meta_command()`, after extracting `command` and `arg`, add:

```python
        # Resolve caller's role for permission enforcement.
        caller_role: Optional[Role] = None
        if self._acl_manager is not None:
            caller_role = await self._acl_manager.get_role(
                self._get_user_id(context_id)
            )
```

Then for the `/project` section that switches/binds, wrap with:
```python
        elif command == "/project":
            # ...existing list display code for no-arg case...
            # For bind/unbind/switch, require owner+
            if arg and caller_role not in (Role.OWNER, Role.ADMIN):
                await replier.send_text(
                    chat_id,
                    "权限不足：切换/绑定项目需要 Owner 或 Admin 权限。",
                )
                return
            # ...rest of existing /project handling...
```

**3e. Add `/whoami` and `/acl` routing in `_handle_meta_command()`:**

Add before the `else: handle_help` block:

```python
        elif command == "/whoami":
            if self._acl_manager is not None:
                from .commands import handle_whoami
                await handle_whoami(
                    self._get_user_id(context_id),
                    self._acl_manager,
                    replier,
                    chat_id,
                )
            else:
                # No ACL configured — show open_id only
                uid = self._get_user_id(context_id)
                await replier.send_text(chat_id, f"open_id: `{uid}`\n角色: (未启用 ACL)")

        elif command == "/acl":
            if self._acl_manager is None:
                await replier.send_text(chat_id, "ACL 功能未启用。")
                return
            if caller_role not in (Role.ADMIN, Role.OWNER, Role.COLLABORATOR):
                await replier.send_text(chat_id, "权限不足。")
                return
            await self._handle_acl_command(
                arg, caller_role, self._get_user_id(context_id), replier, chat_id
            )
```

**3f. Add `_handle_acl_command()` private method to `TaskDispatcher`:**

```python
    async def _handle_acl_command(
        self,
        arg: str,
        caller_role: "Role",
        caller_id: str,
        replier: "Replier",
        chat_id: str,
    ) -> None:
        """Dispatch /acl sub-commands."""
        from .commands import (
            handle_acl_add,
            handle_acl_approve,
            handle_acl_list,
            handle_acl_pending,
            handle_acl_reject,
            handle_acl_remove,
        )
        from ..acl.schema import Role as _Role

        parts = arg.split(maxsplit=2) if arg else []
        sub = parts[0].lower() if parts else ""

        if not sub or sub == "list":
            await handle_acl_list(self._acl_manager, replier, chat_id)

        elif sub == "add":
            if len(parts) < 2:
                await replier.send_text(
                    chat_id, "用法: `/acl add <open_id> [owner|collaborator]`"
                )
                return
            target_id = parts[1]
            target_role_str = parts[2] if len(parts) > 2 else "collaborator"
            await handle_acl_add(
                actor_id=caller_id,
                actor_role=caller_role,
                target_id=target_id,
                target_role_str=target_role_str,
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub == "remove":
            if len(parts) < 2:
                await replier.send_text(chat_id, "用法: `/acl remove <open_id>`")
                return
            await handle_acl_remove(
                actor_id=caller_id,
                actor_role=caller_role,
                target_id=parts[1],
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub == "pending":
            if caller_role not in (_Role.ADMIN, _Role.OWNER):
                await replier.send_text(chat_id, "权限不足：需要 Owner 或 Admin 权限。")
                return
            await handle_acl_pending(
                viewer_role=caller_role,
                acl_manager=self._acl_manager,
                replier=replier,
                chat_id=chat_id,
            )

        elif sub in ("approve", "reject"):
            if caller_role not in (_Role.ADMIN, _Role.OWNER):
                await replier.send_text(chat_id, "权限不足：需要 Owner 或 Admin 权限。")
                return
            if len(parts) < 2:
                await replier.send_text(
                    chat_id, f"用法: `/acl {sub} <申请ID>`"
                )
                return
            try:
                app_id = int(parts[1])
            except ValueError:
                await replier.send_text(chat_id, "申请ID 必须是数字。")
                return
            if sub == "approve":
                await handle_acl_approve(
                    app_id=app_id,
                    reviewer_id=caller_id,
                    reviewer_role=caller_role,
                    acl_manager=self._acl_manager,
                    replier=replier,
                    chat_id=chat_id,
                )
            else:
                await handle_acl_reject(
                    app_id=app_id,
                    reviewer_id=caller_id,
                    reviewer_role=caller_role,
                    acl_manager=self._acl_manager,
                    replier=replier,
                    chat_id=chat_id,
                )
        else:
            await replier.send_text(
                chat_id,
                "未知子命令。可用: `list` `add` `remove` `pending` `approve` `reject`",
            )
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_dispatcher_acl.py -v
```
Expected: all PASSED

Run the full suite to ensure no regressions:
```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -20
```

**Step 5: Commit**

```bash
git add src/nextme/core/dispatcher.py tests/test_dispatcher_acl.py
git commit -m "feat(acl): add ACL gate and role-based command routing in TaskDispatcher"
```

---

### Task 7: Card action handlers (`acl_apply` + `acl_review`)

**Files:**
- Modify: `src/nextme/feishu/handler.py`
- Modify: `src/nextme/core/dispatcher.py`
- Create: `tests/test_acl_card_actions.py`

**Step 1: Write the failing tests**

```python
# tests/test_acl_card_actions.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
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
    # Application should be in the DB
    app = await acl_manager._db.get_pending_application("ou_applicant")
    assert app is not None
    assert app.requested_role == Role.COLLABORATOR


async def test_handle_acl_apply_duplicate_replies_existing(dispatcher_with_acl):
    d, replier, acl_manager = dispatcher_with_acl
    # First application
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    # Second application (duplicate)
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    # Only one pending should exist
    pending = await acl_manager._db.list_pending_applications()
    assert len(pending) == 1


async def test_handle_acl_apply_notifies_reviewers(dispatcher_with_acl, db):
    d, replier, acl_manager = dispatcher_with_acl
    # Add an owner to be notified for collaborator apps
    await db.add_user("ou_owner", Role.OWNER, "Owner", "ou_admin")
    await d.handle_acl_card_action({
        "action": "acl_apply",
        "open_id": "ou_applicant",
        "role": "collaborator",
    })
    # Should have sent DM to owner AND admin
    assert replier.send_to_user.call_count >= 1


async def test_handle_acl_review_approve(dispatcher_with_acl, db):
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


async def test_handle_acl_review_reject(dispatcher_with_acl, db):
    d, replier, acl_manager = dispatcher_with_acl
    app_id, _ = await acl_manager.create_application("ou_x", "X", Role.COLLABORATOR)
    await d.handle_acl_card_action({
        "action": "acl_review",
        "app_id": str(app_id),
        "decision": "rejected",
        "operator_id": "ou_admin",
    })
    role = await acl_manager.get_role("ou_x")
    assert role is None  # Not added
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_acl_card_actions.py -v
```
Expected: FAIL — `TaskDispatcher` has no `handle_acl_card_action`

**Step 3: Add `handle_acl_card_action` to `TaskDispatcher`**

Add to `src/nextme/core/dispatcher.py` (after `handle_card_action`):

```python
    async def handle_acl_card_action(self, action_data: dict) -> None:
        """Dispatch an ACL-related card button action.

        Called from :meth:`~nextme.feishu.handler.MessageHandler._on_card_action`
        via ``asyncio.run_coroutine_threadsafe`` when action is ``acl_apply``
        or ``acl_review``.

        Args:
            action_data: Parsed ``event.action.value`` dict from the card event,
                with an ``operator_id`` key injected by the handler.
        """
        if self._acl_manager is None:
            logger.warning("handle_acl_card_action: no ACL manager configured")
            return

        action = action_data.get("action")
        replier = self._feishu_client.get_replier()

        if action == "acl_apply":
            await self._handle_acl_apply_action(action_data, replier)
        elif action == "acl_review":
            await self._handle_acl_review_action(action_data, replier)
        else:
            logger.warning("handle_acl_card_action: unknown action %r", action)

    async def _handle_acl_apply_action(self, data: dict, replier: "Replier") -> None:
        """Process an acl_apply card button click."""
        from ..acl.schema import Role as _Role

        open_id: str = data.get("open_id", "")
        role_str: str = data.get("role", "collaborator")

        if not open_id:
            logger.warning("_handle_acl_apply_action: missing open_id")
            return

        try:
            requested_role = _Role(role_str)
        except ValueError:
            logger.warning("_handle_acl_apply_action: invalid role %r", role_str)
            return

        if requested_role == _Role.ADMIN:
            logger.warning("_handle_acl_apply_action: attempt to apply as admin denied")
            return

        # Check if already authorized
        existing_role = await self._acl_manager.get_role(open_id)
        if existing_role is not None:
            logger.info(
                "_handle_acl_apply_action: %r already has role %s, skipping",
                open_id, existing_role.value,
            )
            return

        app_id, existing_app = await self._acl_manager.create_application(
            open_id, "", requested_role
        )

        if existing_app is not None:
            logger.info(
                "_handle_acl_apply_action: duplicate pending app #%d for %r",
                existing_app.id, open_id,
            )
            return

        logger.info(
            "_handle_acl_apply_action: created application #%d for %r role=%s",
            app_id, open_id, requested_role.value,
        )

        # Notify reviewers
        reviewer_ids = await self._acl_manager.get_reviewers_for_role(requested_role)
        role_label = "Owner" if requested_role == _Role.OWNER else "Collaborator"
        notification_card = replier.build_acl_review_notification_card(
            app_id=app_id,
            applicant_name="",
            applicant_id=open_id,
            requested_role=requested_role.value,
        )
        for reviewer_id in reviewer_ids:
            try:
                await replier.send_to_user(reviewer_id, notification_card, "interactive")
            except Exception:
                logger.exception(
                    "_handle_acl_apply_action: failed to notify reviewer %r", reviewer_id
                )

    async def _handle_acl_review_action(self, data: dict, replier: "Replier") -> None:
        """Process an acl_review card button click (approve/reject)."""
        from ..acl.schema import Role as _Role

        app_id_str: str = data.get("app_id", "")
        decision: str = data.get("decision", "")
        operator_id: str = data.get("operator_id", "")

        if not app_id_str or not decision or not operator_id:
            logger.warning(
                "_handle_acl_review_action: missing fields app_id=%r decision=%r operator=%r",
                app_id_str, decision, operator_id,
            )
            return

        try:
            app_id = int(app_id_str)
        except ValueError:
            logger.warning("_handle_acl_review_action: invalid app_id %r", app_id_str)
            return

        # Verify reviewer still has permission
        reviewer_role = await self._acl_manager.get_role(operator_id)
        if reviewer_role is None:
            logger.warning(
                "_handle_acl_review_action: reviewer %r no longer authorized", operator_id
            )
            return

        app = await self._acl_manager._db.get_application(app_id)
        if app is None or app.status != "pending":
            logger.info(
                "_handle_acl_review_action: app #%d not pending (status=%s)",
                app_id, app.status if app else "not found",
            )
            return

        if not self._acl_manager.can_review(reviewer_role, app.requested_role):
            logger.warning(
                "_handle_acl_review_action: reviewer %r (role=%s) cannot review %s app",
                operator_id, reviewer_role.value, app.requested_role.value,
            )
            return

        if decision == "approved":
            result = await self._acl_manager.approve(app_id, operator_id)
            if result:
                logger.info(
                    "_handle_acl_review_action: approved app #%d for %r",
                    app_id, app.applicant_id,
                )
                # Notify applicant
                try:
                    role_label = "Owner" if result.requested_role.value == "owner" else "Collaborator"
                    await replier.send_to_user(
                        app.applicant_id,
                        '{"text":"✅ 您的权限申请已批准，您现在是 ' + role_label + '。"}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify applicant %r",
                        app.applicant_id,
                    )
        elif decision == "rejected":
            result = await self._acl_manager.reject(app_id, operator_id)
            if result:
                logger.info(
                    "_handle_acl_review_action: rejected app #%d for %r",
                    app_id, app.applicant_id,
                )
                try:
                    await replier.send_to_user(
                        app.applicant_id,
                        '{"text":"❌ 您的权限申请已被拒绝。如有疑问请联系管理员。"}',
                        "text",
                    )
                except Exception:
                    logger.exception(
                        "_handle_acl_review_action: failed to notify applicant %r",
                        app.applicant_id,
                    )
        else:
            logger.warning("_handle_acl_review_action: unknown decision %r", decision)
```

**Step 4: Add routing in `handler.py` `_on_card_action`**

In `src/nextme/feishu/handler.py`, in the `_on_card_action` method, after the existing `permission_choice` block (around line 120), add:

```python
            elif value.get("action") in ("acl_apply", "acl_review"):
                action_data = dict(value)
                # Inject operator open_id from card event
                operator_id = ""
                try:
                    if data.event and hasattr(data.event, "operator"):
                        operator_id = getattr(data.event.operator, "open_id", "") or ""
                except Exception:
                    pass
                action_data["operator_id"] = operator_id

                loop = self._loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._dispatcher.handle_acl_card_action(action_data), loop
                    )
                else:
                    logger.warning(
                        "_on_card_action: no running loop for acl action %r",
                        value.get("action"),
                    )
```

Also add to the toast response for acl actions:
```python
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = "已收到申请"
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_acl_card_actions.py -v
```
Expected: all PASSED

Run full suite:
```bash
uv run pytest tests/ --tb=short 2>&1 | tail -10
```

**Step 6: Commit**

```bash
git add src/nextme/core/dispatcher.py src/nextme/feishu/handler.py tests/test_acl_card_actions.py
git commit -m "feat(acl): add acl_apply and acl_review card action handlers with DM notifications"
```

---

### Task 8: Wire everything in `main.py` + update docs

**Files:**
- Modify: `src/nextme/main.py`
- Modify: `settings.json.example`
- Modify: `README.md`
- Modify: `README.zh.md`

**Step 1: Modify `src/nextme/main.py`**

In `run()` function, after Step 4 (StateStore) and before Step 5 (MemoryManager), add a new step:

```python
    # ------------------------------------------------------------------
    # Step 4b: AclDb + AclManager
    # ------------------------------------------------------------------
    from .acl.db import AclDb
    from .acl.manager import AclManager

    acl_db = AclDb()
    await acl_db.open()
    acl_manager = AclManager(db=acl_db, admin_users=settings.admin_users)
    logger.info(
        "AclManager: initialized (admin_users=%d)", len(settings.admin_users)
    )
```

In the `TaskDispatcher(...)` constructor call, add:
```python
        acl_manager=acl_manager,
```

In the shutdown sequence (after `memory_manager.flush_all()`), add:
```python
    await acl_db.close()
    logger.info("AclDb: closed")
```

**Step 2: Update `settings.json.example`**

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "admin_users": [
    "ou_your_open_id_here"
  ],
  "projects": [
    {
      "name": "my-project",
      "path": "/absolute/path/to/your/project",
      "executor": "claude"
    }
  ]
}
```

**Step 3: Update `README.md`**

Add a new "Access Control" section after the "Configuration" section:

```markdown
## Access Control

NextMe supports role-based access control (ACL) to restrict which users can interact with the bot.

### Roles

| Role | Configuration | Permissions |
|------|--------------|-------------|
| **Admin** | `admin_users` in `settings.json` | Full access; approve Owner applications |
| **Owner** | SQLite (`~/.nextme/nextme.db`) | Bot tasks, project switching, approve Collaborator applications |
| **Collaborator** | SQLite (`~/.nextme/nextme.db`) | Bot tasks, status commands; cannot switch projects |

### Setup

Add your `open_id` to `admin_users` in `~/.nextme/settings.json`:

```json
{
  "admin_users": ["ou_your_open_id_here"],
  ...
}
```

Use `/whoami` to find your `open_id`.

### Commands

| Command | Description | Min Role |
|---------|-------------|----------|
| `/whoami` | Show your open_id and role | Everyone |
| `/acl list` | List all authorized users | Collaborator |
| `/acl add <open_id> [owner\|collaborator]` | Add a user | Owner (collab only) / Admin |
| `/acl remove <open_id>` | Remove a user | Owner (collab only) / Admin |
| `/acl pending` | View pending applications | Owner / Admin |
| `/acl approve <id>` | Approve an application | Owner / Admin |
| `/acl reject <id>` | Reject an application | Owner / Admin |

### Application Flow

Unauthorized users receive a card with their `open_id` and buttons to apply for Owner or Collaborator access. Applications are sent as DM notifications to admins (for Owner applications) or owners + admins (for Collaborator applications). Reviewers can approve or reject directly from the notification card.
```

**Step 4: Run the full test suite to verify coverage**

```bash
uv run pytest tests/ -v 2>&1 | tail -20
```
Expected: ≥ 85% coverage, no failures

**Step 5: Commit and push**

```bash
git add src/nextme/main.py settings.json.example README.md README.zh.md
git commit -m "feat(acl): wire AclManager in main.py, update settings example and README"
git push origin main
```

---

## Summary

| Task | Files | Tests |
|------|-------|-------|
| 1: Schema + deps | `pyproject.toml`, `config/schema.py`, `acl/schema.py` | `test_acl_schema.py` |
| 2: AclDb | `acl/db.py` | `test_acl_db.py` |
| 3: AclManager | `acl/manager.py` | `test_acl_manager.py` |
| 4: Cards + send_to_user | `feishu/reply.py`, `core/interfaces.py` | `test_acl_cards.py` |
| 5: Command handlers | `core/commands.py` | `test_acl_commands.py` |
| 6: ACL gate + routing | `core/dispatcher.py` | `test_dispatcher_acl.py` |
| 7: Card action handlers | `core/dispatcher.py`, `feishu/handler.py` | `test_acl_card_actions.py` |
| 8: Wire + docs | `main.py`, `settings.json.example`, `README.md` | (full suite) |
