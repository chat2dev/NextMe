# NextMe 设计文档 — Feishu IM × Claude Code Agent Bot

## Context

用户需要一个 Python Bot，作为飞书 IM 与底层 AI Agent（Claude Code CLI）之间的桥接层。Bot 通过飞书长连接接收用户消息，将消息转发给 Claude Code Agent，并将 Agent 的执行结果和状态实时推送回飞书。

---

## 技术选型

| 层次 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| 包管理 | uv + pyproject.toml |
| IM 集成 | lark-oapi (Python SDK)，WebSocket 长连接 |
| Agent 通信 | DirectClaudeRuntime（默认）：`claude --print --output-format stream-json`；ACPRuntime（备选）：JSON-RPC 2.0 over ndjson |
| 并发模型 | asyncio（I/O 密集型，Queue + Lock + Task + Future） |
| 配置 | pydantic v2 + python-dotenv，多源优先级 |
| 状态持久化 | `~/.nextme/state.json`，asyncio 去抖写入（30s），原子 rename |
| 内存持久化 | `~/.nextme/memory/{hash}/`，JSON 文件，asyncio 去抖写入 |
| 上下文压缩 | zlib（标准库）/ lzma（标准库）/ brotli（可选依赖） |
| Skills 系统 | Markdown + YAML frontmatter 文件 |

---

## 项目结构

```
nextme/
├── pyproject.toml
├── settings.json.example        # 配置模板
├── .env.example
├── doc/                         # 设计文档
│   └── design.md
├── skills/                      # 内置 Skills
│   ├── review.md
│   ├── commit.md
│   ├── explain.md
│   ├── test.md
│   └── debug.md
└── src/nextme/
    ├── main.py                  # CLI 入口 (nextme up / nextme down)
    ├── config/
    │   ├── loader.py            # 多源配置加载
    │   ├── schema.py            # AppConfig, Settings, GlobalState (pydantic)
    │   └── state_store.py       # ~/.nextme/state.json 原子读写 + 去抖刷新
    ├── feishu/
    │   ├── client.py            # WebSocket 长连接管理 + 重连
    │   ├── handler.py           # 消息/卡片事件路由
    │   ├── reply.py             # FeishuReplier + 卡片 JSON 构建 (schema 2.0)
    │   └── dedup.py             # LRU 消息去重（1000条，5min TTL）
    ├── core/
    │   ├── interfaces.py        # Replier / IMAdapter / AgentRuntime Protocol 接口
    │   ├── dispatcher.py        # TaskDispatcher：路由 / 权限回复 / 元命令
    │   ├── session.py           # UserContext, Session, SessionRegistry
    │   ├── worker.py            # 每个 Session 的 asyncio 队列消费协程
    │   ├── commands.py          # /new /stop /help /skill /status /project 处理
    │   └── path_lock.py         # 物理路径级 asyncio.Lock 注册表
    ├── acp/
    │   ├── direct_runtime.py    # DirectClaudeRuntime：claude CLI 直接调用（默认）
    │   ├── runtime.py           # ACPRuntime：JSON-RPC 2.0 over cc-acp 子进程
    │   ├── client.py            # JSON-RPC ndjson 消息序列化 / 解析
    │   ├── janitor.py           # ACPRuntimeRegistry + 后台清理空闲进程（2h）
    │   └── protocol.py          # JSON-RPC 消息构造辅助函数
    ├── memory/
    │   ├── manager.py           # MemoryManager：加载/保存/去抖刷新
    │   └── schema.py            # UserMemory, Fact (pydantic)
    ├── context/
    │   ├── manager.py           # ContextManager：线程文件 I/O
    │   └── compression.py       # zlib/lzma/brotli 压缩策略
    ├── skills/
    │   ├── registry.py          # SkillRegistry：扫描 + 加载 markdown
    │   ├── loader.py            # YAML frontmatter 解析
    │   └── invoker.py           # 构建 skill prompt
    └── protocol/
        └── types.py             # Task, Reply, TaskStatus 枚举
```

---

## 解耦层：IMAdapter / AgentRuntime Protocol 接口 (`core/interfaces.py`)

### 设计目标

`core/` 层不直接依赖飞书 SDK 或 ACP 实现，通过 `typing.Protocol` 声明结构化接口，为未来支持 Slack / 钉钉 / 其他 Agent CLI 奠定基础。

