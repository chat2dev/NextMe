# NextMe 设计文档 — Feishu IM × Claude Code Agent Bot

## Context

用户需要一个 Python Bot，作为飞书 IM 与底层 AI Agent（MVP 只支持 Claude Code CLI）之间的桥接层。Bot 通过飞书长连接接收用户消息，将消息转发给 Claude Code Agent（通过 ACP 协议通信），并将 Agent 的执行结果和状态实时推送回飞书。

参考项目：
- `/Users/bytedance/develop/agent/open-jieli`（Go，飞书 + ACP + 多 Agent）
- `/Users/bytedance/develop/agent/deer-flow`（Python，LangGraph 多智能体框架）

---

## 技术选型

| 层次 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| 包管理 | uv + pyproject.toml |
| IM 集成 | lark-oapi (Python SDK)，WebSocket 长连接 |
| Agent 通信 | ACP 协议，`claude-code-acp` 子进程（zed-industries 适配器），ndjson over stdin/stdout，**自行实现协议解析**（不依赖 SDK） |
| 并发模型 | asyncio（I/O 密集型，Queue + Lock + Task + Future） |
| 配置 | pydantic v2 + python-dotenv，多源优先级 |
| 内存持久化 | JSON 文件，asyncio 去抖写入（30s） |
| 上下文压缩 | zlib（标准库）/ lzma（标准库）/ brotli（可选依赖） |
| Skills 系统 | Markdown + YAML frontmatter 文件 |

---

## 项目结构

```
nextme/
├── pyproject.toml
├── nextme.json.example          # 配置模板
├── .env.example
├── doc/                         # 设计文档
│   └── design.md
├── skills/                      # 内置 Skills
│   ├── review.md
│   ├── commit.md
│   └── explain.md
└── src/nextme/
    ├── main.py                  # CLI 入口 (nextme up)
    ├── config/
    │   ├── loader.py            # 多源配置加载
    │   ├── schema.py            # AppConfig, Settings (pydantic)
    │   └── state_store.py       # ~/.nextme/state.json 持久化
    ├── feishu/
    │   ├── client.py            # WebSocket 长连接管理 + 重连
    │   ├── handler.py           # 消息/卡片事件路由
    │   ├── reply.py             # Reply 类型 + 卡片 JSON 构建
    │   └── dedup.py             # LRU 消息去重（1000条，5min TTL）
    ├── core/
    │   ├── interfaces.py        # Replier / IMAdapter / AgentRuntime Protocol 接口
    │   ├── dispatcher.py        # TaskDispatcher：路由 / 权限回复 / 元命令
    │   ├── session.py           # UserContext, Session, SessionRegistry
    │   ├── worker.py            # 每个 Session 的 asyncio 队列消费协程
    │   ├── commands.py          # /new /stop /help /skill 等元命令处理
    │   └── path_lock.py         # 物理路径级 asyncio.Lock 注册表
    ├── acp/
    │   ├── runtime.py           # ACPRuntime：子进程生命周期管理
    │   ├── client.py            # ndjson 消息解析 + 回调分发
    │   ├── janitor.py           # 后台清理空闲 ACP 进程（2h 超时）
    │   └── protocol.py          # ACP 消息类型定义
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

`core/` 层不直接依赖飞书 SDK 或 ACP 实现，通过 `typing.Protocol` 声明结构化接口，**仅换类型标注，不改运行逻辑**。为未来支持 Slack / 钉钉 / 其他 Agent CLI 奠定基础。

### 接口定义

| 接口 | 实现者 | 方法数 | 用途 |
|------|--------|--------|------|
| `Replier` | `FeishuReplier` | 4 async send + 5 sync build | 发送消息、构建卡片 JSON |
| `IMAdapter` | `FeishuClient` | start / stop / get_replier | IM 平台连接生命周期 |
| `AgentRuntime` | `ACPRuntime` | 3 read-only属性 + 5 async方法 | Agent 子进程生命周期 |

所有接口均标注 `@runtime_checkable`，支持 `isinstance()` 断言与测试。

### 使用位置

- `core/worker.py` — `replier: Replier`（原 `FeishuReplier`）
- `core/dispatcher.py` — `feishu_client: IMAdapter`, `replier: Replier`
- `core/commands.py` — 所有 `handle_*` 函数参数 `replier: Replier`

### 验证方式

`tests/test_interfaces.py` 包含 35 个测试：
- `isinstance(FeishuReplier(...), Replier)` → `True`
- `isinstance(FeishuClient(...), IMAdapter)` → `True`
- `isinstance(ACPRuntime(...), AgentRuntime)` → `True`
- 缺少关键方法的对象 `isinstance` 返回 `False`（负例验证）

---

## 核心架构图

```
Feishu User ──WebSocket──▶ FeishuClient
                                │
                          MessageHandler
                           - 去重检查
                           - 解析 chat_id, user_id, text
                                │
                          TaskDispatcher
                           - context_id = "chat_id:user_id"
                           - 权限回复路由
                           - 元命令处理 (/new /stop ...)
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
                                │ (async acquire)
                                ▼
                       ACPRuntime.ensure_ready()
                        - 启动: claude acp serve
                        - 等待 ndjson READY 信号
                                │
                       ACPRuntime.execute(task,
                            on_progress → 飞书中间卡片（3s 去抖）
                            on_permission → 阻塞等待用户确认)
                                │
                    ndjson stream: content_delta / tool_use /
                    permission_request / done
                                │
                       FeishuClient.send_reply(final result)
