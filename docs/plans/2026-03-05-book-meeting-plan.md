# Book Meeting Skill — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `/skill book` command that parses natural language, @mention attendees, and optional room name → creates a Feishu calendar event with video link and optionally reserves a meeting room.

**Architecture:** Three core changes (Task → mentions field, handler mention parsing, dispatcher injection) plus one new API script and one new skill file. All existing tests must keep passing; new tests cover the changed code paths.

**Tech Stack:** Python 3.12, dataclasses, httpx (stdlib urllib.request to avoid new deps), pytest-asyncio, lark-oapi SDK.

---

## Worktree

All work happens in:
```
/Users/bytedance/develop/ai/NextMe/.worktrees/feat-book-meeting/
Branch: feat/book-meeting
```

Run tests from the worktree root:
```bash
cd /Users/bytedance/develop/ai/NextMe/.worktrees/feat-book-meeting
uv run pytest tests/ -x -q
```

---

## Task 1: Add `mentions` field to `Task`

**Files:**
- Modify: `src/nextme/protocol/types.py:53-64`
- Test: `tests/test_protocol_types.py` (create if missing, append if exists)

**Step 1: Write the failing test**

```python
# append to tests/test_protocol_types.py (create file if missing)
from nextme.protocol.types import Task
from unittest.mock import AsyncMock

def test_task_has_empty_mentions_by_default():
    task = Task(
        id="1", content="hi", session_id="s",
        reply_fn=AsyncMock(), message_id="m",
    )
    assert task.mentions == []

def test_task_accepts_mentions_list():
    task = Task(
        id="1", content="hi", session_id="s",
        reply_fn=AsyncMock(), message_id="m",
        mentions=[{"name": "小明", "open_id": "ou_abc"}],
    )
    assert task.mentions == [{"name": "小明", "open_id": "ou_abc"}]
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_protocol_types.py::test_task_has_empty_mentions_by_default -v
# Expected: ERROR — AttributeError: Task has no field 'mentions'
```

**Step 3: Add the field**

In `src/nextme/protocol/types.py`, inside the `Task` dataclass (after `was_queued: bool = False`):

```python
mentions: list[dict[str, str]] = field(default_factory=list)
# Each entry: {"name": "小明", "open_id": "ou_xxxxxxxx"}
```

**Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_protocol_types.py -v
# Expected: PASS
uv run pytest tests/ -x -q
# Expected: all existing tests still pass
```

**Step 5: Commit**

```bash
git add src/nextme/protocol/types.py tests/test_protocol_types.py
git commit -m "feat(types): add mentions field to Task"
```

---

## Task 2: Parse @mentions in `feishu/handler.py`

**Files:**
- Modify: `src/nextme/feishu/handler.py:231-296` (`handle_message` + `_extract_text_from_message`)
- Test: `tests/test_feishu_handler.py` (append new test class)

**Background — Feishu mention formats:**

*text* messages: `message.mentions` is a list; each item has `.id.open_id` and `.name`.
*post* (rich-text) messages: inline nodes with `"tag": "at"`, `"user_id"` (open_id), `"user_name"`.

**Step 1: Write failing tests**

Append to `tests/test_feishu_handler.py`:

```python
class TestMentionParsing:
    """MessageHandler correctly extracts @mention open_ids into task.mentions."""

    def _make_handler(self):
        from nextme.feishu.handler import MessageHandler
        from nextme.feishu.dedup import MessageDedup
        dispatcher = MagicMock()
        dispatcher.dispatch = AsyncMock()
        handler = MessageHandler(dedup=MessageDedup(), dispatcher=dispatcher)
        handler._loop = asyncio.get_event_loop()
        return handler, dispatcher

    def _make_event(self, msg_type, content_json, mentions_sdk=None, chat_id="oc_chat", user_id="ou_user"):
        """Build a fake lark P2ImMessageReceiveV1-like object."""
        message = MagicMock()
        message.message_id = "om_001"
        message.chat_id = chat_id
        message.chat_type = "p2p"
        message.message_type = msg_type
        message.content = json.dumps(content_json)
        message.mentions = mentions_sdk or []
        sender = MagicMock()
        sender.sender_id.open_id = user_id
        event = MagicMock()
        event.message = message
        event.sender = sender
        data = MagicMock()
        data.event = event
        return data

    def _make_mention(self, key, open_id, name):
        m = MagicMock()
        m.key = key
        m.id.open_id = open_id
        m.name = name
        return m

    def test_text_message_parses_mentions(self):
        handler, dispatcher = self._make_handler()
        sdk_mentions = [
            self._make_mention("@_user_1", "ou_aaa", "小明"),
            self._make_mention("@_user_2", "ou_bbb", "小红"),
        ]
        data = self._make_event(
            "text",
            {"text": "@_user_1 @_user_2 /skill book 明天开会"},
            mentions_sdk=sdk_mentions,
        )
        handler.handle_message(data)
        task = dispatcher.dispatch.call_args[0][0]
        assert task.mentions == [
            {"name": "小明", "open_id": "ou_aaa"},
            {"name": "小红", "open_id": "ou_bbb"},
        ]

    def test_text_message_no_mentions_gives_empty(self):
        handler, dispatcher = self._make_handler()
        data = self._make_event("text", {"text": "/skill book 明天开会"})
        handler.handle_message(data)
        task = dispatcher.dispatch.call_args[0][0]
        assert task.mentions == []

    def test_post_message_parses_at_nodes(self):
        handler, dispatcher = self._make_handler()
        post_content = {
            "zh_cn": {
                "title": "",
                "content": [[
                    {"tag": "text", "text": "帮我订会议 "},
                    {"tag": "at", "user_id": "ou_ccc", "user_name": "阿强"},
                    {"tag": "text", "text": " 明天下午3点"},
                ]]
            }
        }
        data = self._make_event("post", post_content)
        handler.handle_message(data)
        task = dispatcher.dispatch.call_args[0][0]
        assert task.mentions == [{"name": "阿强", "open_id": "ou_ccc"}]

    def test_mentions_deduplicated_by_open_id(self):
        handler, dispatcher = self._make_handler()
        sdk_mentions = [
            self._make_mention("@_user_1", "ou_aaa", "小明"),
            self._make_mention("@_user_1", "ou_aaa", "小明"),  # duplicate
        ]
        data = self._make_event("text", {"text": "hi"}, mentions_sdk=sdk_mentions)
        handler.handle_message(data)
        task = dispatcher.dispatch.call_args[0][0]
        assert len(task.mentions) == 1
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_feishu_handler.py::TestMentionParsing -v
# Expected: FAIL — task object has no attribute 'mentions' (or mentions=[])
```

**Step 3: Implement mention parsing in `handle_message`**

In `src/nextme/feishu/handler.py`, modify `handle_message` to call a new helper `_extract_mentions` and pass the result to `Task`:

Add after the `_MENTION_PREFIX_RE` definition (around line 34):
```python
def _extract_mentions(message: Any) -> list[dict[str, str]]:
    """Return deduplicated list of {name, open_id} from Feishu message mentions.

    Handles two formats:
    - text messages: message.mentions SDK list
    - post messages: inline tag=="at" nodes in content JSON
    """
    seen: set[str] = set()
    result: list[dict[str, str]] = []

    def _add(name: str, open_id: str) -> None:
        if open_id and open_id not in seen:
            seen.add(open_id)
            result.append({"name": name or "", "open_id": open_id})

    msg_type: str = getattr(message, "message_type", "") or ""

    # text messages: SDK provides message.mentions
    if msg_type == "text":
        for m in getattr(message, "mentions", None) or []:
            open_id = getattr(getattr(m, "id", None), "open_id", "") or ""
            name = getattr(m, "name", "") or ""
            _add(name, open_id)
        return result

    # post (rich-text) messages: parse tag=="at" nodes from content JSON
    if msg_type == "post":
        try:
            raw = getattr(message, "content", "") or ""
            content_obj = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return result
        for lang_body in content_obj.values():
            paragraphs = lang_body.get("content", []) if isinstance(lang_body, dict) else []
            for paragraph in paragraphs:
                for node in paragraph:
                    if isinstance(node, dict) and node.get("tag") == "at":
                        open_id = node.get("user_id", "") or ""
                        name = node.get("user_name", "") or ""
                        _add(name, open_id)
    return result
