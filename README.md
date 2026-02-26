# NextMe

**Feishu IM × Claude Code Agent Bot**

[中文文档](README.zh.md)

Turn Feishu group chats and direct messages into an interactive Claude Code terminal. Send a message in Feishu — NextMe routes it to a local `claude` subprocess, streams progress updates, and delivers the final result as an interactive card.

---

## Features

| Feature | Description |
|---------|-------------|
| Feishu WebSocket | Persistent long connection, auto-reconnect |
| DirectClaudeRuntime | Spawns `claude --print --output-format stream-json`; session continuity via `--resume` |
| ACPRuntime (optional) | JSON-RPC 2.0 over `cc-acp` subprocess |
| Streaming progress cards | Card updated in real time during execution; live tool-call display |
| Permission flow | Agent pushes a confirmation card for write operations; user replies with a number |
| Multi-project parallel | Each `(user, project)` pair gets an independent worker; multiple projects run concurrently |
| Chat binding | Bind a group chat to a specific project (`/project bind <name>`) |
| Session persistence | Claude session ID survives bot restarts; conversation history seamlessly resumed |
| Long-term memory | `/remember <text>` saves user-level facts (shared across all chats); injected into new sessions automatically |
| Context compression | Oversized contexts auto-compressed with zlib / lzma / brotli |
| Skills system | Markdown prompt templates; tiered discovery (Built-in / Global / NextMe Global / Project); `/review` `/commit` `/test` etc. |
| Meta-commands | `/new` `/stop` `/help` `/status` `/project` `/task` `/remember` `/skill` |
| Path lock | Only one session may write to a given project directory at a time |
| Graceful shutdown | SIGTERM/SIGINT → drain in-flight tasks → flush state → exit |

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Feishu developer account (enterprise self-built app)
- `claude` CLI installed and authenticated

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code
```

### Install

```bash
git clone https://github.com/chat2dev/NextMe.git
cd NextMe
uv sync
```

### Configure

**1. Create `nextme.json`**

```bash
cp nextme.json.example nextme.json
```

Edit `nextme.json`:

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "projects": [
    {
      "name": "my-project",
      "path": "/absolute/path/to/your/project",
      "executor": "claude"
    }
  ]
}
```

**2. Feishu app configuration**

