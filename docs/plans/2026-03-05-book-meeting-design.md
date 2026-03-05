# Book Meeting Skill — Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `/book` skill that lets users book a Feishu calendar event (with video meeting link) and optionally reserve a physical meeting room, by sending a natural-language message with @mention attendees.

**Architecture:** Script + Skill (Method B). A deterministic Python helper script handles all Feishu Calendar API calls; a SKILL.md prompt guides Claude Code to parse natural language, call the script, and format the reply.

**Tech Stack:** Python 3.12+, `httpx` (already in venv via lark-oapi), lark-oapi SDK, Feishu Calendar v4 API, Feishu VC v1 API.

---

## Context & Constraints

- **Auth:** `tenant_access_token` derived from `app_id`/`app_secret` in `~/.nextme/settings.json`.
- **Permissions required** (enable in Feishu Developer Console):
  - `calendar:calendar` — create events + add attendees (users and meeting rooms)
  - Search "会议室" in the console and enable "获取会议室信息"
- **Attendees:** Resolved from Feishu `@mention` open_ids parsed out of the incoming message.
- **Meeting room:** Optional. If the user mentions a room name, Claude searches by keyword; if not mentioned, only an online Feishu Meet link is created.
- **Calendar:** The bot uses its own app calendar. On first run the script auto-creates a "NextMe 会议" shared calendar and caches its `calendar_id` in `~/.nextme/book_meeting_calendar_id`.

---

## Components

### 1. `protocol/types.py` — Task.mentions

```python
mentions: list[dict[str, str]] = Field(default_factory=list)
# Each entry: {"name": "小明", "open_id": "ou_xxxxxxxx"}
```

### 2. `feishu/handler.py` — @mention parsing

**text messages** — `message.mentions` list (lark SDK):
```python
for m in getattr(message, "mentions", None) or []:
    open_id = getattr(getattr(m, "id", None), "open_id", "") or ""
    name    = getattr(m, "name", "") or ""
    if open_id:
        mentions.append({"name": name, "open_id": open_id})
```

**post (rich-text) messages** — inline `tag=="at"` nodes:
```python
if node.get("tag") == "at":
    open_id = node.get("user_id", "")
    name    = node.get("user_name", "")
    if open_id:
        mentions.append({"name": name, "open_id": open_id})
```

Parsed mentions are stored in `task.mentions` (deduplicated by open_id).

### 3. `core/dispatcher.py` — inject mentions into skill user_input

In `_dispatch_skill()` (the code path that builds the prompt for a triggered skill):
```python
if task.mentions:
    lines = [f"- {m['name']} (open_id: {m['open_id']})" for m in task.mentions]
    user_input += "\n\n参与人(@mentions):\n" + "\n".join(lines)
```

### 4. `scripts/feishu_book_meeting.py` — API helper script

**CLI interface:**
```bash
python3 scripts/feishu_book_meeting.py \
  --title   "团队周会" \
  --start   "2026-03-06T15:00:00+08:00" \
  --end     "2026-03-06T16:00:00+08:00" \
  --attendees "ou_aaa,ou_bbb" \   # optional, comma-separated open_ids
  --room    "极光" \               # optional, room name keyword
  --config  "~/.nextme/settings.json"
```

**Internal steps:**
1. Read `app_id` / `app_secret` from config JSON
2. `POST /auth/v3/tenant_access_token/internal` → token
3. Get or create bot calendar:
   - `GET /calendar/v4/calendars` — find calendar named "NextMe 会议"
   - If not found: `POST /calendar/v4/calendars` to create it
   - Cache `calendar_id` in `~/.nextme/book_meeting_calendar_id`
4. `POST /calendar/v4/calendars/{cal_id}/events` — create event with `vchat.vc_type="vc"`
5. If `--attendees`: `POST .../events/{event_id}/attendees` — batch add users
6. If `--room`: `GET /vc/v1/resources?keyword={room}` → find first matching `room_id` → add as `type="meeting_room"` attendee
7. Print JSON result to stdout:

```json
{
  "ok": true,
  "title": "团队周会",
  "start": "2026-03-06 15:00",
  "end":   "2026-03-06 16:00",
  "event_url": "https://applink.feishu.cn/client/calendar/event?eventId=...",
  "vchat_url": "https://meeting.feishu.cn/...",
  "attendees": ["小明", "小红"],
  "room_name": "极光会议室",
  "room_booked": true
}
```

On error, print `{"ok": false, "error": "..."}` and exit with code 1.

### 5. `skills/public/book-meeting/SKILL.md`

```markdown
---
name: Book Meeting
trigger: book
description: 预定飞书会议——支持日程创建、邀请参与人、预定会议室。用法：/book <时间> <标题> [@参与人...] [会议室：<名称>]
---

你是飞书会议预定助手。按以下步骤完成预定：

## 步骤 1：解析会议信息

从下方用户请求中提取：
- **title**: 会议标题（未提及则用"会议"）
- **start**: 开始时间，转为 ISO8601+08:00（如"明天下午3点"→次日T15:00:00+08:00）
- **end**: 结束时间（未提及则开始时间+1小时）
- **attendees**: 参与人 open_id 列表（逗号拼接，来自下方"参与人(@mentions)"）
- **room**: 会议室关键词（如"极光"；若未提及则不传）

## 步骤 2：调用预定脚本

用 `pwd` 确定项目根目录，然后执行：

```bash
python3 <project_root>/scripts/feishu_book_meeting.py \
  --title "<title>" \
  --start "<ISO8601>" \
  --end   "<ISO8601>" \
  [--attendees "<open_id1,open_id2>"] \
  [--room "<room_keyword>"] \
  --config ~/.nextme/settings.json
```

将 stdout 解析为 JSON。

## 步骤 3：回复用户

**成功：**
✅ **{title}** 已预定
📅 {start} – {end}
👥 参与人：{attendees 姓名列表，逗号分隔}
📍 会议室：{room_name}（若已预定）
🔗 飞书会议：{vchat_url}

**失败：** 说明 `error` 字段原因，建议检查飞书应用权限或时间冲突。

---

{user_input}
```

---

## Data Flow

```
User: /book 明天下午3点 团队周会 @小明 @小红 会议室：极光
        │
        ▼
handler.py ── 解析 message.mentions ──► task.mentions = [{小明,ou_xxx},{小红,ou_yyy}]
        │
        ▼
dispatcher.py ── skill /book 触发 ──► user_input 追加参与人行
        │
        ▼
Claude Code ── 解析自然语言 ──► 调用 feishu_book_meeting.py
        │
        ▼
feishu_book_meeting.py
  ├─ tenant_access_token
  ├─ 创建/获取日历
  ├─ 创建日程 + vchat
  ├─ 加参与人 ou_xxx, ou_yyy
  └─ 搜索"极光" → 加会议室
        │
        ▼
stdout JSON ──► Claude 格式化 ──► 回复用户
✅ 团队周会 | 明天 15:00-16:00 | 极光会议室 | 飞书会议链接
```

---

## Files Changed / Created

| File | Change |
|------|--------|
| `protocol/types.py` | Add `mentions: list[dict[str, str]]` to `Task` |
| `feishu/handler.py` | Parse mentions from text + post messages; populate `task.mentions` |
| `core/dispatcher.py` | Inject mentions into skill `user_input` |
| `scripts/feishu_book_meeting.py` | New — Feishu Calendar API helper script |
| `skills/public/book-meeting/SKILL.md` | New — skill prompt template |

---

## Testing Plan

- Unit tests for `handler.py` mention parsing (text + post message types)
- Unit tests for dispatcher mention injection
- Script tested manually with real credentials against sandbox calendar
- E2E: `/book` message with @mention → verify mentions appear in user_input fed to skill