### 接口定义

| 接口 | 实现者 | 方法数 | 用途 |
|------|--------|--------|------|
| `Replier` | `FeishuReplier` | 4 async send + 5 sync build | 发送消息、构建卡片 JSON |
| `IMAdapter` | `FeishuClient` | start / stop / get_replier | IM 平台连接生命周期 |
| `AgentRuntime` | `DirectClaudeRuntime` / `ACPRuntime` | 3 read-only属性 + 5 async方法 | Agent 子进程生命周期 |

所有接口均标注 `@runtime_checkable`，支持 `isinstance()` 断言与测试。

### 使用位置

- `core/worker.py` — `replier: Replier`
- `core/dispatcher.py` — `feishu_client: IMAdapter`, `replier: Replier`
- `core/commands.py` — 所有 `handle_*` 函数参数 `replier: Replier`

---

## 核心架构图

```
Feishu User ──WebSocket──▶ FeishuClient
                                │
                          MessageHandler
                           - 去重检查（LRU, 5min TTL）
                           - 解析 chat_id, user_id, text
                                │
                          TaskDispatcher
                           - context_id = "chat_id:user_id"
                           - 权限回复路由（数字 1/2/3）
                           - 元命令处理（/new /stop ...）
                                │
                          SessionRegistry.get_or_create(context_id)
                                │
                          session.task_queue.put(task)
                                │
                    ┌───────────▼───────────┐
                    │   Session Worker      │  (per-session asyncio Task)
                    │   串行消费 Queue      │
                    └───────────┬───────────┘
                                │
                       PathLockRegistry.get(project_path)
                                │ (async acquire，防多 Session 并发写)
                                ▼
                       ACPRuntimeRegistry.get_or_create(executor)
                        - executor="claude" → DirectClaudeRuntime
                        - executor="cc-acp" → ACPRuntime
                                │
              ┌─────────────────┴──────────────────┐
              │ DirectClaudeRuntime（默认）          │ ACPRuntime（备选）
              │ claude --print --output-format      │ JSON-RPC 2.0
              │   stream-json [--resume session_id] │ over cc-acp subprocess
              └─────────────────┬──────────────────┘
                                │ on_progress → 飞书进度卡片（3s 去抖）
                                │ on_permission → 阻塞等待用户确认
                                ▼
                       FeishuReplier.send_card(result)
```

---

## Agent Runtime 双后端

### 运行时选择

`ACPRuntimeRegistry.get_or_create(executor=...)` 根据 executor 字段路由：

| executor 值 | 运行时 | 协议 |
|-------------|--------|------|
| `"claude"` (默认) | `DirectClaudeRuntime` | stream-json ndjson |
| `"cc-acp"` / `"claude-code-acp"` | `ACPRuntime` | JSON-RPC 2.0 |

配置示例（`~/.nextme/settings.json`）：
```json
{
  "projects": [
    {"name": "my-project", "path": "/path/to/project", "executor": "claude"}
  ]
}
```

### DirectClaudeRuntime（默认，`acp/direct_runtime.py`）

直接调用本地安装的 `claude` CLI，绕过 cc-acp。适用于自定义 API 代理（`ANTHROPIC_BASE_URL`）场景。

**启动命令**：
```bash
claude --print --output-format stream-json --verbose \
       --dangerously-skip-permissions \
       [--resume <session_id>]   # 第二次起用于续接对话
```

**stream-json 事件类型**：

| 事件 | 说明 |
|------|------|
| `system` | 初始化事件，携带 `session_id`、`model`、tools 列表 |
| `assistant` | 模型文本输出，可能分块 |
| `tool_use` | 工具调用，携带 `name` |
| `tool_result` | 工具执行结果 |
| `result` | 最终事件，`is_error=true` 或携带完整 `result` 文本 |
| `user` | 用户输入回显（忽略） |

**session 续接**：`result` 事件携带 `session_id`，存入 `_actual_id`，下次调用追加 `--resume <session_id>`。

**环境隔离**（关键）：子进程 env 必须过滤 `CLAUDECODE` 和 `CLAUDE_CODE_*` 前缀变量，避免 "nested session" 错误（当 NextMe 本身在 Claude Code 终端内启动时）。

### ACPRuntime（备选，`acp/runtime.py`）

