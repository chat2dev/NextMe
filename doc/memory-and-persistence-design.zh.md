# NextMe 记忆与持久化集成设计

> **文档状态：** 设计定稿（Phase 1 待实现）
> **关联文档：**
> - `persistence.zh.md` — 存储布局与底层组件详解
> - `multi-project-isolation.zh.md` — 多项目并行隔离设计

---

## 1. 背景与现状

### 1.1 已实现的基础设施

`persistence.zh.md` 描述了三个存储组件，底层已正确实现，但均**未与 Worker 连接**：

| 组件 | 实现 | 接入状态 |
|------|------|----------|
| `StateStore` — `state.json` | ✅ | ⚠️ 部分：bindings 已接入，**actual_id 未持久化** |
| `ContextManager` — `threads/` | ✅ | ❌ 文件从不写入 |
| `MemoryManager` — `memory/` | ✅ | ❌ 无提取管道，无注入点 |

### 1.2 核心缺口

```
Worker._execute_task() 完成
  → runtime.actual_id 更新（内存）
  → session.actual_id 同步（内存）
  → ❌ 未写入 state_store → Bot 重启后 session 从头开始
  → ❌ 未写入 context_manager → 对话日志从不落盘
  → ❌ 未读取 memory_manager → Claude 每次都不认识用户
```

---

## 2. 记忆架构（参考 Mem0 / Letta / Zep / OpenClaw）

借鉴主流 AI Agent 记忆系统的三层架构：

```
┌─────────────────────────────────────────────────────────────┐
│  Working Memory（工作记忆）— 当前 session 的活跃上下文      │
│  • session.active_task  • progress_buffer  • card_id        │
│  生命周期：单次任务                                          │
├─────────────────────────────────────────────────────────────┤
│  Episodic Memory（情节记忆）— 对话历史日志                  │
│  ~/.nextme/threads/{session_id}/context.txt(.zlib/.lzma/.br) │
│  生命周期：per-session，可压缩，可注入新会话                 │
├─────────────────────────────────────────────────────────────┤
│  Semantic Memory（语义记忆）— 长期事实与偏好                │
│  ~/.nextme/memory/{md5(context_id)}/facts.json              │
│  生命周期：永久，跨 session 积累，按置信度排序              │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 记忆作用域（两个维度）

参考 Mem0 的层级模型，NextMe 区分两种作用域：

| 作用域 | 内容举例 | 存储路径 |
|--------|----------|----------|
| **用户级**（User-scoped） | 偏好中文、习惯用 uv、时区 Asia/Shanghai | `memory/{md5(context_id)}/` |
| **项目级**（Project-scoped） | repo-a 用 FastAPI、覆盖率要求 85% | `memory/{md5(context_id:project_name)}/` |

用户级事实跨项目共享；项目级事实隔离在各项目内。

### 2.2 记忆类型（三类事实）

```json
{
  "text": "用户偏好中文回复，技术术语可以英文",
  "category": "user_preference",
  "confidence": 0.95,
  "source": "conversation",
  "created_at": "2026-02-26T10:00:00"
}

{
  "text": "repo-a 使用 uv 管理依赖，禁止使用 pip",
  "category": "project_context",
  "confidence": 0.9,
  "source": "user_command:/remember"
}

{
  "text": "提交前务必运行 uv run python -m pytest tests/",
  "category": "procedural",
  "confidence": 0.85,
  "source": "conversation"
}
```

---

## 3. Session 持久化（actual_id）

### 3.1 问题

`ProjectState.actual_id` 字段已存在于 `GlobalState.contexts[ctx].projects[name]`，但：
- `StateStore` 没有写 `actual_id` 的方法
- `Worker` 不持有 `state_store` 引用
- Bot 重启后 actual_id 丢失，Claude 无法 `--resume`

### 3.2 数据流

```
任务完成
  Worker._execute_task() 正常返回
    ├─→ session.actual_id = runtime.actual_id
    └─→ state_store.save_project_actual_id(ctx, project_name, actual_id)
                                            ↑
                                       去抖写入（30s 或关闭时强制）

Bot 重启
  TaskDispatcher.dispatch() → UserContext.get_or_create_session()
    └─→ session.actual_id = state_store.get_project_actual_id(ctx, project_name)
                                        ↑
                                   从 state.json 恢复

DirectClaudeRuntime.execute()
  if self._actual_id:
      args += ["--resume", self._actual_id]   ← 已实现，只需传正确的值
```

### 3.3 StateStore 新增 API

```python
def save_project_actual_id(
    self, context_id: str, project_name: str, actual_id: str
) -> None:
    user_state = self.get_user_state(context_id)
    if project_name not in user_state.projects:
        user_state.projects[project_name] = ProjectState()
    user_state.projects[project_name].actual_id = actual_id
    self._dirty = True

def get_project_actual_id(self, context_id: str, project_name: str) -> str:
    state = self._require_loaded()
    user = state.contexts.get(context_id)
    if not user:
        return ""
    proj = user.projects.get(project_name)
    return proj.actual_id if proj else ""
