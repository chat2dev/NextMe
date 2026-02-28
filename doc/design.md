# NextMe Design Document — Feishu IM × Claude Code Agent Bot

## Context

The user needs a Python Bot that acts as a bridge between Feishu IM and the underlying AI Agent (Claude Code CLI). The bot receives user messages over a Feishu long-polling WebSocket connection, forwards them to the Claude Code Agent, and pushes the agent's execution results and status back to Feishu in real time.

---

## Technology Choices

| Layer | Technology |
|-------|------------|
| Language | Python 3.12+ |
| Package management | uv + pyproject.toml |
| IM integration | lark-oapi (Python SDK), WebSocket long connection |
| Agent communication | DirectClaudeRuntime (default): `claude --print --output-format stream-json`; ACPRuntime (alternative): JSON-RPC 2.0 over ndjson |
| Concurrency model | asyncio (I/O-bound, Queue + Lock + Task + Future) |
| Configuration | pydantic v2 + python-dotenv, multi-source priority |
| State persistence | `~/.nextme/state.json`, asyncio debounced writes (30s), atomic rename |
| Memory persistence | `~/.nextme/memory/{hash}/`, JSON files, asyncio debounced writes |
| Context compression | zlib (stdlib) / lzma (stdlib) / brotli (optional dependency) |
| Skills system | Markdown + YAML frontmatter files |

---

## Project Structure

```
nextme/
├── pyproject.toml
├── nextme.json.example          # configuration template
├── .env.example
├── doc/                         # design documents
│   └── design.md
├── skills/                      # built-in Skills
│   ├── review.md
│   ├── commit.md
│   ├── explain.md
│   ├── test.md
│   └── debug.md
└── src/nextme/
    ├── main.py                  # CLI entry point (nextme up / nextme down)
    ├── config/
    │   ├── loader.py            # multi-source config loading
    │   ├── schema.py            # AppConfig, Settings, GlobalState (pydantic)
    │   └── state_store.py       # ~/.nextme/state.json atomic read/write + debounced flush
    ├── feishu/
    │   ├── client.py            # WebSocket long connection management + reconnect
    │   ├── handler.py           # message/card event routing
    │   ├── reply.py             # FeishuReplier + card JSON construction (schema 2.0)
    │   └── dedup.py             # LRU message deduplication (1000 entries, 5min TTL)
    ├── core/
    │   ├── interfaces.py        # Replier / IMAdapter / AgentRuntime Protocol interfaces
    │   ├── dispatcher.py        # TaskDispatcher: routing / permission replies / meta-commands
    │   ├── session.py           # UserContext, Session, SessionRegistry
    │   ├── worker.py            # per-Session asyncio queue consumer coroutine
    │   ├── commands.py          # /new /stop /help /skill /status /project handlers
    │   └── path_lock.py         # physical path-level asyncio.Lock registry
    ├── acp/
    │   ├── direct_runtime.py    # DirectClaudeRuntime: direct claude CLI invocation (default)
    │   ├── runtime.py           # ACPRuntime: JSON-RPC 2.0 over cc-acp subprocess
    │   ├── client.py            # JSON-RPC ndjson message serialization / parsing
    │   ├── janitor.py           # ACPRuntimeRegistry + background cleanup of idle processes (2h)
    │   └── protocol.py          # JSON-RPC message construction helpers
    ├── memory/
    │   ├── manager.py           # MemoryManager: load/save/debounced flush
    │   └── schema.py            # UserMemory, Fact (pydantic)
    ├── context/
    │   ├── manager.py           # ContextManager: thread file I/O
    │   └── compression.py       # zlib/lzma/brotli compression strategies
    ├── skills/
    │   ├── registry.py          # SkillRegistry: scan + load markdown
    │   ├── loader.py            # YAML frontmatter parsing
    │   └── invoker.py           # skill prompt construction
    └── protocol/
        └── types.py             # Task, Reply, TaskStatus enums
```

---

## Decoupling Layer: IMAdapter / AgentRuntime Protocol Interfaces (`core/interfaces.py`)

### Design Goal

The `core/` layer has no direct dependency on the Feishu SDK or ACP implementation. Structural interfaces are declared via `typing.Protocol`, laying the groundwork for future support of Slack / DingTalk / other Agent CLIs.

### Interface Definitions

| Interface | Implementor | Method count | Purpose |
|-----------|-------------|--------------|---------|
| `Replier` | `FeishuReplier` | 4 async send + 5 sync build | Send messages, build card JSON |
| `IMAdapter` | `FeishuClient` | start / stop / get_replier | IM platform connection lifecycle |
| `AgentRuntime` | `DirectClaudeRuntime` / `ACPRuntime` | 3 read-only properties + 5 async methods | Agent subprocess lifecycle |

