# Access Control (ACL) Implementation Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add role-based access control to NextMe so only authorized users can submit tasks to the bot, with an interactive card-based application and approval flow entirely within Feishu.

**Architecture:** Three-tier role model (admin / owner / collaborator) stored in SQLite (`~/.nextme/nextme.db`); ACL gate at the top of `TaskDispatcher.dispatch()`; unauthorized users receive an interactive application card; admin/owner receive DM approval cards via `receive_id_type=open_id` push.

**Tech Stack:** Python 3.12+, aiosqlite, lark-oapi, pydantic v2, existing asyncio architecture.

---

## Section 1 — Role Model & Permission Matrix

### Roles

| Role | Storage | Description |
|------|---------|-------------|
| **admin** | `settings.json: admin_users: [...]` | System-level, cannot be removed via commands |
| **owner** | `nextme.db acl_users` | Approved by admin; manages collaborators |
| **collaborator** | `nextme.db acl_users` | Approved by owner/admin; regular bot user |

### Permission Matrix

| Command / Action | Unauthorized | Collaborator | Owner | Admin |
|-----------------|:------------:|:------------:|:-----:|:-----:|
| `/whoami` | ✅ | ✅ | ✅ | ✅ |
| `/help` | ✅ | ✅ | ✅ | ✅ |
| Submit task (regular message) | ❌ → apply card | ✅ | ✅ | ✅ |
| `/new` `/stop` `/status` `/task` `/skill` `/remember` | ❌ | ✅ | ✅ | ✅ |
| `/project <name>` (switch) | ❌ | ❌ | ✅ | ✅ |
| `/project bind/unbind` | ❌ | ❌ | ✅ | ✅ |
| `/acl list` | ❌ | ✅ | ✅ | ✅ |
| `/acl add/remove collaborator` | ❌ | ❌ | ✅ | ✅ |
| `/acl add/remove owner` | ❌ | ❌ | ❌ | ✅ |
| `/acl pending` | ❌ | ❌ | ✅ (collaborator apps only) | ✅ (all) |
| `/acl approve/reject <id>` | ❌ | ❌ | ✅ (collaborator apps only) | ✅ (all) |

> `/whoami` and `/help` are available to all users including unauthorized, as they serve as entry points to the application flow.

---

## Section 2 — Storage

### Database: `~/.nextme/nextme.db`

Shared SQLite database for all NextMe persistent data (ACL is the first tenant; future tables can be added here).

### Table: `acl_users`

```sql
CREATE TABLE IF NOT EXISTS acl_users (
    open_id      TEXT PRIMARY KEY,
    role         TEXT NOT NULL CHECK(role IN ('owner', 'collaborator')),
    display_name TEXT NOT NULL DEFAULT '',
    added_by     TEXT NOT NULL,
    added_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Table: `acl_applications`

```sql
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
);
```

The `UNIQUE(applicant_id, status)` constraint prevents duplicate `pending` rows for the same user.

### Storage layout

| File | Contents |
|------|---------|
| `settings.json` | `admin_users: [open_id, ...]` (new static field) |
| `nextme.db` | `acl_users` + `acl_applications` tables (dynamic) |
| `state.json` | Unchanged — session state + project bindings |

---

## Section 3 — Application & Approval Flow

### Flow Diagram

```
Unauthorized user sends message
        │
        ▼
Return "no permission" card
┌─────────────────────────────┐
│ ⛔ No Access                │
│ Your open_id: ou_xxx        │
│                             │
│ [Apply as Owner]            │
│ [Apply as Collaborator]     │
└─────────────────────────────┘
        │
        ▼  card button click (action=acl_apply)
Check for existing pending application
  ├─ exists → reply "Application #id already pending"
  └─ new → write acl_applications (pending)
        │
        ├─ requested owner       → DM all admins
        └─ requested collaborator → DM all owners (+admins)

Notification card (sent as DM to reviewers):
┌─────────────────────────────┐
│ 📋 Access Request #42       │
│ Applicant: 张三 (ou_xxx)    │
│ Role: Collaborator          │
│ Time: 2026-03-01 10:00      │
│                             │
│ [✅ Approve]  [❌ Reject]   │
└─────────────────────────────┘
        │
        ▼  reviewer clicks (action=acl_review)
Verify reviewer still has permission
Update acl_applications.status
  ├─ approved → INSERT into acl_users
  └─ rejected → status update only
Replace card buttons with result label (prevent double-click)
DM applicant: "✅ Approved as Collaborator" / "❌ Application rejected"
```

### Edge Cases

| Situation | Handling |
|-----------|---------|
| Duplicate pending application | Reply "Application #id already pending, please wait" |
| Already authorized user clicks apply | Reply "You are already role X" |
| Collaborator applies for owner upgrade | Normal flow (allowed) |
| No owners exist (cold start) | Collaborator applications notify only admins |
| No admins configured | Owner applications saved to DB; reply "No admins configured, contact system administrator" |
| Reviewer lost permission before clicking | Card action validates role; reject with "Insufficient permission" |
| Admin tries to remove another admin | Rejected — admins are in settings.json, not DB |

### DM Push Implementation

Feishu API supports `receive_id_type=open_id` to send messages directly to a user's open_id without needing a pre-existing chat_id. `FeishuReplier` gains a new method:

```python
async def send_to_user(self, open_id: str, content: str, msg_type: str = "text") -> None:
    """Send a DM to any user by open_id (receive_id_type=open_id)."""