Go to the [Feishu Open Platform](https://open.feishu.cn/) and follow the steps below.

**Step 1 — Create the app and enable Bot**

1. Click **Create Custom App**.
2. In the app dashboard, go to **Features** and enable **Bot**.

**Step 2 — Get credentials**

Go to **Credentials & Basic Info**, copy **App ID** and **App Secret**, and paste them into `nextme.json`:

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**Step 3 — Grant permissions**

Go to **Permissions & Scopes**. You can import all required scopes at once using the JSON below:

```json
{
  "scopes": {
    "tenant": [
      "contact:contact.base:readonly",
      "docx:document:readonly",
      "im:chat:read",
      "im:chat:update",
      "im:message.group_at_msg:readonly",
      "im:message.p2p_msg:readonly",
      "im:message.pins:read",
      "im:message.pins:write_only",
      "im:message.reactions:read",
      "im:message.reactions:write_only",
      "im:message:readonly",
      "im:message:recall",
      "im:message:send_as_bot",
      "im:message:send_multi_users",
      "im:message:send_sys_msg",
      "im:message:update",
      "im:resource"
    ],
    "user": [
      "contact:user.employee_id:readonly",
      "docx:document:readonly"
    ]
  }
}
```

Scope reference:

| Scope | Purpose |
|-------|---------|
| `im:message:send_as_bot` | Send messages as bot |
| `im:message:send_multi_users` | Send messages to multiple users |
| `im:message:update` | Update / edit messages |
| `im:message:send_sys_msg` | Send system messages |
| `im:message:recall` | Recall messages |
| `im:message:readonly` | Read messages |
| `im:message.group_at_msg:readonly` | Receive group @ messages |
| `im:message.p2p_msg:readonly` | Receive direct messages |
| `im:message.reactions:read` | Read message reactions |
| `im:message.reactions:write_only` | Add message reactions |
| `im:message.pins:read` | Read pinned messages |
| `im:message.pins:write_only` | Pin / unpin messages |
| `im:chat:read` | Read chat info |
| `im:chat:update` | Update chat info |
| `im:resource` | Upload / download message resources |
| `contact:contact.base:readonly` | Read basic contact info |
| `docx:document:readonly` | Read Feishu Docs content |
| `contact:user.employee_id:readonly` | Read user employee ID (user scope) |

**Step 4 — Start NextMe**

```bash
nextme up
```

*(See [Start](#start) section below for full options.)*

**Step 5 — Configure events and callbacks**

> Complete this step **after** NextMe is running so the persistent connection is active.

Go to **Event Subscriptions**:

- Set **Subscription mode** to **Using persistent connection**.
- Add event: `im.message.receive_v1`

Go to **Callback**:

- Set **Subscription mode** to **Using persistent connection**.
- Add callbacks:
  - `card.action.trigger` — interactive card button actions
  - `url.preview.get` — URL preview

**Step 6 — Publish the app**

Go to **Version Management & Release** and submit for review. Once approved the bot is available in Feishu.

### Start

```bash
nextme up
```

Optional flags:

```
nextme up --directory /path/to/project   # override project directory
           --executor claude             # agent executor (default: claude)
           --log-level DEBUG             # log verbosity
```

Stop:

```bash
nextme down
```

---

## Usage

### Conversational tasks

Send any message to the bot. The agent executes the task inside the configured project directory and returns the result as an interactive card.

### Meta-commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new conversation (clears history) |
| `/stop` | Cancel the currently running task |
| `/help` | Show the help card |
| `/status` | Show all session statuses |
| `/task` | Show active tasks and queue depth per project |
| `/project` | List all configured projects |
| `/project <name>` | Switch the active project |
| `/project bind <name>` | Permanently bind this chat to a project |
| `/project unbind` | Remove the chat-to-project binding |
| `/skill` | List all registered skills grouped by tier (Project / NextMe Global / Global / Built-in) |
| `/skill <trigger>` | Invoke a skill by trigger name |
| `/remember <text>` | Save a fact to long-term memory |

### Built-in skills

| Trigger | Description |
|---------|-------------|
| `/skill review` | Code review: correctness / performance / readability |
| `/skill commit` | Generate a Conventional Commits message from `git diff` |
| `/skill explain` | Explain how code works |
| `/skill test` | Generate unit tests |
| `/skill debug` | Systematic debugging workflow |

### Permission confirmation (ACPRuntime only)

When the agent needs to perform a write operation, a permission card is pushed to Feishu:

```
Authorization required
The agent is about to perform: ...

1. Allow
2. Deny
3. Always allow
```

Reply with the corresponding number to continue.

---

## Configuration

### Priority (low → high)

```
~/.nextme/nextme.json
  → {cwd}/nextme.json
    → ~/.nextme/settings.json
      → .env
        → NEXTME_* environment variables
```

### `nextme.json` fields

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | string | Feishu App ID |
| `app_secret` | string | Feishu App Secret |
| `projects` | array | Project list (`name` / `path` / `executor`) |
| `bindings` | object | Static chat→project bindings (`chat_id: project_name`) |

`executor` values:
- `"claude"` (default) — DirectClaudeRuntime, uses local `claude` CLI
- `"cc-acp"` — ACPRuntime, uses `cc-acp` subprocess (JSON-RPC 2.0)

**Multi-project example:**

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx",
  "projects": [
    {"name": "backend", "path": "/path/to/backend"},
    {"name": "frontend", "path": "/path/to/frontend"},
    {"name": "infra", "path": "/path/to/infra"}
  ],
  "bindings": {
    "oc_groupchat_devops": "infra"
  }
}
```

### `~/.nextme/settings.json` fields

| Field | Default | Description |
|-------|---------|-------------|
| `acp_idle_timeout_seconds` | `7200` | Idle timeout before ACPRuntime process is killed |
| `task_queue_capacity` | `1024` | Per-session task queue capacity |
| `memory_debounce_seconds` | `30` | State / memory flush debounce interval (s) |
| `context_max_bytes` | `1000000` | Context size threshold for compression |
| `context_compression` | `"zlib"` | Compression algorithm: `zlib` / `lzma` / `brotli` |
| `progress_debounce_seconds` | `3.0` | Progress card update debounce interval (s) |
| `permission_timeout_seconds` | `300.0` | Permission confirmation timeout (s) |
| `log_level` | `"INFO"` | Log verbosity |

### Environment variables

```bash
NEXTME_APP_ID=cli_xxx
NEXTME_APP_SECRET=xxx
NEXTME_LOG_LEVEL=INFO
NEXTME_ACP_IDLE_TIMEOUT_SECONDS=7200
```

---

## Custom Skills

Skills are discovered from four tiers (higher priority overrides lower):

| Priority | Directory | Label |
|----------|-----------|-------|
| 4 — highest | `{project_path}/.nextme/skills/*.md` | Project |
| 3 | `~/.nextme/skills/*.md` | NextMe Global |
| 2 | `~/.claude/skills/<name>/SKILL.md` | Global (claude executor only) |
| 1 — lowest | `{package}/skills/*.md` | Built-in |

The **Global** tier (`~/.claude/skills/`) is only scanned when at least one configured project uses `executor: "claude"`. Skills installed via Claude Code appear here automatically.

**NextMe / project skill format** (with `{user_input}` placeholder):

```markdown
---
name: My Skill
trigger: myskill
description: What this skill does
tools_allowlist: []
tools_denylist: []
---

You are a ...

User request: {user_input}
Context: {context}
```

**Claude global skill format** (trigger = directory name, no `{user_input}` placeholder):

```markdown
---
name: My Global Skill
description: What this skill does
allowed-tools: [bash, read]
---

You are a specialist in ...
```

When a global skill template has no `{user_input}` placeholder, NextMe appends `User request: <input>` automatically.

Invoke with `/skill myskill`.

---

## File Storage

```
~/.nextme/
├── nextme.json          # user-level config
├── settings.json        # behaviour settings
├── state.json           # session state (actual_id, active project)
├── nextme.pid           # PID file (used by nextme down)
├── memory/
│   └── {user_hash}/     # per-user memory (facts / preferences)
├── threads/
│   └── {session_id}/    # per-session context files (optionally compressed)
├── skills/              # user-defined skills
└── logs/nextme.log      # rolling log (10 MB × 5 backups)
```

---

## Architecture

```
Feishu User ──WebSocket──▶ FeishuClient
                                │
                          MessageHandler (LRU dedup)
                                │
                          TaskDispatcher
                           ├─ meta-command handling
                           ├─ permission reply routing
                           └─ enqueue regular messages
                                │
                    ┌───────────▼───────────┐
                    │   SessionWorker       │  ← one coroutine per session
                    │   serial task queue   │
                    └───────────┬───────────┘
                                │ PathLock (per-path mutex)
                                ▼
                     ACPRuntimeRegistry
                      ├─ executor="claude" → DirectClaudeRuntime
                      │   claude --print --output-format stream-json
                      │   [--resume session_id]
                      └─ executor="cc-acp" → ACPRuntime
                          JSON-RPC 2.0 over cc-acp subprocess
```

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.12+ |
| Package manager | uv + pyproject.toml |
| IM integration | lark-oapi, WebSocket long connection |
| Agent runtime | DirectClaudeRuntime (default) / ACPRuntime (optional) |
| Concurrency | asyncio (Queue + Lock + Task + Future) |
| Config validation | pydantic v2 + python-dotenv |
| Context compression | zlib / lzma (stdlib) / brotli (optional) |

---

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Lint
uv run ruff check src/

# Run tests
uv run pytest
```

---

## Multi-project Parallel Execution

NextMe assigns an independent asyncio worker to each `(user, project)` pair, so tasks for different projects run concurrently without blocking each other.

**Routing priority (highest → lowest):**

1. Static binding in `nextme.json` (`bindings` field)
2. Dynamic binding set via `/project bind <name>` (persisted in `state.json`)
3. User's current active project (`/project <name>`)
4. First project in the `projects` list (default)

---

## Session Persistence & Memory

**Session persistence** — The Claude session ID (`actual_id`) is saved to `~/.nextme/state.json` after each task. On bot restart, NextMe passes `--resume <id>` to the `claude` CLI so conversation history is seamlessly resumed.

**Long-term memory** — Use `/remember <text>` to save facts. Facts are stored at the **user level** and shared across all chats for the same user. On new sessions (not resumed ones), the top-10 highest-confidence facts are prepended to the task prompt automatically:

```
[Memory]
- I prefer Python over JavaScript
- Use pytest for tests

[Message]
<your message here>
```

---

## Roadmap

- **Phase 1 ✅** — Feishu WebSocket + agent subprocess + session isolation + streaming progress + permission confirmation
- **Phase 2 ✅** — Skills system, multi-project parallel, session persistence across restarts, long-term memory (`/remember`), context compression, path lock
- **Phase 3** — Config hot-reload, Slack / DingTalk adapter, multi-agent orchestration

---

## License

MIT
