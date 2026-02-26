# NextMe

**飞书 IM × Claude Code Agent 机器人**

[English](README.md)

将飞书群聊和单聊变成交互式的 Claude Code 终端。在飞书发送消息，NextMe 将其路由到本地 `claude` 子进程，实时推送进度更新，并以互动卡片形式返回最终结果。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 飞书 WebSocket | 持久长连接，自动重连 |
| DirectClaudeRuntime | 启动 `claude --print --output-format stream-json`；通过 `--resume` 保持会话连续性 |
| ACPRuntime（可选）| JSON-RPC 2.0 协议，通过 `cc-acp` 子进程通信 |
| 流式进度卡片 | 执行过程中实时更新卡片，展示工具调用详情 |
| 权限确认流程 | 写操作时推送确认卡片，用户回复数字授权 |
| 多项目并发 | 每个 `(用户, 项目)` 组合独立 Worker，多项目并发互不阻塞 |
| 聊天绑定 | 将群聊绑定到指定项目（`/project bind <name>`） |
| 会话持久化 | Bot 重启后自动恢复 Claude 会话，对话历史无缝续接 |
| 长期记忆 | `/remember <text>` 保存用户级别 Facts（跨所有聊天共享），新会话自动注入 |
| 上下文压缩 | 超大上下文自动用 zlib / lzma / brotli 压缩 |
| Skills 系统 | Markdown 提示词模板；四级发现（内置 / 全局 / NextMe 全局 / 项目级）；`/review` `/commit` `/test` 等 |
| 元命令 | `/new` `/stop` `/help` `/status` `/project` `/task` `/remember` `/skill` |
| 路径锁 | 同一项目目录同一时刻只允许一个 Session 写入 |
| 优雅退出 | SIGTERM/SIGINT → 等待任务完成 → 刷新状态 → 退出 |

---

## 快速开始

### 前置条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理工具
- 飞书开发者账号（企业自建应用）
- 已安装并认证的 `claude` CLI

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

