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
| 长期记忆 | Facts 按用户存储（跨所有聊天共享），新会话自动以编号列表注入；Agent 可通过 `<memory>` 标签主动新增 / 替换 / 删除 Facts |
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

**1. 创建 `~/.nextme/settings.json`**

```bash
mkdir -p ~/.nextme
cp settings.json.example ~/.nextme/settings.json
```

编辑 `~/.nextme/settings.json`：

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

进入**凭证与基础信息**页面，复制 **App ID** 和 **App Secret** 填入 `~/.nextme/settings.json`：

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
| `cardkit:card:write` | 创建和更新交互式卡片（流式进度） |
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

**快捷脚本**（项目根目录）：

```bash
./start.sh           # 封装 nextme up，带检查和彩色输出
./stop.sh            # 封装 nextme down
```

### 作为系统服务运行

使用 `nohup &` 或在终端会话中启动 NextMe，一旦系统休眠或会话结束服务就会停止。建议将其注册为后台系统服务。

#### macOS — launchd（推荐）

```bash
./launchd-install.sh            # 安装并立即启动
./launchd-install.sh --uninstall
```

plist 文件写入 `~/Library/LaunchAgents/com.nextme.bot.plist`。
NextMe 在登录后自动启动，崩溃后自动重启（冷却 10 秒），锁屏期间保持网络连接。

```bash
tail -f ~/.nextme/logs/nextme.log          # 查看日志
launchctl list | grep nextme               # 检查状态
launchctl stop  com.nextme.bot             # 停止
launchctl start com.nextme.bot             # 启动
```

#### Linux — systemd 用户服务

```bash
./systemd-install.sh            # 安装并立即启动
./systemd-install.sh --uninstall
```

unit 文件写入 `~/.config/systemd/user/nextme.service`。
脚本会自动执行 `loginctl enable-linger`，使服务在注销后也能持续运行（若需要 sudo 权限会有提示）。

```bash
tail -f ~/.nextme/logs/nextme.log          # 查看日志
systemctl --user status nextme             # 检查状态
systemctl --user stop   nextme             # 停止
systemctl --user start  nextme             # 启动
```

#### Windows — 计划任务

在**管理员权限**的 PowerShell 中运行：

```powershell
.\windows-service-install.ps1            # 安装并立即启动
.\windows-service-install.ps1 -Uninstall
```

注册名为 **"NextMe Bot"** 的计划任务，登录后自动启动，崩溃后自动重启（冷却 30 秒）。
同时生成包装脚本 `nextme-wrapper.ps1`，将输出重定向到日志文件。

```powershell
Get-Content "$env:USERPROFILE\.nextme\logs\nextme.log" -Wait -Tail 50   # 查看日志
Get-ScheduledTask  -TaskName "NextMe Bot"      # 检查状态
Stop-ScheduledTask -TaskName "NextMe Bot"      # 停止
Start-ScheduledTask -TaskName "NextMe Bot"     # 启动
```

---

## 安全性

> **在共享或生产环境中运行 NextMe 前，请务必阅读本节。**

### Claude CLI 权限标志

NextMe 的 `DirectClaudeRuntime`（executor `"claude"`）通过以下标志启动本地 `claude` CLI：

```json
[
  "--print",
  "--output-format", "stream-json",
  "--verbose",
  "--dangerously-skip-permissions",
  "--include-partial-messages"
]
```

其中 **`--dangerously-skip-permissions`** 是关键标志。它告知 Claude Agent 自动批准每一个工具调用——包括 Bash 命令、文件写入、网络请求——**无需暂停请求确认**。这是机器人环境（无人在终端守候）的必要设计，但也意味着：

- Agent 可以在已配置的项目目录中运行**任意 Shell 命令**。
- Agent 可以在该目录（以及操作系统用户有权限访问的任意位置）**读写文件**。
- 除操作系统用户自身权限之外，**没有任何沙箱隔离**。

### Claude Code 权限设置（`~/.claude/settings.json`）

除上述标志外，Claude Code CLI 本身还会读取 `~/.claude/settings.json` 来应用**全局权限策略**。此处的规则由 Claude CLI 强制执行，无论 NextMe 发出何种请求，是重要的第二道防线。

推荐的基础配置（`~/.claude/settings.json`）：

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