All interfaces are annotated `@runtime_checkable`, supporting `isinstance()` assertions and testing.

### Usage Locations

- `core/worker.py` — `replier: Replier`
- `core/dispatcher.py` — `feishu_client: IMAdapter`, `replier: Replier`
- `core/commands.py` — all `handle_*` function parameters `replier: Replier`

---

## Core Architecture Diagram

```
Feishu User ──WebSocket──▶ FeishuClient
                                │
                          MessageHandler
                           - dedup check (LRU, 5min TTL)
                           - parse chat_id, user_id, text
                                │
                          TaskDispatcher
                           - context_id = "chat_id:user_id"
                           - permission reply routing (digits 1/2/3)
                           - meta-command handling (/new /stop ...)
                                │
                          SessionRegistry.get_or_create(context_id)
                                │
                          session.task_queue.put(task)
                                │
                    ┌───────────▼───────────┐
                    │   Session Worker      │  (per-session asyncio Task)
                    │   serial Queue drain  │
                    └───────────┬───────────┘
                                │
                       PathLockRegistry.get(project_path)
                                │ (async acquire, prevents concurrent writes from multiple Sessions)
                                ▼
                       ACPRuntimeRegistry.get_or_create(executor, executor_args)
                        - executor="claude"              → DirectClaudeRuntime
                        - executor="cc-acp" / "coco"    → ACPRuntime (cmd = [executor, *executor_args])
                                │
              ┌─────────────────┴──────────────────┐
              │ DirectClaudeRuntime (default)        │ ACPRuntime (alternative)
              │ claude --print --output-format      │ JSON-RPC 2.0
              │   stream-json [--resume session_id] │ over cc-acp subprocess
              └─────────────────┬──────────────────┘
                                │ on_progress → Feishu progress card (3s debounce)
                                │ on_permission → block waiting for user confirmation
                                ▼
                       FeishuReplier.send_card(result)
```

---

## Agent Runtime Dual Backend

### Runtime Selection

`ACPRuntimeRegistry.get_or_create(executor=...)` routes based on the executor field:

| executor value | Runtime | Protocol |
|----------------|---------|---------|
| `"claude"` (default) | `DirectClaudeRuntime` | stream-json ndjson |
| `"cc-acp"` / `"claude-code-acp"` | `ACPRuntime` | JSON-RPC 2.0 |
| `"coco"` | `ACPRuntime` | JSON-RPC 2.0 / ACP |

`executor_args` (optional `list[str]`) appends extra arguments to the subprocess command. Example: `executor="coco"` + `executor_args=["acp", "serve"]` → runs `coco acp serve`.

Configuration example (`~/.nextme/settings.json`):
```json
{
  "projects": [
    {"name": "my-project",  "path": "/path/to/project", "executor": "claude"},
    {"name": "ai-agent",    "path": "/path/to/ai",      "executor": "coco", "executor_args": ["acp", "serve"]}
  ]
}
```

### DirectClaudeRuntime (default, `acp/direct_runtime.py`)

Directly invokes the locally installed `claude` CLI, bypassing cc-acp. Suitable for custom API proxy (`ANTHROPIC_BASE_URL`) scenarios.

**Launch command**:
```bash
claude --print --output-format stream-json --verbose \
       --dangerously-skip-permissions \
       [--resume <session_id>]   # used from the second call onwards to resume a conversation
```

**stream-json event types**:

| Event | Description |
|-------|-------------|
| `system` | Initialization event, carries `session_id`, `model`, tools list |
| `assistant` | Model text output, may be chunked |
| `tool_use` | Tool call, carries `name` |
| `tool_result` | Tool execution result |
| `result` | Final event, `is_error=true` or carries complete `result` text |
| `user` | User input echo (ignored) |

**Session resumption**: The `result` event carries `session_id`, stored in `_actual_id`; the next call appends `--resume <session_id>`.

**Environment isolation** (critical): The subprocess env must filter out `CLAUDECODE` and `CLAUDE_CODE_*` prefix variables to avoid "nested session" errors (when NextMe itself is launched inside a Claude Code terminal).

### ACPRuntime (alternative, `acp/runtime.py`)

Communicates via JSON-RPC 2.0 protocol through the `cc-acp` subprocess.

**JSON-RPC 2.0 flow**:
```
initialize → session/new (or session/load) → session/prompt
                ↑ Server→Client notifications: session/update
                ↑ Server→Client requests:      session/request_permission
```

