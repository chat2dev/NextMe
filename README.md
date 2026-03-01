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
| Long-term memory | Facts stored per user (shared across all chats); injected as a numbered list into new sessions; agent can add / replace / delete facts via `<memory>` tags |
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

**1. Create `~/.nextme/settings.json`**

```bash
mkdir -p ~/.nextme
cp settings.json.example ~/.nextme/settings.json
```

Edit `~/.nextme/settings.json`:

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

Go to **Credentials & Basic Info**, copy **App ID** and **App Secret**, and paste them into `~/.nextme/settings.json`:

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
      "im:resource",
      "cardkit:card:write"
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
| `cardkit:card:write` | Create and update interactive cards (streaming progress) |
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

**Convenience scripts** (project root):

```bash
./start.sh           # wraps nextme up with checks and colored output
./stop.sh            # wraps nextme down
```

---

## Security

> **Read this before running NextMe in a shared or production environment.**

### Claude CLI permission flags

NextMe's `DirectClaudeRuntime` (executor `"claude"`) launches the local `claude` CLI with these flags:

```json
[
  "--print",
  "--output-format", "stream-json",
  "--verbose",
  "--dangerously-skip-permissions",
  "--include-partial-messages"
]
```

The critical flag is **`--dangerously-skip-permissions`**. It tells the Claude agent to auto-approve every tool call — Bash commands, file writes, network requests — **without pausing to ask for confirmation**. This is intentional for a bot environment (no human at the terminal), but it means:

- The agent can run **any shell command** in the configured project directory.
- The agent can **read and write files** within that directory (and anywhere the OS user has access).
- There is **no sandboxing** beyond the OS user's own permissions.

### Recommended hardening measures

Evaluate and apply what's appropriate for your environment:

| Measure | How |
|---------|-----|
| **Restrict who can send messages** | Add your own `open_id` to `admin_users` and enable the ACL feature (Phase 3) |
| **Run as a dedicated OS user** | Create a low-privilege user for NextMe; it inherits only that user's file permissions |
| **Limit project path** | Set `path` in `settings.json` to a narrow directory, not `/` or `~` |
| **Read-only volumes for sensitive dirs** | Mount sensitive directories as read-only for the NextMe process |
| **Review agent memory** | Periodically check `~/.nextme/memory/` to see what facts the agent has recorded |

### Environment variable handling

NextMe strips the following variables from the child `claude` process to prevent nested-session conflicts and credential leakage:

- `CLAUDECODE`, `CLAUDE_CODE_*` — prevents "nested session" errors
- `ANTHROPIC_AUTH_TOKEN` is **not** passed as `ANTHROPIC_API_KEY`; the inner `claude` uses its own `~/.claude.json` credentials

The child process inherits your full environment otherwise, including `ANTHROPIC_BASE_URL` for custom proxy endpoints.

### Claude Code permission settings (`~/.claude/settings.json`)

Beyond the `--dangerously-skip-permissions` flag above, the Claude Code CLI itself reads `~/.claude/settings.json` to apply a **global permission policy**. Rules here are enforced by the CLI regardless of what NextMe requests, making this a critical second line of defense.

Recommended baseline (`~/.claude/settings.json`):

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "deny": [
      "Bash(rm -rf *)",
      "Bash(rm -fr *)",
      "Bash(rm *)",
      "Bash(sudo *)",
      "Bash(doas *)",
      "Bash(* --force-with-leases*)",
      "Bash(* --hard *)",
      "Bash(chown -R *)",
      "Bash(find * -delete)",
      "Bash(find * -exec rm {})"
    ],
    "ask": [
      "Bash(git push *)",
      "Bash(npm publish *)",
      "Bash(pypi upload *)"
    ]
  }
}
```

| Setting | Effect |
|---------|--------|
| `defaultMode: "bypassPermissions"` | All unlisted tools are auto-approved — required for unattended bot use, but means you must explicitly list what to deny |
| `deny` rules | These patterns are **hard-blocked** even with `--dangerously-skip-permissions` — the agent can never execute matching commands |
| `ask` rules | These patterns surface a confirmation prompt; in a bot environment this effectively blocks them (no one watches the terminal) |

> **Security warning:** `defaultMode: "bypassPermissions"` with no `deny` rules means the agent can run arbitrary shell commands. Always populate the `deny` list with destructive-operation patterns before deploying NextMe in a shared environment.

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
~/.nextme/settings.json
  → {cwd}/nextme.json
    → .env
      → NEXTME_* environment variables
```