| 设置 | 效果 |
|------|------|
| `defaultMode: "bypassPermissions"` | 所有未列出的工具均自动批准——无人值守的机器人环境必须如此，但这意味着需要显式配置 deny 规则 |
| `deny` 规则 | 即使指定了 `--dangerously-skip-permissions`，这些模式也会被**硬性拦截**——Agent 永远无法执行匹配的命令 |
| `ask` 规则 | 这些模式会触发确认提示；在机器人环境中，这实际上等同于拦截（无人在终端确认） |

> **安全警告：** 在没有 `deny` 规则的情况下使用 `defaultMode: "bypassPermissions"`，意味着 Agent 可以执行任意 Shell 命令。在共享环境中部署 NextMe 前，务必在 `~/.claude/settings.json` 中配置覆盖破坏性操作的 `deny` 规则。

### 推荐加固措施

| 措施 | 方法 |
|------|------|
| **限制可发送消息的用户** | 将您的 `open_id` 加入 `admin_users`，并启用 ACL 功能 |
| **以专用操作系统用户运行** | 为 NextMe 创建低权限用户；该用户只继承自身的文件权限 |
| **限制项目路径** | 在 `settings.json` 中将 `path` 设置为较小的目录，而非 `/` 或 `~` |
| **对敏感目录挂载只读卷** | 以只读方式挂载 NextMe 进程无需写入的敏感目录 |
| **定期审查 Agent 记忆** | 定期检查 `~/.nextme/memory/`，了解 Agent 已记录的内容 |

### 环境变量处理

NextMe 会从子进程 `claude` 的环境中剥离以下变量，以防止嵌套会话冲突和凭证泄露：

- `CLAUDECODE`、`CLAUDE_CODE_*` — 防止"嵌套会话"错误
- `ANTHROPIC_AUTH_TOKEN` **不会**以 `ANTHROPIC_API_KEY` 的形式传递；内部 `claude` 使用其自身的 `~/.claude.json` 凭证

子进程将完整继承您的环境变量，包括用于自定义代理端点的 `ANTHROPIC_BASE_URL`。

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
~/.nextme/settings.json
  → {cwd}/nextme.json
    → .env
      → NEXTME_* 环境变量
```

`~/.nextme/settings.json` 是唯一的用户级配置文件，同时包含应用凭证/项目列表**和**运行时行为设置。可选的 `{cwd}/nextme.json` 用于添加或覆盖项目本地配置。

### `~/.nextme/settings.json` 字段

**应用凭证与项目**

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_id` | string | 飞书 App ID |
| `app_secret` | string | 飞书 App Secret |
| `projects` | array | 项目列表（`name` / `path` / `executor`） |
| `bindings` | object | 静态聊天→项目绑定（`chat_id: project_name`） |
| `executor_args` | array | 追加到执行器命令的额外参数（例如 `["acp", "serve"]`） |

`executor` 可选值：
- `"claude"`（默认）— DirectClaudeRuntime，使用本地 `claude` CLI
- `"cc-acp"` — ACPRuntime，通过 `cc-acp` 子进程（JSON-RPC 2.0）
- `"coco"` — ACPRuntime，通过 `coco` 子进程（JSON-RPC 2.0 / ACP 协议）；子命令通过 `executor_args` 传入

**运行时行为**

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `acp_idle_timeout_seconds` | `7200` | ACPRuntime 进程空闲超时时间（秒） |
| `task_queue_capacity` | `1024` | 每个会话的任务队列容量 |
| `memory_debounce_seconds` | `30` | 状态/记忆刷盘防抖间隔（秒） |
| `memory_max_facts` | `100` | 每用户最多保留 Facts 数；超限后按置信度从低到高淘汰 |
| `context_max_bytes` | `1000000` | 触发压缩的上下文大小阈值（字节） |
| `context_compression` | `"zlib"` | 压缩算法：`zlib` / `lzma` / `brotli` |
| `progress_debounce_seconds` | `0.5` | 进度卡片更新防抖间隔（秒） |
| `permission_auto_approve` | `false` | 自动批准 ACPRuntime 权限请求，无需用户确认 |
| `log_level` | `"INFO"` | 日志级别 |

**多项目示例（`~/.nextme/settings.json`）：**

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

### 环境变量

```bash
NEXTME_APP_ID=cli_xxx
NEXTME_APP_SECRET=xxx
NEXTME_LOG_LEVEL=INFO
NEXTME_ACP_IDLE_TIMEOUT_SECONDS=7200
```

---

## 访问控制

NextMe 支持基于角色的访问控制（ACL），限制哪些用户可以与机器人交互。

### 角色