**Environment isolation**: Same filtering of `CLAUDECODE`, `CLAUDE_CODE_*`; maps `ANTHROPIC_AUTH_TOKEN` (OAuth token) to `ANTHROPIC_API_KEY` for cc-acp SDK usage.

---

## Persistence Design

NextMe has three persistence layers with independent responsibilities:

```
~/.nextme/
├── state.json           # Session state (conversation ID, active project)
├── memory/{ctx_hash}/   # user long-term memory (facts)
├── threads/{session_id}/# context compressed files
├── nextme.pid           # PID file (prevents accidental kills)
└── logs/nextme.log      # rolling log (10MB × 5)
```

---

### 1. Session State Persistence (`config/state_store.py`)

#### Schema (`config/schema.py`)

```python
class ProjectState(BaseModel):
    salt: str = ""          # random value for deterministic session ID generation
    actual_id: str = ""     # session UUID returned by claude (used for --resume)
    executor: str = "claude"

class UserState(BaseModel):
    last_active_project: str = ""          # restore last active project
    projects: dict[str, ProjectState] = {} # project_name -> ProjectState

class GlobalState(BaseModel):
    contexts: dict[str, UserState] = {}    # context_id -> UserState
```

**File structure** (`~/.nextme/state.json`):
```json
{
  "contexts": {
    "oc_chatXXX:ou_userYYY": {
      "last_active_project": "nextme",
      "projects": {
        "nextme": {
          "actual_id": "1632fafa-a0b6-4f30-9190-466f9eec4faf",
          "executor": "claude"
        }
      }
    }
  }
}
```

#### StateStore Write Strategy

| Feature | Implementation |
|---------|----------------|
| Atomic write | temp file (same directory) + `os.replace()`, guaranteed atomic by POSIX `rename(2)` |
| Debounced flush | background asyncio Task, checks dirty flag every `memory_debounce_seconds` (default 30s) |
| Crash recovery | silently returns `GlobalState()` (empty state) if file is corrupted or invalid JSON — no crash |
| Graceful shutdown | `StateStore.stop()` flushes first then cancels the background Task |

#### session_id Lifecycle

```
Bot starts
  → StateStore.load()
  → [if state.json exists] UserState.projects["nextme"].actual_id loaded into memory
  → Session.actual_id = stored_actual_id

User sends message
  → worker calls DirectClaudeRuntime.execute()
  → claude returns result event carrying session_id
  → DirectClaudeRuntime._actual_id = session_id
  → Session.actual_id = runtime.actual_id
  → StateStore.set_user_state(...) → dirty = True
  → async write to disk within 30s

Bot restarts
  → restore actual_id from state.json
  → append --resume actual_id on next execute
  → conversation context seamlessly resumed
```

---

### 2. User Memory Persistence (`memory/`)

#### File Layout
```
~/.nextme/memory/{md5(context_id)}/
├── user_context.json     # user preferences, interaction style
├── personal.json         # name, timezone, role, etc.
└── facts.json            # learned facts (with confidence scores)
```

#### Facts Schema
```json
{
  "facts": [
    {
      "text": "用户主要使用 Python",
      "confidence": 0.95,
      "created_at": "2025-01-01T12:00:00",
      "source": "conversation"
    }
  ]
}
```

#### MemoryManager Write Strategy

Same as StateStore: in-memory updates are immediate, file writes are async via asyncio debounce (30s), atomic rename. On each Session start, the top-15 facts are injected into the agent system prompt.

---

### 3. Context Compression Persistence (`context/`)

```
~/.nextme/threads/{session_id}/
├── context.txt[.zlib|.lzma|.br]
└── context.meta.json   # {"algorithm": "zlib", "original_size": 1048576, ...}
```

| Algorithm | Stdlib | Speed | Compression ratio | Trigger condition |
|-----------|--------|-------|-------------------|-------------------|
| zlib | ✅ | fast | medium | default |
| lzma | ✅ | slow | high | context > 500KB |
| brotli | optional pip | medium | high | auto-evaluated when installed |

---

### 4. PID File (`~/.nextme/nextme.pid`)

The bot writes its current PID on startup and deletes the file on normal exit. `nextme down` reads the PID file to send SIGTERM, rather than using `pkill nextme` (which could accidentally kill unrelated processes). A stale PID file (process already dead) is automatically cleaned up.

---

## Session Lifecycle

### State Machine
```
new message → QUEUED → WAITING_LOCK → EXECUTING
                                         │
              ← permission_request → WAITING_PERMISSION
                                         │ (user replies 1/2/3)
                                         ↓
                                     EXECUTING
                                         │
                                  /stop → CANCELED → IDLE
                                  done  → IDLE
```