```

### 3.4 /new 清除持久化

```python
# handle_new()
session.actual_id = ""
if state_store:
    state_store.save_project_actual_id(context_id, project_name, "")
```

---

## 4. 对话上下文文件（ContextManager 集成）

### 4.1 定位与价值

```
正常对话延续：   DirectClaudeRuntime --resume actual_id  ← 主路径（不需要 ContextManager）
Session 过期：   Claude 服务端 TTL 到期                  ← ContextManager 降级保底
Bot 重启：       actual_id 已持久化 → --resume 仍有效    ← 不需要 ContextManager
新用户首次使用：  注入项目事实 + 用户偏好                 ← MemoryManager 负责
```

**结论：** ContextManager 是"Claude session 过期后的降级保底"和"人工审计日志"，不是主路径。

### 4.2 写入策略（OpenClaw 日志模式）

每次任务完成后，追加摘要行（异步，不阻塞）：

```
[2026-02-26 20:00:00] Q: 帮我分析 dispatcher.py 的架构
[2026-02-26 20:00:08] A: TaskDispatcher 负责路由消息到 SessionWorker，支持多项目并行...
[2026-02-26 20:05:00] Q: 给 dispatch 方法加单元测试
[2026-02-26 20:05:45] A: 已在 test_core_dispatcher.py 添加 12 个测试，覆盖路由/绑定/命令...
```

- **session_id** = `context_id:project_name`
- **压缩阈值**：超过 `settings.context_max_bytes`（默认 1MB）自动压缩
- **摘要长度**：Q 截取 100 字，A 截取 200 字

### 4.3 注入策略（仅新会话）

```python
is_new_session = not session.actual_id

if is_new_session and context_manager:
    history = await context_manager.load(session_id)
    if history:
        # 只注入末尾 2000 字符（最近对话最有价值）
        tail = history[-2000:]
        task.content = f"[最近对话摘要]\n{tail}\n\n{task.content}"
```

---

## 5. 长期记忆（MemoryManager 集成）

### 5.1 注入策略（仅新会话）

参考 Letta 的"Core Memory Blocks"模式，仅在新会话开始时注入：

```python
is_new_session = not session.actual_id

if is_new_session and memory_manager:
    await memory_manager.load(context_id)
    facts = memory_manager.get_top_facts(context_id, n=10)
    if facts:
        facts_text = "\n".join(f"- {f.text}" for f in facts)
        task.content = (
            f"[关于用户和项目的已知信息]\n{facts_text}\n\n{task.content}"
        )
```

**注入条件：** 仅 `actual_id == ""`（新会话），避免重复注入到有历史的 session。

### 5.2 写入路径

#### Phase 1：显式命令（立即实现）

```
/remember <text>
  → Fact(text=text, confidence=0.95, source="user_command")
  → memory_manager.add_fact(context_id, fact)
  → 去抖写入 facts.json
```

**示例：**
```
/remember 这个项目用 uv 管理依赖，不要用 pip
/remember 测试覆盖率：新代码 90%，整体 85%
/remember 提交前运行 uv run python -m pytest tests/
```

#### Phase 2：自动提取（未来）

参考 Mem0 的两阶段流水线：

```
任务完成后（异步，低优先级）
  ↓
Phase 1 - 提取：
  LLM(Haiku) 分析对话摘要
  → 候选事实列表（3-5 条）
  → 过滤低信号噪声
  ↓
Phase 2 - 合并：
  计算与已有事实的语义相似度（余弦相似）
  > 0.8 → 合并更新（保留高置信度）
  < 0.8 → 追加新事实
  ↓
  持久化 + 更新 confidence
```

**触发条件：** 每 10 次任务后 / context 超过阈值 / 正常关闭时

### 5.3 记忆管理命令

| 命令 | 功能 |
|------|------|
| `/remember <text>` | 添加事实（用户级） |
| `/remember project <text>` | 添加事实（项目级） |
| `/memory` | 列出事实（按 confidence 排序） |
| `/forget <n>` | 删除第 n 条事实 |

### 5.4 去重策略（参考 Zep Temporal Knowledge Graph）

```python
def add_fact(context_id, new_fact):
    existing = get_top_facts(context_id, n=50)
    for old in existing:
        # 字符串相似度（简化版，不引入 embedding）
        similarity = difflib.SequenceMatcher(
            None, old.text.lower(), new_fact.text.lower()
        ).ratio()
        if similarity > 0.8:
            # 更新：保留更高 confidence，更新文本
            old.text = new_fact.text if new_fact.confidence >= old.confidence else old.text
            old.confidence = max(old.confidence, new_fact.confidence)
            return  # 不追加重复
    # 新事实
    facts.append(new_fact)
```

---

## 6. Worker 接入方案

### 6.1 新增依赖参数

```python
class SessionWorker:
    def __init__(
        self,
        session: Session,
        acp_registry: ACPRuntimeRegistry,
        replier: Replier,
        settings: Settings,
        path_lock_registry: PathLockRegistry,
        # 新增（可选，None 时降级跳过）
        state_store: Optional[StateStore] = None,
        context_manager: Optional[ContextManager] = None,
        memory_manager: Optional[MemoryManager] = None,
    ):