通过 `cc-acp` 子进程使用 JSON-RPC 2.0 协议通信。

**JSON-RPC 2.0 流程**：
```
initialize → session/new (or session/load) → session/prompt
                ↑ Server→Client notifications: session/update
                ↑ Server→Client requests:      session/request_permission
```

**环境隔离**：同样过滤 `CLAUDECODE`、`CLAUDE_CODE_*`；将 `ANTHROPIC_AUTH_TOKEN`（OAuth token）映射为 `ANTHROPIC_API_KEY` 供 cc-acp SDK 使用。

---

## 持久化设计

NextMe 有三层持久化，职责独立：

```
~/.nextme/
├── state.json           # Session 状态（对话 ID、活跃项目）
├── memory/{ctx_hash}/   # 用户长期记忆（facts）
├── threads/{session_id}/# 上下文压缩文件
├── nextme.pid           # PID 文件（防误杀）
└── logs/nextme.log      # 滚动日志（10MB × 5）
```

---

### 1. Session 状态持久化（`config/state_store.py`）

#### Schema（`config/schema.py`）

```python
class ProjectState(BaseModel):
    salt: str = ""          # 确定性 session ID 生成用随机数
    actual_id: str = ""     # claude 返回的 session UUID（用于 --resume）
    executor: str = "claude"

class UserState(BaseModel):
    last_active_project: str = ""          # 恢复上次活跃项目
    projects: dict[str, ProjectState] = {} # project_name -> ProjectState

class GlobalState(BaseModel):
    contexts: dict[str, UserState] = {}    # context_id -> UserState
```

**文件结构**（`~/.nextme/state.json`）：
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

#### StateStore 写入策略

| 特性 | 实现 |
|------|------|
| 原子写入 | temp file（同目录）+ `os.replace()`，POSIX `rename(2)` 原子性保证 |
| 去抖刷新 | 后台 asyncio Task，每 `memory_debounce_seconds`（默认 30s）检查 dirty 标志 |
| 崩溃恢复 | 文件损坏或 JSON 非法时静默返回 `GlobalState()`（空状态），不崩溃 |
| 优雅停机 | `StateStore.stop()` 先 flush 再取消后台 Task |

#### session_id 生命周期

```
Bot 启动
  → StateStore.load()
  → [若 state.json 存在] UserState.projects["nextme"].actual_id 读入内存
  → Session.actual_id = stored_actual_id

用户发消息
  → worker 调用 DirectClaudeRuntime.execute()
  → claude 返回 result 事件携带 session_id
  → DirectClaudeRuntime._actual_id = session_id
  → Session.actual_id = runtime.actual_id
  → StateStore.set_user_state(...) → dirty = True
  → 30s 内异步写盘

Bot 重启
  → 从 state.json 恢复 actual_id
  → 下次 execute 时追加 --resume actual_id
  → 对话上下文无缝续接
```

---

### 2. 用户记忆持久化（`memory/`）

#### 文件布局
```
~/.nextme/memory/{md5(context_id)}/
├── user_context.json     # 用户偏好、交互风格
├── personal.json         # 姓名、时区、角色等
└── facts.json            # 已学习事实（含置信度）
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

#### MemoryManager 写入策略

与 StateStore 相同：内存立即更新，文件通过 asyncio 去抖（30s）异步写入，原子 rename。每次 Session 启动时注入 top-15 facts 到 agent system prompt。

---

### 3. 上下文压缩持久化（`context/`）

```
~/.nextme/threads/{session_id}/
├── context.txt[.zlib|.lzma|.br]
└── context.meta.json   # {"algorithm": "zlib", "original_size": 1048576, ...}
```

| 算法 | 标准库 | 速度 | 压缩率 | 触发条件 |
|------|--------|------|--------|----------|
| zlib | ✅ | 快 | 中 | 默认 |
| lzma | ✅ | 慢 | 高 | 上下文 > 500KB |
| brotli | 可选 pip | 中 | 高 | 安装后自动评估 |

---

### 4. PID 文件（`~/.nextme/nextme.pid`）

Bot 启动时写入当前 PID，正常退出时删除。`nextme down` 读取 PID 文件发送 SIGTERM，而非 `pkill nextme`（避免误杀同名进程）。PID 文件陈旧（进程已死）时自动清除。

---

## Session 生命周期

### 状态机
```
新消息 → QUEUED → WAITING_LOCK → EXECUTING
                                     │
              ← permission_request → WAITING_PERMISSION
                                     │ (用户回复 1/2/3)
                                     ↓
                                 EXECUTING
                                     │
                              /stop → CANCELED → IDLE
                              done  → IDLE
