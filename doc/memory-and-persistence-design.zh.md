# NextMe 记忆与持久化集成设计

> **文档状态：** 设计草稿
> **关联文档：** `persistence.zh.md`（存储布局与底层组件详解）
> **覆盖范围：** Session 持久化、对话上下文文件、长期记忆——三者的集成架构与实现计划

---

## 1. 现状分析（Gap Analysis）

`persistence.zh.md` 描述了三个已实现的存储组件，但它们目前均处于**孤立状态**，未与核心执行链路（Worker）连接。

| 组件 | 实现状态 | 集成状态 |
|------|----------|----------|
| `StateStore` — `state.json` | ✅ 完整实现 | ⚠️ 部分：绑定已接入，**actual_id 未持久化** |
| `ContextManager` — `threads/` | ✅ 完整实现 | ❌ **未接入 Worker，文件从不写入** |
| `MemoryManager` — `memory/` | ✅ 完整实现 | ❌ **无提取管道，无注入点** |

### 1.1 Session 持久化的具体缺口

```
Worker._execute_task() 完成
  → runtime.actual_id 更新（在内存中）
  → session.actual_id 同步（在内存中）
  → ❌ 未调用 state_store.save_actual_id()
  → Bot 重启后 actual_id 丢失，所有会话从头开始
```

`GlobalState.contexts[context_id].projects[project_name].actual_id` 字段虽然存在于 schema，但 StateStore 没有写入它的方法，Worker 也不持有 `state_store` 引用。

### 1.2 DirectClaudeRuntime 的会话继续机制

```
第一次执行：                     后续执行：
claude --print ...              claude --resume <actual_id> --print ...
  → 生成新 session               → 继续已有 session（对话历史完整）
  → 输出 {"session_id": "xxx"}   → 输出 {"session_id": "xxx"}
  → _actual_id = "xxx"
```

只要 `actual_id` 被持久化并在重启后恢复，**Claude 自身的会话历史就能跨重启存活**（受限于 Claude 的服务端 session TTL）。

---

## 2. Session 持久化设计

### 2.1 数据流

```
任务执行完成
  Worker._execute_task() 正常返回
    │
    ├─→ session.actual_id = runtime.actual_id
    └─→ state_store.save_project_actual_id(
            context_id, project_name, runtime.actual_id
        )  ← 新增调用

Bot 重启
  Session 首次被创建（UserContext.get_or_create_session）
    │
    └─→ 从 state_store 读取 ProjectState
        └─→ session.actual_id = project_state.actual_id  ← 恢复

DirectClaudeRuntime.execute()
  └─→ if self._actual_id: args += ["--resume", self._actual_id]
  ✅ 已实现，只需保证 session.actual_id 正确传入 runtime 即可
```

### 2.2 StateStore 新增方法

```python
def save_project_actual_id(
    self, context_id: str, project_name: str, actual_id: str
) -> None:
    """持久化 actual_id 到 state.json（去抖写入）。"""
    user_state = self.get_user_state(context_id)
    if project_name not in user_state.projects:
        user_state.projects[project_name] = ProjectState()
    user_state.projects[project_name].actual_id = actual_id
    self._dirty = True

def get_project_actual_id(
    self, context_id: str, project_name: str
) -> str:
    """读取持久化的 actual_id，返回空字符串表示无记录。"""
    state = self._require_loaded()
    user_state = state.contexts.get(context_id)
    if not user_state:
        return ""
    project_state = user_state.projects.get(project_name)
    return project_state.actual_id if project_state else ""
```

### 2.3 Session 恢复时机

```python
# UserContext.get_or_create_session()
def get_or_create_session(self, project, settings, state_store=None):
    if project.name not in self.sessions:
        session = Session(...)
        # 从 state_store 恢复 actual_id
        if state_store:
            session.actual_id = state_store.get_project_actual_id(
                self.context_id, project.name
            )
        self.sessions[project.name] = session
    return self.sessions[project.name]
```

### 2.4 /new 命令清除持久化

```python
# handle_new()
session.actual_id = ""
if state_store:
    state_store.save_project_actual_id(context_id, project_name, "")
```

---

## 3. 对话上下文文件（ContextManager 集成）

### 3.1 职责定位

