# NextMe 多项目并行隔离设计

> **文档状态：** 已实现（`feat/multi-project-parallel` → `main`）
> **关联文档：** `persistence.zh.md`、`memory-and-persistence-design.zh.md`

---

## 1. 背景与问题

### 1.1 原始设计的限制

旧架构中 `_worker_tasks` 和 `acp_registry` 均以 `context_id`（`chat_id:user_id`）为键：

```
_worker_tasks["chat_abc:user_xyz"] = worker_A
acp_registry["chat_abc:user_xyz"] = runtime_A
```

导致：
- **同一用户只能有一个 Worker**：切换项目会停止旧 Worker 再建新 Worker
- **只有一个活跃 session**：所有项目共用一个 ACP/Claude 子进程
- **串行执行**：多项目任务无法并行

### 1.2 目标

1. 同一用户可以同时向多个 repo 发任务，Worker 并行运行
2. 群聊可以静态或动态绑定到特定项目
3. 多项目间的进度、状态完全隔离

---

## 2. 核心设计：以 `context_id:project_name` 为键

### 2.1 键变更

```
旧：_worker_tasks[context_id]
新：_worker_tasks[f"{context_id}:{project_name}"]

旧：acp_registry.get_or_create(session_id=context_id)
新：acp_registry.get_or_create(session_id=f"{context_id}:{project_name}")
```

每个 `(用户, 项目)` 组合拥有独立的：
- **asyncio Task**（Worker）
- **Claude 子进程**（DirectClaudeRuntime / ACPRuntime）
- **任务队列**（`asyncio.Queue`）
- **会话文件**（`threads/{context_id}:{project_name}/`）

### 2.2 Session 数据结构

```
UserContext（per user）
  context_id: "chat_abc:user_xyz"
  active_project: "repo-a"           ← 当前活跃项目名
  sessions: {
    "repo-a": Session(
        project_name="repo-a",
        project_path="/path/to/repo-a",
        executor="claude",
        actual_id="xxx",              ← Claude session UUID
        task_queue=asyncio.Queue(),
        active_task=None
    ),
    "repo-b": Session(
        project_name="repo-b",
        ...
    )
  }
```

### 2.3 并行示意

```
用户发消息：
  "任务 A → repo-a"   →  worker[chat:user:repo-a]  → claude --resume xxx (repo-a 目录)
  "任务 B → repo-b"   →  worker[chat:user:repo-b]  → claude --resume yyy (repo-b 目录)
                              ↓ 并行运行，互不阻塞
  "任务 A 完成" → 发送结果卡片（独立 progress card）
  "任务 B 完成" → 发送结果卡片（独立 progress card）
```

---

## 3. 消息路由：项目绑定

### 3.1 路由优先级

```
1. 静态绑定（nextme.json bindings）         highest
2. 动态绑定（state.json，/project bind 设置）
3. 用户当前 active_project
4. 配置文件第一个 project（default）         lowest
```

### 3.2 静态绑定（nextme.json）

```json
{
  "projects": [...],
  "bindings": {
    "oc_groupchat_123": "repo-b",   // 群聊永远路由到 repo-b
    "oc_groupchat_456": "repo-a"
  }
}
```

适用场景：团队群聊固定对应某个仓库，无需每次切换。

### 3.3 动态绑定（/project bind）

```
/project bind repo-b
  → dispatcher._dynamic_bindings["chat_abc"] = "repo-b"
  → state_store.set_binding("chat_abc", "repo-b")
  → 持久化到 state.json
  → 后续消息自动路由到 repo-b

/project unbind
  → 移除绑定，恢复 active_project 路由
```

动态绑定通过 `chat_id`（无用户 ID 部分）匹配，作用于整个群聊。

### 3.4 配置合并规则

全局配置（`~/.nextme/nextme.json`）与本地配置（`{cwd}/nextme.json`）合并时：

| 字段 | 合并策略 |
|------|----------|
| `projects` | 按 `name` 去重合并，本地条目覆盖同名全局条目 |
| `bindings` | dict 合并（`{**global, **local}`），本地键覆盖全局键 |
| 其他字段 | 本地直接覆盖全局 |

