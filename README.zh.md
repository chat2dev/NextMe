# NextMe

**飞书 IM × Claude Code Agent Bot**

将飞书群聊 / 单聊变成 Claude Code 的交互终端。用户在飞书发消息，NextMe 将消息路由至本地 `claude` 子进程执行任务，流式推送进度，并将最终结果以交互卡片形式返回。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| 飞书 WebSocket 长连接 | 实时接收消息，自动重连 |
| DirectClaudeRuntime | 调用 `claude --print --output-format stream-json`，通过 `--resume` 续接对话 |
| ACPRuntime（可选） | JSON-RPC 2.0 over `cc-acp` 子进程 |
| 流式进度卡片 | 每 3s 更新一次执行进度，工具调用实时显示 |
| 权限确认流程 | Agent 执行写操作时推送确认卡片，用户回复数字继续 |
| 多项目并行执行 | 每个 `(用户, 项目)` 组合独立 Worker，多项目任务并行不阻塞 |
| 群聊绑定 | `/project bind <name>` 将群聊永久绑定到指定项目 |
| Session 持久化 | Claude Session ID 跨重启保留，自动 `--resume` 续接对话历史 |
| 长期记忆 | `/remember <text>` 保存用户事实；新 Session 自动注入 |
| 上下文压缩 | 超大上下文自动 zlib/lzma/brotli 压缩存储 |
| Skills 系统 | Markdown 文件定义 Skill，`/review` `/commit` 等一键触发 |
| 元命令 | `/new` `/stop` `/help` `/status` `/project` `/task` `/remember` |
| 路径锁 | 同一物理路径同时只允许一个 Session 写入 |
| 优雅停机 | SIGTERM/SIGINT → 等待任务完成 → 刷新状态 → 退出 |

---

## 快速开始

### 前置要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理器
- 飞书开发者账号（需创建企业自建应用）
- `claude` CLI 已安装并完成认证

```bash
# 安装 Claude Code CLI
npm install -g @anthropic-ai/claude-code
```

### 安装

```bash
git clone https://github.com/chat2dev/NextMe.git
cd NextMe
uv sync
```

### 配置

**1. 创建 `nextme.json`**

```bash
cp nextme.json.example nextme.json
```

编辑 `nextme.json`：

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

**2. 飞书应用配置**

在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用，并开启以下权限：

- `im:message` — 读取/发送消息
- `im:message.group_at_msg` — 接收群组 @ 消息
- `im:message.p2p_msg` — 接收单聊消息

订阅事件：`im.message.receive_v1`

### 启动

```bash
nextme up
```

可选参数：

```
nextme up --directory /path/to/project   # 指定项目目录
           --executor claude             # 执行器（默认：claude）
           --log-level DEBUG             # 日志级别
```

停止：

```bash
nextme down
```

---

## 使用方式

### 普通对话

直接向 Bot 发送消息，Agent 会在配置的项目目录下执行任务，结果以交互卡片形式返回。

### 元命令

| 命令 | 说明 |
|------|------|
| `/new` | 开启新对话（清除当前对话历史） |
| `/stop` | 取消当前正在执行的任务 |
| `/help` | 显示帮助卡片 |
| `/status` | 查看所有 Session 状态 |
| `/task` | 查看各项目当前任务和队列深度 |
| `/project` | 列出所有配置的项目 |
| `/project <name>` | 切换活跃项目 |
| `/project bind <name>` | 将当前群聊永久绑定到指定项目 |
| `/project unbind` | 解除群聊绑定 |
| `/skill` | 列出所有已注册 Skills |
| `/skill <trigger>` | 手动触发指定 Skill |
| `/remember <text>` | 保存一条长期记忆 |

### 内置 Skills

| 触发词 | 功能 |
|--------|------|
| `/review` | 代码 Review（正确性 / 性能 / 可读性） |
| `/commit` | 生成 Conventional Commits 规范的提交信息 |
| `/explain` | 解释代码工作原理 |
| `/test` | 生成单元测试 |
| `/debug` | 系统化调试流程 |

### 权限确认（仅 ACPRuntime）

当 Agent 需要执行写操作时，飞书会弹出权限确认卡片：

```
需要授权
Agent 即将执行以下操作：...

1. 允许
2. 拒绝
3. 总是允许
```

回复对应数字即可继续。

---

## 配置说明

### 配置优先级（低 → 高）

```
~/.nextme/nextme.json
  → {cwd}/nextme.json
    → ~/.nextme/settings.json
      → .env
        → NEXTME_* 环境变量
```

### `nextme.json` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_id` | string | 飞书应用 App ID |
| `app_secret` | string | 飞书应用 App Secret |
| `projects` | array | 项目列表（name / path / executor） |
| `bindings` | object | 静态群聊→项目绑定（`chat_id: project_name`） |

`executor` 可选值：
- `"claude"`（默认）— DirectClaudeRuntime，使用本地 `claude` CLI
- `"cc-acp"` — ACPRuntime，使用 `cc-acp` 子进程（JSON-RPC 2.0）

**多项目配置示例：**

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