| 场景 | DirectClaudeRuntime | ContextManager |
|------|---------------------|----------------|
| 正常继续对话 | `--resume actual_id` ✅ 已解决 | 不需要 |
| Bot 重启后继续 | `--resume` + 持久化 actual_id | 不需要 |
| Claude session 过期（TTL） | 丢失 | **注入 context 摘要** ← 核心价值 |
| 人工审计 / 历史查看 | 无 | **对话日志** ← 辅助价值 |

**结论：** ContextManager 的主要价值是在 Claude session 过期后为新会话提供"冷启动上下文"。它不替代 `--resume`，而是作为降级保底。

### 3.2 Context 写入策略

```
每次任务完成后，追加一条摘要行：
  [2026-02-26 20:00:00] Q: {task.content[:100]}
  [2026-02-26 20:00:05] A: {result_summary[:200]}
```

- **session_id** = `context_id:project_name`（与 worker key 一致）
- **写入时机**：`_execute_task()` 成功返回后，异步追加（不阻塞）
- **压缩触发**：内容超过 `settings.context_max_bytes`（默认 1MB）

### 3.3 Context 注入时机

```
Worker 准备 execute 时：
  if session.actual_id == "":          # 全新会话（重启后 session 过期或 /new）
      context = await context_manager.load(session_id)
      if context:
          task.content = f"[历史上下文摘要]\n{context[-2000:]}\n\n{task.content}"
                                        # 截取最近 2000 字符
```

**为什么截取末尾 2000 字符：** 完整历史可能很大，只注入最近对话保持 prompt 精简。

### 3.4 Context 格式

```
~/.nextme/threads/chat_abc:user_xyz:repo-a/
└── context.txt  (or context.zlib when > 1MB)

内容示例：
[2026-02-26 20:00:00] Q: 帮我分析 src/core/dispatcher.py 的架构
[2026-02-26 20:00:08] A: TaskDispatcher 负责路由消息到 SessionWorker...
[2026-02-26 20:05:00] Q: 给 dispatch 方法加单元测试
[2026-02-26 20:05:45] A: 已在 tests/test_core_dispatcher.py 添加 12 个测试...
```

---

## 4. 长期记忆（MemoryManager 集成）

### 4.1 设计目标

记忆解决的问题：**每次对话都从零开始，Claude 不知道用户偏好、项目背景、历史决策**。

两类记忆：

| 类型 | 内容举例 | 提取方式 |
|------|----------|----------|
| **用户偏好**（UserContextMemory） | 偏好中文、喜欢简洁代码、时区 Asia/Shanghai | 显式命令 / 自动提取 |
| **事实**（FactStore） | "repo-a 使用 uv 管理依赖"、"测试覆盖率要求 85%" | 显式命令 / 自动提取 |

### 4.2 记忆注入（读路径）

```
Worker 开始 execute：
  memory_facts = memory_manager.get_top_facts(context_id, n=10)
  if memory_facts and session.actual_id == "":
      # 仅在新会话时注入（已有 session 的 Claude 自身有历史）
      facts_text = "\n".join(f"- {f.text}" for f in memory_facts)
      task.content = f"[关于用户和项目的背景知识]\n{facts_text}\n\n{task.content}"
```

**注入条件：** 仅在 `actual_id == ""`（新会话）时注入，避免重复注入到已有历史的 session 中。

### 4.3 记忆写入（写路径）

#### Phase 1：显式命令（立即实现）

```
/remember <text>
  → MemoryManager.add_fact(context_id, Fact(text=text, source="user"))
  → 去抖写入 facts.json
```

用法示例：
- `/remember 这个项目用 uv 管理依赖，不要用 pip`
- `/remember 测试覆盖率要求新代码 90%，总体 85%`
- `/remember 用户偏好中文回复`

#### Phase 2：自动提取（未来实现）

```
每 N 次任务完成后，异步触发摘要任务：
  summary_prompt = f"""
  分析以下对话，提取 3-5 条用户偏好或项目事实，JSON 格式输出：
  {recent_context}
  """
  → 单独调用轻量模型（如 Claude Haiku）
  → 解析 JSON → MemoryManager.add_fact()
```

Phase 2 触发条件（可配置）：
- 每 10 次任务后
- 当 context 超过某阈值时
- Bot 正常关闭时（低优先级后台任务）