```

### 权限流程
```
DirectClaudeRuntime（skip-permissions，无权限请求）
ACPRuntime 发送 session/request_permission
  → Session.perm_future = asyncio.Future()
  → 发送权限卡片给用户（带编号选项）
  → worker await perm_future（持续等待，直到用户响应或 /stop 取消）
用户回复 "1"
  → TaskDispatcher 识别为权限回复
  → perm_future.set_result(choice)
  → worker 恢复执行，发送 permission_response 给 ACP
```

### Session 生命周期与 ACPJanitor
- 每个 `context_id` + `project_name` 组合对应一个 Session
- DirectClaudeRuntime 每次 execute 启动新子进程（无常驻进程）
- ACPRuntime 子进程常驻，后台 Janitor（每分钟检查）：空闲 2h 后自动终止
- `/new` 命令清除 `actual_id`，下次 execute 开启新对话

---

## 关键数据结构

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
    content: str              # 用户消息
    session_id: str           # chatID:userID
    reply_fn: Callable        # 异步回调
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
    executor: str             # "claude" (默认) 或 "cc-acp"
    salt: str                 # 生成确定性 session ID 的随机数
    actual_id: str            # claude 返回的 session UUID（--resume 用）
    status: TaskStatus
    task_queue: asyncio.Queue # 容量 1024
    pending_tasks: list[Task]
    active_task: Optional[Task]
    perm_future: Optional[asyncio.Future[PermissionChoice]]
    perm_options: list[PermOption]

class UserContext:
    context_id: str           # "chatID:userID"
    active_project: str
    sessions: dict[str, Session]  # project_name -> Session

class SessionRegistry:
    """全局单例：context_id -> UserContext"""
```

---

## 消息去重与并发控制

- **消息去重**：LRU 缓存（1000条，5min TTL），基于 Feishu `message_id`，防止 WebSocket 重连时重复处理
- **Session 串行**：每个 Session 有一个 asyncio worker，任务串行执行
- **跨用户并行**：不同 `context_id` 完全并行（asyncio 并发协程）
- **路径锁**：`PathLockRegistry`，同一物理路径同时只允许一个 Session 写入（防多用户同写同一代码库）

---

## 元命令 (`/` 开头)

| 命令 | 功能 |
|------|------|
| `/new` | 清除 actual_id（下次对话开新 session） |
| `/stop` | 取消当前执行中的任务（SIGTERM 子进程） |
| `/help` | 显示帮助卡片 |
| `/skill <trigger>` | 手动触发 Skill |
| `/status` | 显示当前 Session 状态（project / executor / session_id） |
| `/project <name>` | 切换活跃项目 |

---

## Feishu 卡片（schema 2.0）

所有卡片使用 Feishu 互动卡片 **schema 2.0**。已知限制：

| 标签 | schema 2.0 支持 |
|------|-----------------|
| `markdown` | ✅ |
| `hr` | ✅ |
| `action` / `button` | ✅ |
| `collapsible_panel` | ✅ |
| `note` | ❌（已废弃，改用 `markdown`） |

| 卡片类型 | 触发时机 | 颜色 |
|----------|----------|------|
| 进度卡片 | 任务开始 / content_delta | 黄色 |
| 结果卡片 | 任务完成 | 蓝色 |
| 权限卡片 | ACPRuntime permission_request | 橙色 |
| 错误卡片 | 执行失败 | 红色 |
| 帮助卡片 | /help 命令 | 绿色 |

---

## Skills 系统 (`skills/`)

### Skill 文件格式
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

### 发现优先级（高→低）
1. `{project_path}/.nextme/skills/*.md`
2. `~/.nextme/skills/*.md`
3. `{package_dir}/skills/*.md`（内置）

### 内置 Skills
| 触发词 | 功能 |
|--------|------|
| `/review` | 代码 Review |
| `/commit` | 生成 Commit Message |
| `/explain` | 解释代码 |
| `/test` | 生成单元测试 |
| `/debug` | 系统化调试 |