```

In `handle_message`, add mentions parsing and pass to Task.
Find the Task construction (around line 278) and add:
```python
        mentions = _extract_mentions(message)

        task = Task(
            id=str(uuid.uuid4()),
            content=text,
            session_id=session_id,
            reply_fn=_reply_fn,
            message_id=message_id,
            chat_type=chat_type,
            created_at=datetime.now(),
            mentions=mentions,       # ← new
        )
```

**Step 4: Run to verify tests pass**

```bash
uv run pytest tests/test_feishu_handler.py::TestMentionParsing -v
# Expected: 4 tests PASS
uv run pytest tests/ -x -q
# Expected: all tests pass
```

**Step 5: Commit**

```bash
git add src/nextme/feishu/handler.py tests/test_feishu_handler.py
git commit -m "feat(handler): parse @mention open_ids into Task.mentions"
```

---

## Task 3: Inject mentions into skill `user_input` in dispatcher

**Files:**
- Modify: `src/nextme/core/dispatcher.py:527-543`
- Test: `tests/test_dispatcher_commands.py` (append new tests)

**Background:** When `/skill book 明天下午3点 @小明` is dispatched, the dispatcher builds `user_input = "明天下午3点 @小明"` and calls `SkillInvoker().build_prompt(skill, user_input=...)`. We need to append the structured mentions list to `user_input` before building the prompt.

**Step 1: Write failing test**

Append to `tests/test_dispatcher_commands.py`:

```python
class TestSkillMentionInjection:
    """When a skill task has mentions, they are appended to user_input."""

    @pytest.fixture
    def dispatcher_with_skill(self, tmp_path):
        from nextme.skills.registry import SkillRegistry
        from nextme.skills.loader import SkillMeta
        from nextme.skills.loader import Skill

        registry = SkillRegistry()
        registry._skills["book"] = Skill(
            meta=SkillMeta(trigger="book", name="Book Meeting", description=""),
            template="User request: {user_input}",
            source="test",
        )
        config = MagicMock()
        config.projects = []
        config.default_project = None
        config.get_binding = MagicMock(return_value=None)
        settings = Settings(task_queue_capacity=10, progress_debounce_seconds=0.0)
        feishu_client = MagicMock()
        replier = MagicMock()
        replier.send_text = AsyncMock()
        replier.send_reaction = AsyncMock()
        feishu_client.get_replier = MagicMock(return_value=replier)
        d = TaskDispatcher(
            config=config, settings=settings,
            session_registry=SessionRegistry(),
            acp_registry=ACPRuntimeRegistry(),
            path_lock_registry=PathLockRegistry(),
            feishu_client=feishu_client,
            skill_registry=registry,
        )
        return d, replier

    async def test_skill_user_input_includes_mentions(self, dispatcher_with_skill):
        dispatcher, replier = dispatcher_with_skill
        task = Task(
            id="t1", content="/skill book 明天下午3点",
            session_id="oc_chat:ou_user",
            reply_fn=AsyncMock(), message_id="m1", chat_type="p2p",
            mentions=[
                {"name": "小明", "open_id": "ou_aaa"},
                {"name": "小红", "open_id": "ou_bbb"},
            ],
        )
        await dispatcher.dispatch(task)
        # The skill_task enqueued should have mentions in its content
        user_ctx = dispatcher._session_registry.get("oc_chat:ou_user")
        session = user_ctx.get_active_session() if user_ctx else None
        assert session is not None
        skill_task = session.pending_tasks[-1]
        assert "ou_aaa" in skill_task.content
        assert "小明" in skill_task.content
        assert "ou_bbb" in skill_task.content
        # cleanup
        for wt in list(dispatcher._worker_tasks.values()):
            if not wt.done():
                wt.cancel()
                await asyncio.gather(wt, return_exceptions=True)

    async def test_skill_no_mentions_unchanged(self, dispatcher_with_skill):
        dispatcher, replier = dispatcher_with_skill
        task = Task(
            id="t2", content="/skill book 明天下午3点",
            session_id="oc_chat:ou_user2",
            reply_fn=AsyncMock(), message_id="m2", chat_type="p2p",
            mentions=[],
        )
        await dispatcher.dispatch(task)
        user_ctx = dispatcher._session_registry.get("oc_chat:ou_user2")
        session = user_ctx.get_active_session() if user_ctx else None
        assert session is not None
        skill_task = session.pending_tasks[-1]
        assert "参与人" not in skill_task.content
        # cleanup
        for wt in list(dispatcher._worker_tasks.values()):
            if not wt.done():
                wt.cancel()
                await asyncio.gather(wt, return_exceptions=True)
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_dispatcher_commands.py::TestSkillMentionInjection -v
# Expected: FAIL — mentions not in skill_task.content
```

**Step 3: Inject mentions in dispatcher**

In `src/nextme/core/dispatcher.py`, find the skill dispatch block (around line 543):

```python
            prompt = SkillInvoker().build_prompt(skill, user_input=user_input.strip())