```

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
    project_name: str
    project_path: Path
    executor: str             # "claude"
    salt: str                 # 生成确定性 session ID 的随机数
    actual_id: str            # ACP agent 返回的 session UUID
    status: TaskStatus
    task_queue: asyncio.Queue # 容量 1024
    pending_tasks: list[Task]
    active_task: Optional[Task]
    # 权限门：阻塞 worker 直到用户作出选择
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

## Session 生命周期状态机

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

---

## ACP 运行时管理 (`acp/runtime.py`)

### 启动命令
```bash
claude-code-acp   # zed-industries/claude-code-acp 适配器
```
通过 `asyncio.create_subprocess_exec` 启动，`stdin=PIPE, stdout=PIPE, start_new_session=True`（进程组隔离，SIGKILL 可杀整棵进程树）。协议解析直接实现（不依赖 acp-sdk 包），参考 open-jieli 的 Go 实现。

### ndjson 协议（stdin/stdout）

| 方向 | 消息类型 | 说明 |
|------|----------|------|
| claude → bot | `ready` | 进程就绪 |
| claude → bot | `session_created` | 新 session UUID |
| claude → bot | `content_delta` | 流式内容片段 |
| claude → bot | `tool_use` | 工具调用事件 |
| claude → bot | `permission_request` | 权限请求，阻塞直到用户确认 |
| claude → bot | `done` | 执行完成 + 最终结果 |
| bot → claude | `new_session` | 创建新会话 |
| bot → claude | `load_session` | 加载已有会话 |
| bot → claude | `prompt` | 发送用户消息 |
| bot → claude | `permission_response` | 返回权限选择 |
| bot → claude | `cancel` | 取消执行 |

### 生命周期
- 每个 Session 懒初始化一个 ACP 进程，**复用**于多次交互
- 后台 Janitor（每分钟检查）：空闲 2h 后自动停止进程
- 重置 (`/new`)：仅清除 `agent_session_id`，可选重启进程

### 权限流程
```
ACP 发送 permission_request
  → session.perm_future = asyncio.Future()
  → 发送权限卡片给用户（带编号选项）
  → worker await perm_future（最长等待 5min）
用户回复 "1"
  → TaskDispatcher 识别为权限回复
  → perm_future.set_result(choice)
  → worker 恢复执行，发送 permission_response 给 ACP
```

---

## 内存系统 (`memory/`)

### 文件布局
```
~/.nextme/memory/{md5(context_id)}/
├── user_context.json     # 用户偏好、交互风格
├── personal.json         # 姓名、时区、角色等
└── facts.json            # 已学习事实（含置信度）
```

### Facts Schema
```json
{
  "facts": [
    {
      "text": "用户主要使用 Python",
      "confidence": 0.95,
      "created_at": "...",
      "source": "conversation"
    }
  ]
}
```

### 写入策略
- **内存中立即更新**，文件写入通过 asyncio 去抖（默认 30s）
- 原子写入：temp file + rename，避免崩溃时文件损坏
- 每次 Session 启动时注入 top-15 facts 到 agent system prompt

---

## 上下文压缩 (`context/compression.py`)

```
~/.nextme/threads/{session_id}/
├── context.txt[.zlib|.lzma|.br]
└── context.meta.json   # {"algorithm": "zlib", "original_size": 1MB, ...}
```

| 算法 | 标准库 | 速度 | 压缩率 | 适用场景 |
|------|--------|------|--------|----------|
| zlib | ✅ | 快 | 中 | 默认，< 500KB |
| lzma | ✅ | 慢 | 高 | 大上下文（> 500KB） |
| brotli | 可选pip | 中 | 高 | 文本密集场景 |

选择策略：默认 zlib；上下文 > 500KB 且 lzma 可用则切换 lzma；brotli 安装后自动评估。

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
上下文：{context}

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
~/.nextme/nextme.json  →  {cwd}/nextme.json  →
~/.nextme/settings.json  →  .env  →  NEXTME_* 环境变量
```