```

### 6.2 _execute_task 钩子

```python
async def _execute_task(self, task: Task) -> None:
    is_new_session = not self._session.actual_id
    session_id = f"{self._session.context_id}:{self._session.project_name}"
    context_id = self._session.context_id

    # ── BEFORE EXECUTE ─────────────────────────────────────────────────
    if is_new_session:
        # [1] 注入 semantic memory（事实）
        if self._memory_manager:
            await self._memory_manager.load(context_id)
            facts = self._memory_manager.get_top_facts(context_id, n=10)
            if facts:
                task = _prepend_facts(task, facts)

        # [2] 注入 episodic memory（历史摘要）
        if self._context_manager:
            history = await self._context_manager.load(session_id)
            if history:
                task = _prepend_history(task, history[-2000:])

    # ── EXECUTE（现有逻辑不变）──────────────────────────────────────────
    ...

    # ── AFTER EXECUTE ──────────────────────────────────────────────────
    # [3] 持久化 actual_id
    if self._state_store and runtime.actual_id:
        self._state_store.save_project_actual_id(
            context_id, self._session.project_name, runtime.actual_id
        )

    # [4] 写入对话日志（异步，不阻塞）
    if self._context_manager:
        asyncio.ensure_future(
            self._context_manager.append(
                session_id,
                f"[{_now()}] Q: {task.content[:100]}\n"
                f"[{_now()}] A: {result_summary[:200]}"
            )
        )
```

### 6.3 Dispatcher 透传

`TaskDispatcher.__init__` 接收 `context_manager` 和 `memory_manager`，并在创建 `SessionWorker` 时透传：

```python
SessionWorker(
    session=session,
    ...
    state_store=self._state_store,
    context_manager=self._context_manager,
    memory_manager=self._memory_manager,
)
```

---

## 7. 实现计划（分阶段）

### Phase 1：Session 持久化（高优先级）

**目标：** Bot 重启后 Claude session 自动恢复

- [ ] `StateStore.save_project_actual_id()` + `get_project_actual_id()`
- [ ] `Worker` 接入 `state_store`，任务完成后写入 actual_id
- [ ] `UserContext.get_or_create_session()` 恢复 actual_id
- [ ] `/new` 命令清除持久化的 actual_id
- [ ] 测试：重启后 session 恢复验证

**验证：** 发几条消息后重启 Bot，再发消息确认对话历史未丢失。

### Phase 2：显式记忆命令（中优先级）

**目标：** 用户手动教 Bot 记住偏好和项目知识

- [ ] `/remember <text>` 命令（写入 FactStore）
- [ ] `/memory` 查看命令
- [ ] Worker 接入 `memory_manager`，新会话注入事实
- [ ] 简单字符串去重逻辑

**验证：** `/remember 用 uv 管理依赖`，`/new`，发消息确认 Claude 知道。

### Phase 3：Context 文件集成（低优先级）

**目标：** Claude session 过期时提供降级上下文

- [ ] Worker 接入 `context_manager`
- [ ] 任务完成后追加对话摘要行
- [ ] 新会话时注入末尾 2000 字符

### Phase 4：自动记忆提取（未来）

**目标：** 无需手动 `/remember`，自动从对话提炼事实

- [ ] Post-task 异步摘要提取（Claude Haiku）
- [ ] 事实去重与置信度更新
- [ ] 语义相似度替代字符串匹配（可选：sqlite-vec）

---

## 8. 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| Session 恢复策略 | `--resume actual_id` | Claude 自身管理历史，最可靠 |
| 记忆存储后端 | JSON 文件（无向量数据库）| 无外部依赖，易调试，单机场景足够 |
| 语义搜索 | Phase 1 跳过（按 confidence 排序） | 避免引入 embedding 依赖 |
| 去重策略 | 字符串相似度（difflib）| 简单够用，可升级为向量相似度 |
| 注入时机 | 仅新会话（actual_id 空）| 避免重复注入，prompt 保持精简 |
| 注入上限 | Top-10 facts + 2000 chars history | 控制 context 增长，≤ 1500 tokens |
| 自动提取 | Phase 2+ | Phase 1 手动命令更精确可控 |
| 压缩算法 | threads: zlib（已有）<br>facts: 不压缩（JSON 小）| 简单，避免过度工程 |

---

## 9. 参考项目

| 项目 | 核心借鉴点 |
|------|-----------|
| **OpenClaw** | Markdown 日志 + MEMORY.md 双层模式；Context 压缩前强制写入记忆 |
| **Mem0** | 用户级 + 项目级两层作用域；两阶段提取（extraction → consolidation）|
| **Letta** | Core Memory Blocks 模式（少量高置信度事实 pinned 到 system prompt）|
| **Zep** | Temporal Knowledge Graph；双时间戳；语义去重而非删除 |
| **Cowork** | 团队协作场景下的 session 隔离（per-channel per-user）|