---

## 配置系统

### 优先级（低→高）
```
~/.nextme/settings.json  →  {cwd}/nextme.json  →  .env  →  NEXTME_* 环境变量
```

`~/.nextme/settings.json` 是唯一的用户级配置文件，同时包含应用凭证/项目列表和运行时行为设置。`{cwd}/nextme.json` 是可选的项目本地覆盖层（projects 和 bindings 做合并，其他字段直接覆盖）。

### `~/.nextme/settings.json`
```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxxxx",
  "projects": [
    {"name": "my-api", "path": "/abs/path/to/project", "executor": "claude"},
    {"name": "ai-agent", "path": "/abs/path/to/ai", "executor": "coco", "executor_args": ["acp", "serve"]}
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

## 启动与优雅停机 (`main.py`)

```
nextme up [--directory DIR] [--executor EXE] [--log-level LEVEL]
nextme down [--timeout SECS]

启动顺序：
1. 加载配置（AppConfig + Settings）
2. 写 PID 文件
3. 初始化 StateStore → load()
4. 初始化 MemoryManager, ContextManager, SkillRegistry
5. 初始化 SessionRegistry, PathLockRegistry
6. 初始化 ACPRuntimeRegistry, ACPJanitor
7. 初始化 MessageHandler, TaskDispatcher, FeishuClient
8. 启动后台任务：janitor.run(), state_store.start_debounce_loop(),
               memory_manager.start_debounce_loop()
9. 注册 SIGTERM/SIGINT 处理器
10. FeishuClient.start()  ← 阻塞，等待信号

SIGTERM / SIGINT 优雅停机：
1. 停止飞书 WebSocket
2. 等待 in-flight 任务完成（或超时 30s 强制退出）
3. ACPRuntimeRegistry.stop_all()
4. MemoryManager.flush_all()
5. StateStore.stop()（flush + 取消去抖 Task）
6. 取消后台 asyncio.Task
7. 删除 PID 文件
```

---

## 文件存储布局

```
~/.nextme/
├── settings.json        # 用户级配置（凭证 + 项目 + 运行时设置）
├── state.json           # Session 状态（actual_id, active_project, 动态绑定）
├── nextme.pid           # PID 文件（nextme down 定向 SIGTERM）
├── memory/
│   └── {md5(ctx_id)}/   # per-user 长期记忆
│       ├── facts.json
│       ├── personal.json
│       └── user_context.json
├── threads/
│   └── {session_id}/    # per-session 上下文压缩文件
│       ├── context.txt[.zlib|.lzma|.br]
│       └── context.meta.json
├── skills/              # 用户自定义 Skills（覆盖内置）
└── logs/nextme.log      # 滚动日志（10MB × 5 backup）
```

---

## 关键依赖

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "lark-oapi>=1.5.3",     # 飞书 Python SDK
    "pydantic>=2.0",        # 配置/数据校验
    "python-dotenv>=1.0",   # .env 加载
    # 无需 acp-sdk，协议层自行实现
]

[project.optional-dependencies]
brotli = ["brotli>=1.0"]    # 可选：brotli 压缩
```

外部工具依赖（需用户自行安装）：
- `claude`（`npm install -g @anthropic-ai/claude-code`，推荐 v2+）
- `cc-acp`（可选备选，`npm install -g @zed-industries/claude-code-acp`）

---

## 验证方式

1. **飞书连接**：`nextme up` 后向 Bot 发消息，Bot 出现"思考中..."进度卡片
2. **Agent 执行**：发送 `"列出当前目录的文件"` → Bot 返回结果卡片
3. **流式进度**：长任务中每 3s 看到进度卡片内容更新
4. **Session 续接**：连续发两条消息，第二条带上 `--resume session_id`（日志确认）
5. **重启恢复**：重启 Bot 后发消息，观察 `--resume` 是否携带上次的 `session_id`
6. **Session 隔离**：两个不同用户同时发消息，互不干扰
7. **权限确认**（仅 cc-acp）：Agent 执行写操作时，飞书弹出权限选择卡片
8. **Skills 调用**：`/review` 触发 Code Review Skill，Agent 返回结构化 Review 结果
9. **安全停机**：`nextme down` 发送 SIGTERM → Bot 等待 in-flight 任务完成后退出
