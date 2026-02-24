# NextMe

**飞书 IM × Claude Code Agent Bot**

将飞书群聊 / 单聊变成 Claude Code 的交互终端。用户在飞书发消息，Bot 通过 [ACP 协议](https://github.com/zed-industries/claude-code-acp) 驱动本地 `claude-code-acp` 子进程执行任务，将流式进度和最终结果以交互卡片形式实时回推到飞书。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| 飞书 WebSocket 长连接 | 实时接收消息，自动重连 |
| Claude Code ACP 集成 | 子进程管理，ndjson 流式协议，自行实现（无 SDK 依赖） |
| 流式进度卡片 | 每 3s 更新一次执行进度，工具调用实时显示 |
| 权限确认流程 | Agent 执行写操作时推送确认卡片，用户回复数字继续 |
| Session 隔离 | 每个用户独立 Session，多用户完全并行 |
| 持久化内存 | 用户事实、偏好跨会话保留，注入 Agent system prompt |
| 上下文压缩 | 超大上下文自动 zlib/lzma/brotli 压缩存储 |
| Skills 系统 | Markdown 文件定义 Skill，`/review` `/commit` 等一键触发 |
| 元命令 | `/new` `/stop` `/help` `/status` `/project` |
| 路径锁 | 同一物理路径同时只允许一个 Session 写入 |
| 优雅停机 | SIGTERM/SIGINT → 等待任务完成 → 刷新内存 → 退出 |

---

## 快速开始

### 前置要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理器
- 飞书开发者账号（需创建企业自建应用）
- `claude-code-acp` 命令可用

```bash
# 安装 claude-code-acp（需 Node.js）
npm install -g @zed-industries/claude-code-acp
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
      "executor": "claude-code-acp"
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
           --executor claude-code-acp    # ACP 执行器命令
           --log-level DEBUG             # 日志级别
```

---

## 使用方式

### 普通对话

直接向 Bot 发送消息，Agent 会在项目目录下执行任务，结果以卡片形式返回。

### 元命令

| 命令 | 说明 |
|------|------|
| `/new` | 重置对话历史（开启新 ACP Session） |
| `/stop` | 取消当前正在执行的任务 |
| `/help` | 显示帮助卡片 |
| `/status` | 查看当前 Session 状态 |
| `/project <name>` | 切换活跃项目 |
| `/skill <trigger>` | 手动触发指定 Skill |

### 内置 Skills

| 触发词 | 功能 |
|--------|------|
| `/review` | 代码 Review（正确性 / 性能 / 可读性） |
| `/commit` | 生成 Conventional Commits 规范的提交信息 |
| `/explain` | 解释代码工作原理 |
| `/test` | 生成单元测试 |
| `/debug` | 系统化调试流程 |

### 权限确认

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

### `~/.nextme/settings.json` 字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `acp_idle_timeout_seconds` | `7200` | ACP 进程空闲超时（秒） |
| `task_queue_capacity` | `1024` | 每个 Session 的任务队列容量 |
| `memory_debounce_seconds` | `30` | 内存写入防抖间隔（秒） |
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
NEXTME_CLAUDE_PATH=claude-code-acp
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

你是一位...

用户请求：{user_input}
上下文：{context}

请完成以下任务...
```

触发方式：`/skill myskill` 或直接 `/myskill`。

---

## 文件存储

```
~/.nextme/
├── nextme.json          # 用户级配置
├── settings.json        # 行为设置
├── state.json           # Session 状态持久化
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
                          ACPRuntime
                           ├─ 启动 claude-code-acp 子进程
                           ├─ ndjson 流式协议
                           ├─ 进度回调（3s 防抖卡片更新）
                           └─ 权限请求（asyncio.Future 阻塞）
```

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| 包管理 | uv + pyproject.toml |
| IM 集成 | lark-oapi，WebSocket 长连接 |
| Agent 通信 | ACP 协议，ndjson over stdin/stdout，自行实现 |
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

## 路线图

- **Phase 1 ✅** — 飞书 WebSocket + ACP 子进程 + Session 隔离 + 流式进度 + 权限确认
- **Phase 2** — Skills 系统完整集成、持久化内存（facts 注入）、上下文压缩
- **Phase 3** — 多项目切换、热重载配置、路径锁多用户冲突防护

---

## License

MIT