### `~/.nextme/settings.json` 字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `acp_idle_timeout_seconds` | `7200` | ACPRuntime 进程空闲超时（秒） |
| `task_queue_capacity` | `1024` | 每个 Session 的任务队列容量 |
| `memory_debounce_seconds` | `30` | 状态/内存写入防抖间隔（秒） |
| `context_max_bytes` | `1000000` | 触发压缩的上下文大小阈值 |
| `context_compression` | `"zlib"` | 压缩算法：`zlib` / `lzma` / `brotli` |
| `progress_debounce_seconds` | `3.0` | 进度卡片更新防抖间隔（秒） |
| `permission_timeout_seconds` | `300.0` | 权限确认超时（秒） |
| `log_level` | `"INFO"` | 日志级别 |

### 环境变量

```bash
NEXTME_APP_ID=cli_xxx
NEXTME_APP_SECRET=xxx
NEXTME_LOG_LEVEL=INFO
NEXTME_ACP_IDLE_TIMEOUT_SECONDS=7200
```

---

## 自定义 Skills

在以下任一目录创建 `.md` 文件即可自动加载（优先级从高到低）：

1. `{project_path}/.nextme/skills/*.md` — 项目级
2. `~/.nextme/skills/*.md` — 用户全局
3. `{package}/skills/*.md` — 内置

文件格式：

```markdown
---
name: My Skill
trigger: myskill
description: 功能说明
tools_allowlist: []
tools_denylist: []
---

You are a ...

User request: {user_input}
Context: {context}

Please complete the following task ...
```

触发方式：`/skill myskill` 或直接 `/myskill`。

---

## 文件存储

```
~/.nextme/
├── nextme.json          # 用户级配置
├── settings.json        # 行为设置
├── state.json           # Session 状态持久化（actual_id、活跃项目）
├── nextme.pid           # PID 文件（nextme down 定向 SIGTERM）
├── memory/
│   └── {ctx_hash}/      # 每用户内存（facts / 偏好 / 个人信息）
├── threads/
│   └── {session_id}/    # 每 Session 上下文文件（可压缩）
├── skills/              # 用户自定义 Skills
└── logs/nextme.log      # 滚动日志（10MB × 5）
```

---

## 架构简述

```
Feishu User ──WebSocket──▶ FeishuClient
                                │
                          MessageHandler（LRU 去重）
                                │
                          TaskDispatcher
                           ├─ 元命令处理
                           ├─ 权限回复路由
                           └─ 普通消息入队
                                │
                    ┌───────────▼───────────┐
                    │   SessionWorker       │  ← 每 Session 一个协程
                    │   串行消费任务队列     │
                    └───────────┬───────────┘
                                │ PathLock（路径级互斥）
                                ▼
                     ACPRuntimeRegistry
                      ├─ executor="claude" → DirectClaudeRuntime
                      │   claude --print --output-format stream-json
                      │   [--resume session_id]
                      └─ executor="cc-acp" → ACPRuntime
                          JSON-RPC 2.0 over cc-acp 子进程
```

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| 包管理 | uv + pyproject.toml |
| IM 集成 | lark-oapi，WebSocket 长连接 |
| Agent 运行时 | DirectClaudeRuntime（默认）/ ACPRuntime（可选） |
| 并发模型 | asyncio（Queue + Lock + Task + Future） |
| 配置校验 | pydantic v2 + python-dotenv |
| 上下文压缩 | zlib / lzma（标准库）/ brotli（可选） |

---

## 开发

```bash
# 安装开发依赖
uv sync --extra dev

# 代码检查
uv run ruff check src/

# 运行测试
uv run pytest
```

---

## 多项目并行执行

NextMe 为每个 `(用户, 项目)` 组合分配独立的 asyncio Worker，多个项目任务可以同时运行，互不阻塞。

**消息路由优先级（高→低）：**

1. `nextme.json` 中的静态绑定（`bindings` 字段）
2. `/project bind <name>` 设置的动态绑定（持久化于 `state.json`）
3. 用户当前活跃项目（`/project <name>` 切换）
4. 配置文件第一个项目（默认）

---

## Session 持久化与长期记忆

**Session 持久化** — 每次任务执行后，Claude Session ID（`actual_id`）自动保存到 `~/.nextme/state.json`。Bot 重启后，NextMe 自动传入 `--resume <id>` 恢复对话历史，用户无感知。

**长期记忆** — 使用 `/remember <text>` 保存事实。仅对新 Session（非恢复的），最多注入 10 条置信度最高的事实：

```
[用户记忆]
- 我偏好 Python 而非 JavaScript
- 使用 pytest 编写测试

[用户消息]
<你的消息>
```

---

## 路线图

- **Phase 1 ✅** — 飞书 WebSocket + Agent 子进程 + Session 隔离 + 流式进度 + 权限确认
- **Phase 2 ✅** — Skills 系统、多项目并行、Session 持久化跨重启恢复、长期记忆（`/remember`）、上下文压缩、路径锁
- **Phase 3** — 配置热重载、Slack / 钉钉适配、多 Agent 编排

---

## License

MIT