```

Change to:

```python
            enriched_input = user_input.strip()
            if task.mentions:
                lines = [
                    f"- {m['name']} (open_id: {m['open_id']})"
                    for m in task.mentions
                ]
                enriched_input += "\n\n参与人(@mentions):\n" + "\n".join(lines)
            prompt = SkillInvoker().build_prompt(skill, user_input=enriched_input)
```

**Step 4: Run to verify tests pass**

```bash
uv run pytest tests/test_dispatcher_commands.py::TestSkillMentionInjection -v
# Expected: 2 tests PASS
uv run pytest tests/ -x -q
# Expected: all tests pass
```

**Step 5: Commit**

```bash
git add src/nextme/core/dispatcher.py tests/test_dispatcher_commands.py
git commit -m "feat(dispatcher): inject @mention open_ids into skill user_input"
```

---

## Task 4: Write `scripts/feishu_book_meeting.py`

**Files:**
- Create: `scripts/feishu_book_meeting.py`
- Test: `tests/test_book_meeting_script.py` (create)

This script handles all Feishu Calendar API calls. It uses only Python stdlib (`urllib.request`, `json`, `argparse`) — no new dependencies.

**Step 1: Write failing tests**

Create `tests/test_book_meeting_script.py`:

```python
"""Unit tests for scripts/feishu_book_meeting.py.

Tests mock all HTTP calls — no real Feishu API access.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

# Add scripts/ to path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import feishu_book_meeting as bm


class TestGetToken:
    def test_returns_token_on_success(self):
        response_data = {"code": 0, "tenant_access_token": "t-abc123", "expire": 7200}
        with patch.object(bm, "_http_post", return_value=response_data):
            token = bm.get_tenant_token("app_id", "app_secret")
        assert token == "t-abc123"

    def test_raises_on_error_code(self):
        with patch.object(bm, "_http_post", return_value={"code": 99991663, "msg": "bad"}):
            with pytest.raises(RuntimeError, match="tenant_access_token"):
                bm.get_tenant_token("app_id", "app_secret")


class TestGetOrCreateCalendar:
    def test_returns_existing_calendar(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        cache_file.write_text("cal_cached_id\n")
        cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_cached_id"

    def test_creates_calendar_when_cache_missing(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        list_resp = {"code": 0, "data": {"calendar_list": []}}
        create_resp = {"code": 0, "data": {"calendar": {"calendar_id": "cal_new_id"}}}
        with patch.object(bm, "_http_get", return_value=list_resp), \
             patch.object(bm, "_http_post", return_value=create_resp):
            cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_new_id"
        assert cache_file.read_text().strip() == "cal_new_id"

    def test_finds_existing_calendar_by_name(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        list_resp = {"code": 0, "data": {"calendar_list": [
            {"calendar_id": "cal_existing", "summary": "NextMe 会议"}
        ]}}
        with patch.object(bm, "_http_get", return_value=list_resp):
            cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_existing"


class TestCreateEvent:
    def test_returns_event_id_and_vchat(self):
        resp = {"code": 0, "data": {"event": {
            "event_id": "ev_001",
            "vchat": {"meeting_url": "https://meeting.feishu.cn/abc"},
        }}}
        with patch.object(bm, "_http_post", return_value=resp):
            event_id, vchat_url = bm.create_event(
                "token", "cal_id",
                title="Team Meeting",
                start="2026-03-06T15:00:00+08:00",
                end="2026-03-06T16:00:00+08:00",
            )
        assert event_id == "ev_001"
        assert vchat_url == "https://meeting.feishu.cn/abc"

    def test_raises_on_api_error(self):
        with patch.object(bm, "_http_post", return_value={"code": 1, "msg": "error"}):
            with pytest.raises(RuntimeError, match="create event"):
                bm.create_event("token", "cal_id", "title",
                                "2026-03-06T15:00:00+08:00", "2026-03-06T16:00:00+08:00")


class TestAddAttendees:
    def test_adds_users_successfully(self):
        resp = {"code": 0, "data": {"attendees": []}}
        with patch.object(bm, "_http_post", return_value=resp):
            bm.add_attendees("token", "cal_id", "ev_id",
                             user_ids=["ou_aaa", "ou_bbb"], room_id=None)

    def test_adds_room_when_room_id_provided(self):
        calls = []
        def fake_post(url, token, body):
            calls.append(body)
            return {"code": 0, "data": {"attendees": []}}
        with patch.object(bm, "_http_post", side_effect=fake_post):
            bm.add_attendees("token", "cal_id", "ev_id",
                             user_ids=[], room_id="omm_room_abc")
        assert any(
            any(a.get("type") == "meeting_room" for a in c.get("attendees", []))
            for c in calls
        )


class TestSearchRoom:
    def test_returns_first_match(self):
        resp = {"code": 0, "data": {"resources": [
            {"room_id": "omm_abc", "name": "极光会议室"},
        ]}}
        with patch.object(bm, "_http_get", return_value=resp):
            room_id, room_name = bm.search_room("token", "极光")
        assert room_id == "omm_abc"
        assert room_name == "极光会议室"

    def test_returns_none_when_no_match(self):
        resp = {"code": 0, "data": {"resources": []}}
        with patch.object(bm, "_http_get", return_value=resp):
            room_id, room_name = bm.search_room("token", "不存在的会议室")
        assert room_id is None
        assert room_name is None
```

**Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_book_meeting_script.py -v
# Expected: ModuleNotFoundError: No module named 'feishu_book_meeting'
```

**Step 3: Implement `scripts/feishu_book_meeting.py`**

Create `scripts/feishu_book_meeting.py`:

```python
#!/usr/bin/env python3
"""Feishu Calendar booking helper.