前往[飞书开放平台](https://open.feishu.cn/)，按以下步骤操作。

**第 1 步 — 创建应用并启用机器人**

1. 点击**创建企业自建应用**。
2. 进入应用管理页，在**功能**中启用**机器人**。

**第 2 步 — 获取凭证**

进入**凭证与基础信息**页面，复制 **App ID** 和 **App Secret** 填入 `nextme.json`：

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**第 3 步 — 配置权限**

进入**权限管理**页面，可使用以下 JSON 一次性导入所有所需权限：

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

权限说明：

| 权限 | 用途 |
|------|------|
| `im:message:send_as_bot` | 以机器人身份发送消息 |
| `im:message:send_multi_users` | 向多用户发送消息 |
| `im:message:update` | 更新/编辑消息 |
| `im:message:send_sys_msg` | 发送系统消息 |
| `im:message:recall` | 撤回消息 |
| `im:message:readonly` | 读取消息 |
| `im:message.group_at_msg:readonly` | 接收群聊 @ 消息 |
| `im:message.p2p_msg:readonly` | 接收单聊消息 |
| `im:message.reactions:read` | 读取消息回应 |
| `im:message.reactions:write_only` | 添加消息回应（Emoji 反应） |
| `im:message.pins:read` | 读取置顶消息 |
| `im:message.pins:write_only` | 置顶/取消置顶消息 |
| `im:chat:read` | 读取群组信息 |
| `im:chat:update` | 更新群组信息 |
| `im:resource` | 上传/下载消息资源 |
| `contact:contact.base:readonly` | 读取基础联系人信息 |
| `docx:document:readonly` | 读取飞书文档内容 |
| `contact:user.employee_id:readonly` | 读取用户工号（用户权限） |

**第 4 步 — 启动 NextMe**

```bash
nextme up
```

*(完整参数见下方[启动](#启动)章节。)*

**第 5 步 — 配置事件和回调**

> 请在 NextMe 启动后再配置此步骤，确保长连接已建立。

进入**事件订阅**：

- 将**订阅方式**设为**使用长连接接收事件**。
- 添加事件：`im.message.receive_v1`

进入**回调配置**（卡片回调）：

- 将**订阅方式**设为**使用长连接**。
- 添加回调：
  - `card.action.trigger` — 卡片交互按钮事件
  - `url.preview.get` — URL 预览

**第 6 步 — 发布应用**

进入**版本管理与发布**，申请发布并等待审核通过。审核通过后，用户即可在飞书搜索 Bot 名称进行对话或将其添加到群组。

### 启动

```bash
nextme up
```

可选参数：

```
nextme up --directory /path/to/project   # 覆盖项目目录
           --executor claude             # Agent 执行器（默认: claude）
           --log-level DEBUG             # 日志级别
```

停止：

```bash
nextme down
```

---

## 使用说明

### 对话任务

直接向 Bot 发送任意消息。Agent 在配置的项目目录中执行任务，并以互动卡片形式返回结果。

### 元命令

| 命令 | 说明 |
|------|------|
| `/new` | 开始新对话（清除历史） |
| `/stop` | 取消当前正在运行的任务 |
| `/help` | 显示帮助卡片 |
| `/status` | 显示所有会话状态 |
| `/task` | 显示各项目的活跃任务和队列深度 |
| `/project` | 列出所有已配置项目 |
| `/project <name>` | 切换活跃项目 |
| `/project bind <name>` | 将当前聊天永久绑定到指定项目 |
| `/project unbind` | 解除聊天与项目的绑定 |
| `/skill` | 列出所有已注册 Skill（按层级分组：项目级 / NextMe 全局 / 全局 / 内置） |
| `/skill <trigger>` | 按触发词调用 Skill |
| `/remember <text>` | 将信息保存到长期记忆 |

### 内置 Skills

| 触发词 | 说明 |
|--------|------|
| `/skill review` | 代码审查：正确性 / 性能 / 可读性 |
| `/skill commit` | 根据 `git diff` 生成 Conventional Commits 提交信息 |
| `/skill explain` | 解释代码工作原理 |
| `/skill test` | 生成单元测试 |
| `/skill debug` | 系统化调试流程 |

### 权限确认（仅 ACPRuntime）

Agent 需要执行写操作时，会向飞书推送确认卡片：

```
需要授权
Agent 即将执行：...

1. 允许
2. 拒绝
3. 始终允许
```

回复对应数字继续。

---

## 配置说明

### 优先级（低 → 高）

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
| `app_id` | string | 飞书 App ID |
| `app_secret` | string | 飞书 App Secret |
| `projects` | array | 项目列表（`name` / `path` / `executor`） |
| `bindings` | object | 静态聊天→项目绑定（`chat_id: project_name`） |

`executor` 可选值：
- `"claude"`（默认）— DirectClaudeRuntime，使用本地 `claude` CLI
- `"cc-acp"` — ACPRuntime，通过 `cc-acp` 子进程（JSON-RPC 2.0）

**多项目示例：**

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
| `acp_idle_timeout_seconds` | `7200` | ACPRuntime 进程空闲超时时间（秒） |
| `task_queue_capacity` | `1024` | 每个会话的任务队列容量 |
| `memory_debounce_seconds` | `30` | 状态/记忆刷盘防抖间隔（秒） |
| `context_max_bytes` | `1000000` | 触发压缩的上下文大小阈值（字节） |
| `context_compression` | `"zlib"` | 压缩算法：`zlib` / `lzma` / `brotli` |
| `progress_debounce_seconds` | `3.0` | 进度卡片更新防抖间隔（秒） |
| `permission_timeout_seconds` | `300.0` | 权限确认超时时间（秒） |
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

Skills 从四个层级发现（优先级高的覆盖低的）：

| 优先级 | 目录 | 标签 |
|--------|------|------|
| 4 — 最高 | `{project_path}/.nextme/skills/*.md` | 项目级 |
| 3 | `~/.nextme/skills/*.md` | NextMe 全局 |
| 2 | `~/.claude/skills/<name>/SKILL.md` | 全局（仅 claude executor） |
| 1 — 最低 | `{package}/skills/*.md` | 内置 |

**全局**层级（`~/.claude/skills/`）仅在至少一个项目使用 `executor: "claude"` 时才会扫描。通过 Claude Code 安装的 Skills 会自动出现在此层级。

**NextMe / 项目级 Skill 格式**（含 `{user_input}` 占位符）：

```markdown
---
name: My Skill
trigger: myskill
description: 这个 Skill 的功能描述
tools_allowlist: []
tools_denylist: []
---

你是一个...

用户请求：{user_input}
上下文：{context}
```

**Claude 全局 Skill 格式**（触发词 = 目录名，无 `{user_input}` 占位符）：

```markdown
---
name: My Global Skill
description: 这个 Skill 的功能描述
allowed-tools: [bash, read]
---

你是一个专精于...的专家
```

当全局 Skill 模板没有 `{user_input}` 占位符时，NextMe 会自动在末尾追加 `User request: <input>`。

通过 `/skill myskill` 调用。

---

## 文件存储

```
~/.nextme/
├── nextme.json          # 用户级配置
├── settings.json        # 行为设置
├── state.json           # 会话状态（actual_id, 活跃项目）
├── nextme.pid           # PID 文件（供 nextme down 使用）
├── memory/
│   └── {user_hash}/     # 用户级记忆（Facts / 偏好）
├── threads/
│   └── {session_id}/    # 会话上下文文件（可选压缩）
├── skills/              # 用户自定义 Skills
└── logs/nextme.log      # 滚动日志（10 MB × 5 份备份）
```

---

## 架构

```
飞书用户 ──WebSocket──▶ FeishuClient
                              │
                        MessageHandler（LRU 去重）
                              │
                        TaskDispatcher
                         ├─ 元命令处理
                         ├─ 权限回复路由
                         └─ 普通消息入队
                              │
                  ┌───────────▼───────────┐
                  │   SessionWorker       │  ← 每个 Session 一个协程
                  │   串行任务队列        │
                  └───────────┬───────────┘
                              │ PathLock（路径级互斥锁）
                              ▼
                   ACPRuntimeRegistry
                    ├─ executor="claude" → DirectClaudeRuntime
                    │   claude --print --output-format stream-json
                    │   [--resume session_id]
                    └─ executor="cc-acp" → ACPRuntime
                        JSON-RPC 2.0 over cc-acp subprocess
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 编程语言 | Python 3.12+ |
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

## 多项目并发执行

NextMe 为每个 `(用户, 项目)` 组合分配独立的 asyncio Worker，不同项目的任务并发执行，互不阻塞。

**路由优先级（高 → 低）：**

1. `nextme.json` 中的静态绑定（`bindings` 字段）
2. 通过 `/project bind <name>` 设置的动态绑定（持久化到 `state.json`）
3. 用户当前活跃项目（通过 `/project <name>` 切换）
4. `projects` 列表中的第一个项目（默认）

---

## 会话持久化与记忆

**会话持久化** — 每次任务完成后，Claude 会话 ID（`actual_id`）保存到 `~/.nextme/state.json`。Bot 重启时，NextMe 向 `claude` CLI 传入 `--resume <id>`，无缝续接对话历史。

**长期记忆** — 使用 `/remember <text>` 保存 Facts。Facts 存储在**用户级别**，跨该用户的所有聊天共享。在非续接的新会话中，置信度最高的 10 条 Facts 会自动注入到任务提示词开头：

```
[用户记忆]
- 我偏好 Python 而非 JavaScript
- 测试框架使用 pytest

[用户消息]
<你的消息>
```

---

## 路线图

- **Phase 1 ✅** — 飞书 WebSocket + Agent 子进程 + 会话隔离 + 流式进度 + 权限确认
- **Phase 2 ✅** — Skills 系统、多项目并发、会话持久化、长期记忆（`/remember`）、上下文压缩、路径锁
- **Phase 3** — 配置热重载、Slack / 钉钉适配器、多 Agent 编排

---

## 许可证

MIT