`~/.nextme/settings.json` is the single user-level config file. It holds both app credentials / project list **and** runtime behaviour settings. The optional `{cwd}/nextme.json` can add or override project-local entries.

### `~/.nextme/settings.json` fields

**App credentials & projects**

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | string | Feishu App ID |
| `app_secret` | string | Feishu App Secret |
| `projects` | array | Project list (`name` / `path` / `executor`) |
| `bindings` | object | Static chat→project bindings (`chat_id: project_name`) |
| `executor_args` | array | Extra arguments appended to the executor command (e.g. `["acp", "serve"]`) |

`executor` values:
- `"claude"` (default) — DirectClaudeRuntime, uses local `claude` CLI
- `"cc-acp"` — ACPRuntime, uses `cc-acp` subprocess (JSON-RPC 2.0)
- `"coco"` — ACPRuntime, uses `coco` subprocess (JSON-RPC 2.0 / ACP protocol); use `executor_args` for sub-commands

**Runtime behaviour**

| Field | Default | Description |
|-------|---------|-------------|
| `acp_idle_timeout_seconds` | `7200` | Idle timeout before ACPRuntime process is killed |
| `task_queue_capacity` | `1024` | Per-session task queue capacity |
| `memory_debounce_seconds` | `30` | State / memory flush debounce interval (s) |
| `memory_max_facts` | `100` | Maximum facts kept per user; lowest-confidence evicted first |
| `context_max_bytes` | `1000000` | Context size threshold for compression |
| `context_compression` | `"zlib"` | Compression algorithm: `zlib` / `lzma` / `brotli` |
| `progress_debounce_seconds` | `0.5` | Progress card update debounce interval (s) |
| `permission_auto_approve` | `false` | Auto-approve ACPRuntime permission requests without user confirmation |
| `log_level` | `"INFO"` | Log verbosity |

**Multi-project example (`~/.nextme/settings.json`):**

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx",
  "projects": [
    {"name": "backend", "path": "/path/to/backend"},
    {"name": "frontend", "path": "/path/to/frontend"},
    {"name": "infra", "path": "/path/to/infra"},
    {"name": "ai-agent", "path": "/path/to/ai", "executor": "coco", "executor_args": ["acp", "serve"]}
  ],
  "bindings": {
    "oc_groupchat_devops": "infra"
  }
}
```

### Environment variables

```bash
NEXTME_APP_ID=cli_xxx
NEXTME_APP_SECRET=xxx
NEXTME_LOG_LEVEL=INFO
NEXTME_ACP_IDLE_TIMEOUT_SECONDS=7200
```

---

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
├── settings.json        # single user-level config (credentials + projects + settings)
├── state.json           # session state (actual_id, active project, dynamic bindings)
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

1. Static binding in `~/.nextme/settings.json` (`bindings` field)
2. Dynamic binding set via `/project bind <name>` (persisted in `state.json`)
3. User's current active project (`/project <name>`)
4. First project in the `projects` list (default)

---

## Session Persistence & Memory

**Session persistence** — The Claude session ID (`actual_id`) is saved to `~/.nextme/state.json` after each task. On bot restart, NextMe passes `--resume <id>` to the `claude` CLI so conversation history is seamlessly resumed.

**Long-term memory** — Facts are stored at the **user level** (`~/.nextme/memory/{user_hash}/facts.json`) and shared across all chats for the same user. On new sessions (not resumed), the top-10 highest-confidence facts are injected into the task prompt via a Jinja2 template:

```
[User Memory] (2 facts; append <memory> tags to update)
0. Prefer Python over JavaScript
1. Use pytest for tests