### 4.4 记忆查看与管理

新增命令：

| 命令 | 功能 |
|------|------|
| `/remember <text>` | 添加一条事实 |
| `/memory` | 列出已记住的事实（按 confidence 排序） |
| `/forget <n>` | 删除第 n 条事实 |

---

## 5. Worker 接入方案

Worker 是三个组件的消费者，需要新增三个依赖参数：

```python
class SessionWorker:
    def __init__(
        self,
        session: Session,
        acp_registry: ACPRuntimeRegistry,
        replier: Replier,
        settings: Settings,
        path_lock_registry: PathLockRegistry,
        # 新增（可选，None 时降级跳过对应逻辑）
        state_store: Optional[StateStore] = None,
        context_manager: Optional[ContextManager] = None,
        memory_manager: Optional[MemoryManager] = None,
    ):
```

`_execute_task()` 新增三个钩子：

```python
async def _execute_task(self, task: Task) -> None:
    # ── Before execute ──────────────────────────────────────────────────
    is_new_session = not self._session.actual_id

    # [1] 注入记忆（仅新会话）
    if is_new_session and self._memory_manager:
        facts = self._memory_manager.get_top_facts(context_id, n=10)
        if facts:
            task = _inject_facts(task, facts)

    # [2] 注入 context 摘要（仅新会话且有历史文件）
    if is_new_session and self._context_manager:
        summary = await self._context_manager.load(session_id)
        if summary:
            task = _inject_context(task, summary)

    # ── Execute ─────────────────────────────────────────────────────────
    ... (现有逻辑)

    # ── After execute ───────────────────────────────────────────────────
    # [3] 持久化 actual_id
    if self._state_store and runtime.actual_id:
        self._state_store.save_project_actual_id(
            context_id, project_name, runtime.actual_id
        )

    # [4] 追加 context 日志
    if self._context_manager:
        await self._context_manager.append(
            session_id,
            f"[{now}] Q: {task.content[:100]}\n"
            f"[{now}] A: {result_summary[:200]}"
        )
```

---

## 6. 实现优先级

### Phase 1：Session 持久化（高优先级，独立可交付）

**目标：** Bot 重启后 Claude session 自动恢复，用户无感知

- [ ] `StateStore.save_project_actual_id()` + `get_project_actual_id()`
- [ ] Worker 接入 `state_store`：任务完成后写入 actual_id
- [ ] `UserContext.get_or_create_session()` 从 state_store 恢复 actual_id
- [ ] `/new` 命令清除持久化的 actual_id

**验证方式：** 发几条消息后重启 Bot，再发消息确认对话历史未丢失。

### Phase 2：显式记忆命令（中优先级）

**目标：** 用户可以手动教 Bot 记住项目和个人偏好

- [ ] `/remember <text>` 命令
- [ ] `/memory` 查看命令
- [ ] Worker 接入 `memory_manager`：新会话时注入记忆

**验证方式：** `/remember 这个项目用 uv`，`/new`，再发消息确认 Claude 知道用 uv。

### Phase 3：Context 文件集成（低优先级）

**目标：** Claude session 过期后提供降级的上下文延续

- [ ] Worker 接入 `context_manager`：任务完成后写入对话摘要行
- [ ] 新会话时注入 context 末尾 N 字符

### Phase 4：自动记忆提取（未来）

**目标：** 无需手动 `/remember`，自动从对话中提炼事实

- [ ] Post-task 异步摘要提取（调用 Claude Haiku）
- [ ] 事实去重与置信度衰减

---

## 7. 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| Session 恢复策略 | `--resume actual_id` | Claude 自身管理历史，最简单可靠 |
| Context 注入条件 | 仅新会话（actual_id 空）| 已有 session 无需注入，避免 prompt 膨胀 |
| 记忆注入条件 | 仅新会话 | 同上，避免重复 |
| 事实提取 Phase 1 | 显式命令 | 精确可控，避免误提取 |
| ContextManager 缓存 | 不缓存 | 内容大，每次 load/save 即可 |
| actual_id 持久化延迟 | 去抖 30s | 与其他状态一致，重启前 flush 保证不丢 |
| Context 格式 | 人类可读摘要行 | 便于人工审计，也可机器读取 |