Usage:
    python3 scripts/feishu_book_meeting.py \
        --title "Team Sync" \
        --start "2026-03-06T15:00:00+08:00" \
        --end   "2026-03-06T16:00:00+08:00" \
        [--attendees "ou_aaa,ou_bbb"] \
        [--room "极光"] \
        --config ~/.nextme/settings.json

Prints JSON result to stdout. Exits 1 on error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path

BASE_URL = "https://open.feishu.cn/open-apis"
CALENDAR_NAME = "NextMe 会议"
DEFAULT_CACHE = os.path.expanduser("~/.nextme/book_meeting_calendar_id")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (mockable in tests)
# ---------------------------------------------------------------------------

def _http_post(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------

def get_tenant_token(app_id: str, app_secret: str) -> str:
    resp = _http_post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        token="",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"tenant_access_token failed: {resp.get('msg')}")
    return resp["tenant_access_token"]


def get_or_create_calendar(token: str, cache_path: str = DEFAULT_CACHE) -> str:
    # Return cached calendar_id if available
    path = Path(cache_path)
    if path.exists():
        cached = path.read_text().strip()
        if cached:
            return cached

    # Search existing calendars
    resp = _http_get(f"{BASE_URL}/calendar/v4/calendars", token)
    for cal in (resp.get("data") or {}).get("calendar_list") or []:
        if cal.get("summary") == CALENDAR_NAME:
            cal_id = cal["calendar_id"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cal_id + "\n")
            return cal_id

    # Create new calendar
    resp = _http_post(f"{BASE_URL}/calendar/v4/calendars", token, {
        "summary": CALENDAR_NAME,
        "description": "由 NextMe bot 创建的共享会议日历",
        "permissions": "show_only_free_busy",
        "color": -1,
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"create calendar failed: {resp.get('msg')}")
    cal_id = resp["data"]["calendar"]["calendar_id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cal_id + "\n")
    return cal_id


def create_event(
    token: str,
    cal_id: str,
    title: str,
    start: str,
    end: str,
) -> tuple[str, str]:
    """Create calendar event. Returns (event_id, vchat_url)."""
    body = {
        "summary": title,
        "start_time": {"timestamp": _iso_to_timestamp(start), "timezone": "Asia/Shanghai"},
        "end_time":   {"timestamp": _iso_to_timestamp(end),   "timezone": "Asia/Shanghai"},
        "vchat": {"vc_type": "vc"},
        "free_busy_status": "busy",
        "visibility": "default",
        "need_notification": True,
    }
    resp = _http_post(f"{BASE_URL}/calendar/v4/calendars/{cal_id}/events", token, body)
    if resp.get("code") != 0:
        raise RuntimeError(f"create event failed: {resp.get('msg')}")
    event = resp["data"]["event"]
    vchat_url = (event.get("vchat") or {}).get("meeting_url") or ""
    return event["event_id"], vchat_url


def add_attendees(
    token: str,
    cal_id: str,
    event_id: str,
    user_ids: list[str],
    room_id: str | None,
) -> None:
    attendees = [{"type": "user", "user_id": uid} for uid in user_ids if uid]
    if room_id:
        attendees.append({"type": "meeting_room", "room_id": room_id})
    if not attendees:
        return
    resp = _http_post(
        f"{BASE_URL}/calendar/v4/calendars/{cal_id}/events/{event_id}/attendees"
        "?user_id_type=open_id",
        token,
        {"attendees": attendees, "need_notification": True},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"add attendees failed: {resp.get('msg')}")


def search_room(token: str, keyword: str) -> tuple[str | None, str | None]:
    """Search meeting room by keyword. Returns (room_id, room_name) or (None, None)."""
    url = f"{BASE_URL}/vc/v1/resources?keyword={urllib.parse.quote(keyword)}&resource_type=meeting_room"
    resp = _http_get(url, token)
    resources = (resp.get("data") or {}).get("resources") or []
    if not resources:
        return None, None
    first = resources[0]
    return first.get("room_id"), first.get("name")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_to_timestamp(iso: str) -> str:
    """Convert ISO8601 string to Unix timestamp string."""
    from datetime import datetime, timezone, timedelta
    # Handle +08:00 suffix
    if iso.endswith("+08:00"):
        iso_clean = iso[:-6]
        dt = datetime.fromisoformat(iso_clean).replace(tzinfo=timezone(timedelta(hours=8)))
    else:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return str(int(dt.timestamp()))


def _format_dt(iso: str) -> str:
    from datetime import datetime, timezone, timedelta
    if iso.endswith("+08:00"):
        iso_clean = iso[:-6]
        dt = datetime.fromisoformat(iso_clean)
    else:
        dt = datetime.fromisoformat(iso)
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu meeting booking helper")
    parser.add_argument("--title",     required=True)
    parser.add_argument("--start",     required=True, help="ISO8601+08:00")
    parser.add_argument("--end",       required=True, help="ISO8601+08:00")
    parser.add_argument("--attendees", default="", help="comma-separated open_ids")
    parser.add_argument("--room",      default="", help="room name keyword")
    parser.add_argument("--config",    default="~/.nextme/settings.json")
    args = parser.parse_args()

    config_path = os.path.expanduser(args.config)
    with open(config_path) as f:
        cfg = json.load(f)

    try:
        token = get_tenant_token(cfg["app_id"], cfg["app_secret"])
        cal_id = get_or_create_calendar(token)
        event_id, vchat_url = create_event(token, cal_id, args.title, args.start, args.end)

        user_ids = [u.strip() for u in args.attendees.split(",") if u.strip()]
        room_id = room_name = None
        if args.room:
            room_id, room_name = search_room(token, args.room)

        add_attendees(token, cal_id, event_id, user_ids, room_id)

        result = {
            "ok": True,
            "title":      args.title,
            "start":      _format_dt(args.start),
            "end":        _format_dt(args.end),
            "event_id":   event_id,
            "vchat_url":  vchat_url,
            "attendees":  user_ids,
            "room_name":  room_name or "",
            "room_booked": room_id is not None,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

**Step 4: Run to verify tests pass**

```bash
uv run pytest tests/test_book_meeting_script.py -v
# Expected: all tests PASS
uv run pytest tests/ -x -q
# Expected: all tests pass, coverage ≥ 85%
```

**Step 5: Commit**

```bash
git add scripts/feishu_book_meeting.py tests/test_book_meeting_script.py
git commit -m "feat(scripts): add feishu_book_meeting.py Calendar API helper"
```

---

## Task 5: Write `skills/public/book-meeting/SKILL.md`

**Files:**
- Create: `skills/public/book-meeting/SKILL.md`
- Test: verify skill loads correctly via SkillRegistry

**Step 1: Write failing test**

Append to `tests/test_skill_loader.py` (create if missing):

```python
def test_book_meeting_skill_loads():
    from nextme.skills.loader import load_skill_file
    from pathlib import Path
    skill_path = Path(__file__).parent.parent / "skills/public/book-meeting/SKILL.md"
    skill = load_skill_file(skill_path, source="nextme")
    assert skill.meta.trigger == "book"
    assert skill.meta.name == "Book Meeting"
    assert "{user_input}" in skill.template
```

**Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_skill_loader.py::test_book_meeting_skill_loads -v
# Expected: FAIL — file not found
```

**Step 3: Create the skill file**

Create `skills/public/book-meeting/SKILL.md`:

````markdown
---
name: Book Meeting
trigger: book
description: 预定飞书会议——支持日程创建、邀请参与人、预定会议室。用法：/skill book <时间> <标题> [@参与人...] [会议室：<名称>]
---

你是飞书会议预定助手。按以下步骤完成预定，**不要询问用户，信息不足时自动填充默认值**。

## 步骤 1：解析会议信息

从用户请求中提取以下字段（缺失时使用默认值）：

| 字段 | 提取规则 | 默认值 |
|------|---------|--------|
| title | 会议标题，如"团队周会" | "会议" |
| start | 开始时间 → ISO8601+08:00，如"明天下午3点" → 次日T15:00:00+08:00 | 今天最近的整点+1h |
| end | 结束时间 | start + 1小时 |
| attendees | 参与人(@mentions)段落中每行的 open_id，逗号拼接 | ""（空）|
| room | 会议室关键词，如"极光"；若用户未提及，留空 | ""（空，仅创建线上会议）|

## 步骤 2：确定脚本路径

```bash
PROJECT_ROOT=$(pwd)
SCRIPT="$PROJECT_ROOT/scripts/feishu_book_meeting.py"
```

## 步骤 3：调用预定脚本

```bash
python3 "$SCRIPT" \
  --title "<title>" \
  --start "<ISO8601+08:00>" \
  --end   "<ISO8601+08:00>" \
  [--attendees "<open_id1,open_id2>"] \
  [--room "<room_keyword>"] \
  --config ~/.nextme/settings.json
```

- `--attendees` 和 `--room` 仅在有值时传入。
- 将 stdout 解析为 JSON。

## 步骤 4：回复用户

**成功（ok=true）：**

✅ **{title}** 已预定
📅 {start} – {end}
👥 参与人：{attendees 列表，若空则"仅自己"}
📍 会议室：{room_name}（若 room_booked=true）
🔗 飞书会议：{vchat_url}

**失败（ok=false）：**

❌ 预定失败：{error}
请检查：① 飞书应用是否已开启 `calendar:calendar` 权限；② 时间是否与现有日程冲突；③ 会议室名称是否正确。

---

{user_input}
````

**Step 4: Run to verify test passes**

```bash
uv run pytest tests/test_skill_loader.py::test_book_meeting_skill_loads -v
# Expected: PASS
uv run pytest tests/ -x -q
# Expected: all tests pass, coverage ≥ 85%
```

**Step 5: Commit**

```bash
git add skills/public/book-meeting/SKILL.md tests/test_skill_loader.py
git commit -m "feat(skills): add book-meeting skill for Feishu calendar booking"
```

---

## Final Verification

After all 5 tasks are complete:

```bash
# Full test suite
uv run pytest tests/ -x -q
# Expected: ≥ 1260 tests passed, coverage ≥ 85%

# Verify skill is registered when bot starts
uv run nextme --help   # just to confirm the package loads
grep -r "book" skills/public/book-meeting/SKILL.md  # confirm file exists

# Smoke-test the script CLI (requires real credentials)
python3 scripts/feishu_book_meeting.py \
  --title "测试会议" \
  --start "$(date -v+1d '+%Y-%m-%dT15:00:00+08:00')" \
  --end   "$(date -v+1d '+%Y-%m-%dT16:00:00+08:00')" \
  --config ~/.nextme/settings.json
```

**Required Feishu permissions** (enable in Developer Console before testing):
- Search "日历" → enable `calendar:calendar`
- Search "会议室" → enable "获取会议室信息"