```

---

## Section 4 — Command Design

### New Commands

#### `/whoami` (all users including unauthorized)

Returns a card showing the user's own info:
- `open_id`
- Role (or "No access" for unauthorized)
- Join date and added-by (if authorized)
- "Apply for access" buttons (if unauthorized)

#### `/acl list` (collaborator and above)

Card listing all authorized users grouped by role. Shows `display_name`, `open_id`, and `added_at`.

#### `/acl add <open_id> [owner|collaborator]`

- Owner: can only add collaborators
- Admin: can add owner or collaborator
- Default role if omitted: `collaborator`
- Error if user already exists with same or higher role

#### `/acl remove <open_id>`

- Owner: can remove collaborators only
- Admin: can remove owner or collaborator (not another admin)
- DM notification sent to removed user

#### `/acl pending`

Interactive card listing all `pending` applications with Approve/Reject buttons:
- Owner sees: collaborator applications only
- Admin sees: all applications (owner + collaborator)

#### `/acl approve <id>` / `/acl reject <id>`

Text-based fallback for card approval (same logic, different entry point).

### Modified Commands

| Command | Change |
|---------|--------|
| `/project <name>` | Add role check: collaborator gets "Insufficient permission" |
| `/project bind/unbind` | Same role check |
| `/help` | Dynamically show commands based on caller's role |

---

## Section 5 — Architecture & Code Changes

### ACL Gate Position in Call Chain

```
handler.py
  _on_message_receive()
       │  (dedup + Task construction only, no ACL)
       ▼
dispatcher.py
  dispatch()
       │
       ├─ 1. Extract user_id from task.session_id
       ├─ 2. role = await acl_manager.get_role(user_id)   ← NEW
       │        ├─ user_id in settings.admin_users → Role.ADMIN
       │        ├─ SQLite owner → Role.OWNER
       │        ├─ SQLite collaborator → Role.COLLABORATOR
       │        └─ not found → None (unauthorized)
       │
       ├─ None + not /whoami or /help → send_access_denied_card(), return
       │
       ├─ 3. Parse meta-command
       │        └─ _check_command_permission(role, command)   ← NEW
       │             └─ insufficient → reply "Insufficient permission", return
       │
       └─ 4. Normal routing (existing logic unchanged)
```

### New Files

```
src/nextme/
└── acl/
    ├── __init__.py
    ├── schema.py      # Role enum, ACLUser, ACLApplication (pydantic models)
    ├── db.py          # AclDb — aiosqlite CRUD (create tables, insert, query, update)
    └── manager.py     # AclManager — business logic:
                       #   get_role(), add_user(), remove_user()
                       #   create_application(), approve(), reject()
                       #   notify_reviewers()
```

### Modified Files

| File | Change |
|------|--------|
| `config/schema.py` | `Settings`: add `admin_users: list[str] = []` |
| `core/dispatcher.py` | Inject `AclManager`; add ACL gate in `dispatch()`; add role checks in `_handle_meta_command()`; add `/acl` and `/whoami` routing |
| `core/commands.py` | Add `handle_whoami`, `handle_acl_list`, `handle_acl_add`, `handle_acl_remove`, `handle_acl_pending`, `handle_acl_approve`, `handle_acl_reject` |
| `feishu/reply.py` | Add `send_to_user(open_id, content, msg_type)` |
| `core/interfaces.py` | Add `send_to_user` to `Replier` protocol |
| `main.py` | Initialize `AclDb` + `AclManager`; pass to `TaskDispatcher` |

### Card Action Routing Extension

`handler.py _on_card_action` currently handles only `action=permission_choice`. Two new action types are added:

| `action` value | Trigger | Handler |
|---------------|---------|---------|
| `permission_choice` | ACP permission confirm (existing) | `dispatcher.handle_card_action` |
| `acl_apply` | Unauthorized user applies for role | `dispatcher.handle_acl_apply_action` (new) |
| `acl_review` | Admin/owner approves/rejects | `dispatcher.handle_acl_review_action` (new) |

### New Dependency

`aiosqlite` — async SQLite driver, lightweight, no conflicts with existing stack.

---

## Summary

| Aspect | Decision |
|--------|---------|
| Storage | `~/.nextme/nextme.db` (SQLite via aiosqlite) |
| Role tiers | admin (settings.json) / owner / collaborator (DB) |
| ACL gate | Top of `TaskDispatcher.dispatch()` |
| Unauthorized handling | Always show apply card with open_id info |
| Approval flow | Interactive Feishu cards + DM push to reviewers |
| Fallback | `/acl approve <id>` text command as alternative to card |
| New dependency | `aiosqlite` |