Memory operations (use only when necessary):
- Add:    <memory>content</memory>
- Update: <memory op="replace" idx="0">new content</memory>
- Delete: <memory op="forget" idx="1"></memory>

[User Message]
<your message here>
```

The agent can actively manage memory by writing `<memory>` tags in its reply:

| Tag | Operation |
|-----|-----------|
| `<memory>text</memory>` | Add a new fact |
| `<memory op="replace" idx="0">new text</memory>` | Replace fact at index 0 |
| `<memory op="forget" idx="1"></memory>` | Delete fact at index 1 |

Tags are stripped before displaying the response to the user. Facts longer than 500 characters are kept visible and also recorded.

**Deduplication** — When adding a fact, if an existing fact has a similarity ratio > 0.85 (difflib `SequenceMatcher`), the two are merged in-place (higher-confidence text wins). At most `memory_max_facts` facts are kept; the lowest-confidence ones are evicted first.

**Custom template** — Override the injection format by creating `~/.nextme/prompts/memory.md` (Jinja2). Variables: `{{ count }}`, `{% for fact in facts %}{{ loop.index0 }}. {{ fact.text }}{% endfor %}`.

Use `/remember <text>` to add a fact directly from Feishu without waiting for the agent to write a `<memory>` tag.

---

## Stability & Auto-Recovery

NextMe is a long-running daemon. Below are recommended approaches to keep it online and self-healing.

### Option A — macOS launchd (recommended for Mac)

Create `~/Library/LaunchAgents/com.nextme.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.nextme</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd /path/to/NextMe &amp;&amp; uv run nextme up</string>
  </array>
  <key>KeepAlive</key>          <true/>          <!-- auto-restart on crash -->
  <key>RunAtLoad</key>          <true/>          <!-- start on login -->
  <key>StandardOutPath</key>    <string>/tmp/nextme.out</string>
  <key>StandardErrorPath</key>  <string>/tmp/nextme.err</string>
  <key>ThrottleInterval</key>   <integer>10</integer>  <!-- min 10s between restarts -->
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.nextme.plist   # start
launchctl unload ~/Library/LaunchAgents/com.nextme.plist # stop
```

### Option B — Linux systemd (recommended for servers)

Create `/etc/systemd/system/nextme.service`:

```ini
[Unit]
Description=NextMe Feishu Agent Bot
After=network.target

[Service]
Type=simple
User=nextme                          # dedicated low-privilege user
WorkingDirectory=/path/to/NextMe
ExecStart=/usr/local/bin/uv run nextme up
Restart=on-failure
RestartSec=10s
StartLimitIntervalSec=60
StartLimitBurst=5                    # max 5 restarts per minute
StandardOutput=journal
StandardError=journal
Environment=HOME=/home/nextme

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nextme   # start + enable on boot
sudo systemctl status nextme
sudo journalctl -u nextme -f         # follow logs
```

### Option C — Supervisor (cross-platform)

```ini
[program:nextme]
command=uv run nextme up
directory=/path/to/NextMe
autostart=true
autorestart=true
startretries=5
startsecs=5
stderr_logfile=/var/log/nextme.err.log
stdout_logfile=/var/log/nextme.out.log
```

```bash
supervisorctl reread && supervisorctl update
supervisorctl status nextme
```

### Built-in resilience features

| Feature | Behaviour |
|---------|-----------|
| WebSocket auto-reconnect | lark-oapi reconnects automatically on connection drop |
| Graceful shutdown | SIGTERM drains in-flight tasks before exit |
| PID file | `~/.nextme/nextme.pid` — `nextme down` uses it for clean stop |
| State persistence | Session IDs and bindings survive restarts via `state.json` |

---

## Roadmap

- **Phase 1 ✅** — Feishu WebSocket + agent subprocess + session isolation + streaming progress + permission confirmation
- **Phase 2 ✅** — Skills system, multi-project parallel, session persistence across restarts, long-term memory (`/remember` + agent-driven add/replace/delete), context compression, path lock
- **Phase 3** — Config hot-reload, Slack / DingTalk adapter, multi-agent orchestration

---

## License

MIT