### Permission Flow
```
DirectClaudeRuntime (skip-permissions, no permission requests)
ACPRuntime sends session/request_permission
  → Session.perm_future = asyncio.Future()
  → send permission card to user (with numbered options)
  → worker await perm_future (blocks until user responds or /stop cancels)
User replies "1"
  → TaskDispatcher identifies as permission reply
  → perm_future.set_result(choice)
  → worker resumes execution, sends permission_response to ACP
```

### Session Lifecycle and ACPJanitor
- Each `context_id` + `project_name` combination corresponds to one Session
- DirectClaudeRuntime launches a new subprocess for each execute (no persistent process)
- ACPRuntime subprocess is persistent; background Janitor (checks every minute): auto-terminates after 2h of idle
- `/new` command clears `actual_id`, next execute starts a new conversation

---

## Key Data Structures

### `protocol/types.py`

```python
class TaskStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    WAITING_LOCK = "waiting_lock"
    EXECUTING = "executing"
    WAITING_PERMISSION = "waiting_permission"
    DONE = "done"
    CANCELED = "canceled"

@dataclass
class Task:
    id: str                   # UUID
    content: str              # user message
    session_id: str           # chatID:userID
    reply_fn: Callable        # async callback
    created_at: datetime
    timeout: timedelta = timedelta(hours=8)
    canceled: bool = False
```

### `core/session.py`

```python
class Session:
    context_id: str           # "chatID:userID"
    project_name: str
    project_path: Path
    executor: str             # "claude" / "cc-acp" / "coco"
    executor_args: list[str]  # extra args appended to executor command
    salt: str                 # random value for deterministic session ID generation
    actual_id: str            # session UUID returned by claude (used for --resume)
    status: TaskStatus
    task_queue: asyncio.Queue # capacity 1024
    pending_tasks: list[Task]
    active_task: Optional[Task]
    perm_future: Optional[asyncio.Future[PermissionChoice]]
    perm_options: list[PermOption]

class UserContext:
    context_id: str           # "chatID:userID"
    active_project: str
    sessions: dict[str, Session]  # project_name -> Session

class SessionRegistry:
    """Global singleton: context_id -> UserContext"""
```

---

## Message Deduplication and Concurrency Control

- **Message deduplication**: LRU cache (1000 entries, 5min TTL), based on Feishu `message_id`, prevents duplicate processing on WebSocket reconnects
- **Session serialization**: each Session has one asyncio worker, tasks execute serially
- **Cross-user parallelism**: different `context_id` values run fully in parallel (asyncio concurrent coroutines)
- **Path lock**: `PathLockRegistry`, only one Session may write to the same physical path at a time (prevents multiple users from concurrently writing to the same codebase)

---

## Meta-Commands (`/` prefix)

| Command | Function |
|---------|----------|
| `/new` | Clear actual_id (next conversation starts a new session) |
| `/stop` | Cancel the currently executing task (SIGTERM to subprocess) |
| `/help` | Display help card |
| `/skill <trigger>` | Manually trigger a Skill |
| `/status` | Display current Session state (project / executor / session_id) |
| `/project <name>` | Switch active project |

---

## Feishu Cards (schema 2.0)

All cards use Feishu interactive cards **schema 2.0**. Known limitations:

| Element | schema 2.0 support |
|---------|--------------------|
| `markdown` | ✅ |
| `hr` | ✅ |
| `action` / `button` | ✅ |
| `collapsible_panel` | ✅ |
| `note` | ❌ (deprecated, replaced by `markdown`) |

| Card type | Trigger | Color |
|-----------|---------|-------|
| Progress card | task start / content_delta | yellow |
| Result card | task complete | blue |
| Permission card | ACPRuntime permission_request | orange |
| Error card | execution failure | red |
| Help card | /help command | green |

---

## Skills System (`skills/`)

### Skill File Format
```markdown
---
name: Code Review
trigger: review
description: 对代码进行结构化 Review
tools_allowlist: []
tools_denylist: []
---

你是一位资深工程师。
用户请求：{user_input}
请从正确性、性能、可读性三个维度进行 Review。
```

### Discovery Priority (high → low)
1. `{project_path}/.nextme/skills/*.md` — project-local
2. `~/.nextme/skills/*.md` — NextMe global
3. `~/.claude/skills/<name>/SKILL.md` — executor global (claude executor only)
4. `{package_dir}/skills/*.md` — built-in

### Built-in Skills
| Trigger | Function |
|---------|----------|
| `/review` | Code Review |
| `/commit` | Generate Commit Message |
| `/explain` | Explain code |
| `/test` | Generate unit tests |
| `/debug` | Systematic debugging |