| 角色 | 配置方式 | 权限 |
|------|---------|------|
| **Admin（管理员）** | `settings.json` 中的 `admin_users` | 完全访问；审批 Owner 申请 |
| **Owner（所有者）** | SQLite（`~/.nextme/nextme.db`） | 执行 Bot 任务、切换项目、审批 Collaborator 申请 |
| **Collaborator（协作者）** | SQLite（`~/.nextme/nextme.db`） | 执行 Bot 任务、状态命令；不能切换项目 |

### 配置

将您的 `open_id` 添加到 `~/.nextme/settings.json` 中的 `admin_users`：

```json
{
  "admin_users": ["ou_your_open_id_here"],
  ...
}
```

使用 `/whoami` 查看您的 `open_id`。

### 命令

| 命令 | 说明 | 最低角色 |
|------|------|----------|
| `/whoami` | 显示您的 open_id 和角色 | 所有人 |
| `/acl list` | 列出所有已授权用户 | Collaborator |
| `/acl add <open_id> [owner\|collaborator]` | 添加用户 | Owner（仅限 collab）/ Admin |
| `/acl remove <open_id>` | 移除用户 | Owner（仅限 collab）/ Admin |
| `/acl pending` | 查看待审批申请 | Owner / Admin |
| `/acl approve <id>` | 批准申请 | Owner / Admin |
| `/acl reject <id>` | 拒绝申请 | Owner / Admin |

### 申请流程

未授权用户会收到一张包含其 `open_id` 和申请按钮的卡片，可申请 Owner 或 Collaborator 权限。申请通知将以私信方式发送给管理员（Owner 申请）或 Owner + 管理员（Collaborator 申请）。审批人可直接在通知卡片上批准或拒绝申请。

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
├── settings.json        # 用户级配置（凭证 + 项目 + 运行时设置）
├── state.json           # 会话状态（actual_id, 活跃项目, 动态绑定）
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

1. `~/.nextme/settings.json` 中的静态绑定（`bindings` 字段）
2. 通过 `/project bind <name>` 设置的动态绑定（持久化到 `state.json`）
3. 用户当前活跃项目（通过 `/project <name>` 切换）
4. `projects` 列表中的第一个项目（默认）

---

## 会话持久化与记忆

**会话持久化** — 每次任务完成后，Claude 会话 ID（`actual_id`）保存到 `~/.nextme/state.json`。Bot 重启时，NextMe 向 `claude` CLI 传入 `--resume <id>`，无缝续接对话历史。

**长期记忆** — Facts 存储在**用户级别**（`~/.nextme/memory/{user_hash}/facts.json`），跨该用户的所有聊天共享。在非续接的新会话中，置信度最高的 10 条 Facts 通过 Jinja2 模板注入到任务提示词开头：

```
[用户记忆] (共 2 条，可在回复末尾用 <memory> 标签更新)
0. 偏好 Python 而非 JavaScript
1. 测试框架使用 pytest

记忆操作（仅在有必要时使用）：
- 新增: <memory>内容</memory>
- 更新: <memory op="replace" idx="0">新内容</memory>
- 删除: <memory op="forget" idx="1"></memory>

[用户消息]
<你的消息>
```

Agent 可在回复末尾写 `<memory>` 标签主动管理记忆（标签在展示给用户前自动剥离）：

| 标签 | 操作 |
|------|------|
| `<memory>内容</memory>` | 新增一条 Fact |
| `<memory op="replace" idx="0">新内容</memory>` | 替换第 0 条 Fact |
| `<memory op="forget" idx="1"></memory>` | 删除第 1 条 Fact |

**去重** — 新增时若与已有 Fact 相似度 > 0.85（difflib `SequenceMatcher`），自动合并（置信度更高的文本胜出）。总数超过 `memory_max_facts`（默认 100）时淘汰置信度最低的 Facts。

**自定义模板** — 创建 `~/.nextme/prompts/memory.md`（Jinja2）可完全自定义注入格式。可用变量：`{{ count }}`、`{% for fact in facts %}{{ loop.index0 }}. {{ fact.text }}{% endfor %}`。

使用 `/remember <text>` 可直接从飞书添加 Fact，无需等待 Agent 写 `<memory>` 标签。

---

## 路线图

- **Phase 1 ✅** — 飞书 WebSocket + Agent 子进程 + 会话隔离 + 流式进度 + 权限确认
- **Phase 2 ✅** — Skills 系统、多项目并发、会话持久化、长期记忆（`/remember` + Agent 主动 add/replace/delete）、上下文压缩、路径锁
- **Phase 3** — 配置热重载、Slack / 钉钉适配器、多 Agent 编排

---

## 许可证

MIT