### `nextme.json`
```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxxxx",
  "projects": [
    {"name": "my-api", "path": "/abs/path/to/project", "executor": "claude"}
  ]
}
```

### `~/.nextme/settings.json`
```json
{
  "claude_path": "claude",
  "acp_idle_timeout_seconds": 7200,
  "task_queue_capacity": 1024,
  "memory_debounce_seconds": 30,
  "context_max_bytes": 1000000,
  "context_compression": "zlib",
  "log_level": "INFO"
}
```

---

## 消息去重与并发控制

- **消息去重**：LRU 缓存（1000条，5min TTL），基于 Feishu `message_id`，防止 WebSocket 重连时重复处理
- **Session 串行**：每个 Session 有一个 asyncio worker，任务串行执行
- **跨用户并行**：不同 `context_id` 完全并行（asyncio 并发协程）
- **路径锁**：`PathLockRegistry`，同一物理路径同时只允许一个 Session 写入

---

## 元命令 (`/` 开头)

| 命令 | 功能 |
|------|------|
| `/new` | 重置 ACP Session（清除对话历史） |
| `/stop` | 取消当前执行中的任务 |
| `/help` | 显示帮助卡片 |
| `/skill <trigger>` | 手动触发 Skill |
| `/status` | 显示当前 Session 状态 |
| `/project <name>` | 切换活跃项目 |

---

## 文件存储布局

```
~/.nextme/
├── nextme.json          # 用户级配置
├── settings.json        # 行为设置
├── state.json           # Session 状态（active_project, session IDs）
├── memory/
│   └── {ctx_hash}/      # per-user 内存
├── threads/
│   └── {session_id}/    # per-session 上下文文件
├── skills/              # 用户自定义 Skills
└── logs/nextme.log      # 滚动日志
```

---

## 启动与优雅停机 (`main.py`)

```
nextme up [--directory DIR] [--executor claude]

启动顺序：
1. 加载配置
2. 初始化各组件（StateStore, MemoryManager, SkillRegistry, ...）
3. 启动后台任务（acp_janitor, memory_debounce_flusher）
4. 注册飞书事件处理器
5. FeishuClient.start()  ← 阻塞，等待信号

SIGTERM / SIGINT 优雅停机：
1. 停止飞书 WebSocket
2. 等待 in-flight 任务完成（或超时 30s 强制退出）
3. ACPRuntimeRegistry.stop_all()
4. MemoryManager.flush_all()
5. 取消后台 asyncio.Task
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
- `claude-code-acp`（npm: `@zed-industries/claude-code-acp` 或参考其 README）

---

## 开发阶段规划

### Phase 1 - MVP（本次交付）
- [x] 飞书 WebSocket 长连接 + 消息收发
- [x] Claude Code ACP 子进程管理
- [x] Session 隔离 + 串行任务队列
- [x] 流式进度推送（3s 去抖）
- [x] 权限确认卡片 + 异步 Future 阻塞

### Phase 2
- [ ] Skills 系统（Markdown 文件）
- [ ] 持久化内存（facts + 用户上下文）
- [ ] 上下文文件 + 多算法压缩

### Phase 3
- [ ] 多项目切换
- [ ] 热重载配置
- [ ] 路径锁（多用户冲突防护）

---

## 验证方式

1. **飞书连接**：`nextme up` 后向 Bot 发消息，Bot 出现"思考中..."反应
2. **ACP 执行**：发送 `"列出当前目录的文件"` → Bot 返回 `ls` 结果
3. **流式进度**：长任务（如代码重构）中每 3s 看到进度卡片更新
4. **权限确认**：Agent 执行写操作时，飞书弹出权限选择卡片，用户回复后继续
5. **Session 隔离**：两个不同用户同时发消息，互不干扰
6. **重启恢复**：重启 Bot 后，`/status` 显示之前的 `session_id` 已恢复
7. **上下文压缩**：超过 1MB 的上下文文件自动生成 `.zlib` 压缩版本
8. **Skills 调用**：`/review` 触发 Code Review Skill，Agent 返回结构化 Review 结果