---

## Configuration System

### Priority (low → high)
```
~/.nextme/settings.json  →  {cwd}/nextme.json  →  .env  →  NEXTME_* environment variables
```

`~/.nextme/settings.json` is the single user-level config file containing both app credentials/project list and runtime behaviour. `{cwd}/nextme.json` is an optional project-local override layer (projects and bindings are merged; other fields override).

### `~/.nextme/settings.json`
```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxxxx",
  "projects": [
    {"name": "my-api",    "path": "/abs/path/to/project", "executor": "claude"},
    {"name": "ai-agent",  "path": "/abs/path/to/ai",      "executor": "coco", "executor_args": ["acp", "serve"]}
  ],
  "acp_idle_timeout_seconds": 7200,
  "task_queue_capacity": 1024,
  "memory_debounce_seconds": 30,
  "context_max_bytes": 1000000,
  "context_compression": "zlib",
  "log_level": "INFO",
  "progress_debounce_seconds": 0.5,
  "permission_auto_approve": false
}
```

---

## Startup and Graceful Shutdown (`main.py`)

```
nextme up [--directory DIR] [--executor EXE] [--log-level LEVEL]
nextme down [--timeout SECS]

Startup sequence:
1. Load configuration (AppConfig + Settings)
2. Write PID file
3. Initialize StateStore → load()
4. Initialize MemoryManager, ContextManager, SkillRegistry
5. Initialize SessionRegistry, PathLockRegistry
6. Initialize ACPRuntimeRegistry, ACPJanitor
7. Initialize MessageHandler, TaskDispatcher, FeishuClient
8. Start background tasks: janitor.run(), state_store.start_debounce_loop(),
                           memory_manager.start_debounce_loop()
9. Register SIGTERM/SIGINT handlers
10. FeishuClient.start()  ← blocks, waiting for signal

SIGTERM / SIGINT graceful shutdown:
1. Stop Feishu WebSocket
2. Wait for in-flight tasks to complete (or force exit after 30s timeout)
3. ACPRuntimeRegistry.stop_all()
4. MemoryManager.flush_all()
5. StateStore.stop() (flush + cancel debounce Task)
6. Cancel background asyncio.Tasks
7. Delete PID file
```

---

## File Storage Layout

```
~/.nextme/
├── settings.json        # single user-level config (credentials + projects + behaviour settings)
├── state.json           # Session state (actual_id, active_project, dynamic bindings)
├── nextme.pid           # PID file (nextme down targets SIGTERM here)
├── memory/
│   └── {md5(ctx_id)}/   # per-user long-term memory
│       ├── facts.json
│       ├── personal.json
│       └── user_context.json
├── threads/
│   └── {session_id}/    # per-session context compressed files
│       ├── context.txt[.zlib|.lzma|.br]
│       └── context.meta.json
├── skills/              # user-defined Skills (override built-ins)
└── logs/nextme.log      # rolling log (10MB × 5 backups)
```

---

## Key Dependencies

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "lark-oapi>=1.5.3",     # Feishu Python SDK
    "pydantic>=2.0",        # config/data validation
    "python-dotenv>=1.0",   # .env loading
    # no acp-sdk needed — protocol layer is self-implemented
]

[project.optional-dependencies]
brotli = ["brotli>=1.0"]    # optional: brotli compression
```

External tool dependencies (must be installed by the user):
- `claude` (`npm install -g @anthropic-ai/claude-code`, v2+ recommended)
- `cc-acp` (optional, `npm install -g @zed-industries/claude-code-acp`)
- `coco` (optional, ACP-compatible code agent CLI; invoke with `executor_args: ["acp", "serve"]`)

---

## Verification Checklist

1. **Feishu connection**: after `nextme up`, send a message to the bot — bot shows a "思考中..." progress card
2. **Agent execution**: send `"列出当前目录的文件"` → bot returns a result card
3. **Streaming progress**: during a long task, see progress card content update every 3s
4. **Session resumption**: send two consecutive messages; the second one appends `--resume session_id` (confirm in logs)
5. **Restart recovery**: restart the bot and send a message; verify that `--resume` carries the previous `session_id`
6. **Session isolation**: two different users send messages simultaneously; they do not interfere with each other
7. **Permission confirmation** (cc-acp only): when the agent performs a write operation, Feishu shows a permission selection card
8. **Skills invocation**: `/review` triggers the Code Review Skill; agent returns a structured review result
9. **Safe shutdown**: `nextme down` sends SIGTERM → bot waits for in-flight tasks to complete before exiting