```python
# projects 合并示例
global:  [{"name": "main", "path": "~/.nextme/main"}]
local:   [{"name": "repo-a", "path": "./"}]
merged:  [{"name": "main", ...}, {"name": "repo-a", ...}]  # 联合

# bindings 合并示例
global:  {"chat_123": "main"}
local:   {"chat_456": "repo-a"}
merged:  {"chat_123": "main", "chat_456": "repo-a"}  # 联合
```

---

## 4. 命令集

### 4.1 项目管理命令

| 命令 | 功能 |
|------|------|
| `/project` | 列出所有项目（★ 活跃，⚓ 绑定）|
| `/project <name>` | 切换活跃项目（仅当前用户）|
| `/project bind <name>` | 将当前群聊绑定到项目（持久化）|
| `/project unbind` | 解除群聊绑定 |

### 4.2 状态查看命令

| 命令 | 功能 |
|------|------|
| `/status` | 卡片展示所有 session 状态（★ 活跃，路径，执行器，当前任务）|
| `/task` | 展示每个项目的任务队列和执行中任务内容 |

### 4.3 /project 输出示例

```
项目列表：

• main  ★ 活跃   `/Users/me/.nextme/main`
• repo-a          `/Users/me/projects/repo-a`
• repo-b  ⚓ 绑定  `/Users/me/projects/repo-b`

用法：/project <name> 切换 | /project bind <name> 绑定 | /project unbind 解绑
```

---

## 5. 路径锁（PathLockRegistry）

防止多用户同时向同一物理目录写入：

```
Worker 执行前：
  await path_lock_registry.acquire(project_path)
    ↓
  如果另一个 Worker 已持有同路径的锁 → session.status = "waiting_lock"
  等待锁释放后继续
    ↓
  执行完成后：path_lock_registry.release(project_path)
```

同一 repo 的多用户请求串行执行；不同 repo 的请求并行执行。

---

## 6. 状态持久化（跨重启）

配合 `memory-and-persistence-design.zh.md` 的 Phase 1：

```
state.json
└── GlobalState
    ├── bindings: {"chat_123": "repo-b"}     ← 动态绑定
    └── contexts:
        └── "chat_abc:user_xyz":
            ├── last_active_project: "repo-a"
            └── projects:
                ├── "repo-a": {actual_id: "xxx", executor: "claude"}
                └── "repo-b": {actual_id: "yyy", executor: "claude"}
```

重启后：
1. `_dynamic_bindings` 从 `state.json` 恢复
2. `session.actual_id` 从 `state.json` 恢复 → DirectClaudeRuntime 使用 `--resume`
3. 用户无感知，对话历史完整

---

## 7. 测试验证

集成测试位于 `tests/test_integration_multi_project.py`，覆盖：

| 测试 | 验证内容 |
|------|----------|
| `test_two_projects_get_independent_workers` | 两个项目创建独立 Worker |
| `test_project_b_not_blocked_by_project_a` | 慢任务不阻塞另一项目（timing 验证）|
| `test_static_binding_routes_to_correct_project` | 静态绑定路由 |
| `test_dynamic_binding_via_project_bind_command` | 动态绑定 + 持久化 |
| `test_unbind_reverts_to_active_project` | 解绑恢复默认路由 |
| `test_binding_survives_restart` | 绑定跨重启存活 |
| `test_worker_keys_are_project_scoped` | Worker key 格式验证 |

---

## 8. 已知限制与未来方向

| 限制 | 说明 | 未来方向 |
|------|------|----------|
| 同一 repo 串行执行 | PathLock 导致同路径请求排队 | 只读任务允许并发（无锁） |
| 无 worktree 支持 | 多 feature 并行需要手动创建 worktree | 自动 `git worktree add` |
| 切换只改 active_project | 旧 Worker 不停止，持续消费队列 | 支持 `/stop project-a` 停止特定 Worker |
| 绑定作用于 chat_id | 同群聊所有用户共享绑定 | 支持 per-user binding（`user_id` 维度）|
