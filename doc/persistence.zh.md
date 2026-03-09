# NextMe 持久化设计文档

## 概述

NextMe 采用**混合持久化方案**：运行状态和记忆使用 **JSON 文件**，ACL 权限数据使用 **SQLite**。所有文件存储在 `~/.nextme/` 目录下，通过 **asyncio 去抖写入 + 原子 rename** 保证性能与一致性。

---

## 存储布局

```
~/.nextme/
├── settings.json                  # AppConfig + Settings（用户全局配置）
├── state.json                     # GlobalState（动态运行状态：绑定、会话 ID、话题记录）
├── nextme.db                      # ACL SQLite 数据库（用户权限、审批申请）
├── nextme.pid                     # PID 文件（启动写入，关闭删除）
├── logs/
│   └── nextme.log                 # RotatingFileHandler（10 MB × 5 备份）
├── memory/
│   └── {md5(context_id)}/         # Per-user 记忆目录（context_id = "chatID:userID"）
│       ├── user_context.json      # 沟通风格、语言偏好（UserContextMemory）
│       ├── personal.json          # 姓名、时区、角色（PersonalInfo）
│       └── facts.json             # 事实列表（FactStore，带置信度）
└── threads/
    └── {session_id}/              # Per-session 对话上下文目录
        ├── context.txt            # 明文（< 1 MB）
        ├── context.{zlib|lzma|br} # 压缩格式（>= 1 MB）
        └── context.meta.json      # 压缩元数据（仅压缩时存在）
```

---

## 组件详解

### 1. StateStore — `config/state_store.py`

**职责：** 管理全局动态运行状态，持久化到 `state.json`。

#### 数据结构

```
GlobalState                                    ← state.json 根节点
├── contexts: dict[context_id → UserState]
│   └── UserState
│       ├── last_active_project: str           # 最近使用的项目名
│       └── projects: dict[name → ProjectState]
│           └── ProjectState
│               ├── salt: str                  # session ID 生成随机盐（预留）
│               ├── actual_id: str             # Claude/ACP 分配的会话 UUID（重启恢复用）
│               └── executor: str              # 运行时类型（"claude" / "cc-acp"）
│
├── bindings: dict[chat_id → project_name]     # /project bind 设置的动态绑定
│
└── thread_records: dict[key → ThreadRecord]   # 活跃话题记录，key = "chat_id:thread_root_id"
    └── ThreadRecord
        ├── chat_id: str                       # Feishu 群聊 ID
        ├── thread_root_id: str                # 话题根消息 ID（唯一标识）
        ├── project_name: str                  # 话题关联的项目
        ├── created_at: datetime               # 话题创建时间
        └── last_active_at: datetime           # 最近活跃时间（touch_thread 更新）
```

#### 读写生命周期

```
启动
 └── load() → 读 state.json → 解析到 _state（in-memory）
                     ↓ 文件不存在或损坏 → GlobalState()（空默认值）

运行中
 ├── get_user_state(ctx)                → 内存读（首次自动创建空 UserState）
 ├── save_project_actual_id(ctx, proj, id) → 更新内存 + _dirty = True
 ├── set_binding(chat_id, project)      → 更新内存 + _dirty = True
 ├── register_thread(chat_id, root_id, proj) → 注册话题 + _dirty = True
 ├── unregister_thread(chat_id, root_id)    → 删除话题 + _dirty = True
 └── start_debounce_loop() → 后台任务每 30s（可配置）若 _dirty → flush()

关闭
 └── stop() → 取消去抖任务 → flush()（强制最终写入）
```

#### 原子写入

```
临时文件 (.state_tmp_XXXX.json) → os.replace → state.json
                                   ↑
                            POSIX rename(2)（同目录内，原子）
```

- 写入失败 → 临时文件自动清理，旧文件完好
- 读取失败 → 返回 `GlobalState()` 空默认值，不崩溃

#### 话题限流机制

`max_active_threads_per_chat`（默认 100）限制每个群聊的并发话题数：
- 超出限制 → 新话题任务放入 `_pending_thread_queue` 等待
- 话题关闭（`/done` 或 `unregister_thread`）→ 自动取出队首任务重新调度

---

### 2. MemoryManager — `memory/manager.py`

**职责：** 管理 per-user 长期记忆（跨会话持久），每个 context 对应三个 JSON 文件。

#### 数据结构

```
memory/{md5(context_id)}/
├── user_context.json → UserContextMemory
│   ├── preferred_language: str = "zh"
│   ├── communication_style: str
│   ├── notes: str
│   └── updated_at: datetime
│
├── personal.json → PersonalInfo
│   ├── name: str
│   ├── timezone: str
│   ├── role: str
│   └── updated_at: datetime
│
└── facts.json → FactStore
    └── facts: list[Fact]
        └── Fact
            ├── text: str
            ├── confidence: float = 0.9
            ├── created_at: datetime
            ├── updated_at: datetime | None
            └── source: str = "conversation"
```

#### 读写生命周期

```
首次访问
 └── load(context_id) → 读三个文件 → 缓存到 _cache[context_id]
                              ↓ 文件不存在 → 默认空对象

运行中
 ├── add_fact(ctx, fact)
 │    ├── difflib 相似度 > 0.85 → 合并（高置信度优先）
 │    ├── 超出 memory_max_facts → 按 confidence 淘汰低分
 │    └── 更新 _cache + 加入 _dirty set
 ├── update_user_context(ctx, ucm) → 更新 _cache + _dirty
 ├── update_personal_info(ctx, pi)  → 更新 _cache + _dirty
 ├── get_top_facts(ctx, n=15)       → 按 confidence 排序，返回前 N 条（内存读）
 └── start_debounce_loop() → 后台任务每 30s 若 _dirty → flush_all()

关闭
 └── stop() → 取消去抖任务 → flush_all()（强制写所有脏 context）
```

#### 缓存策略

- `_cache: dict[str, _ContextData]` — 全量内存缓存，进程生命周期内不淘汰
- `_dirty: set[str]` — 仅脏 context 才写盘，减少 I/O
- 目录名 = `md5(context_id)`，隔离不同用户

---

### 3. ContextManager — `context/manager.py`

**职责：** 管理 per-session 对话上下文文件，支持透明压缩。每次 prompt 执行后，ACP/DirectClaudeRuntime 将 Claude 返回的会话上下文保存到此处，以便重启后恢复 (`--resume`)。

#### 读写生命周期

```
每次 prompt 执行后
 └── save(session_id, content)
       ├── len(content.encode()) <= context_max_bytes (默认 1 MB)
       │    └── 写 context.txt（明文，UTF-8）
       └── > context_max_bytes
            └── choose_algorithm() → compress() → 写 context.{ext} + meta.json

读取时
 └── load(session_id)
       ├── 找 context.{zlib|lzma|br} → decompress() → 返回字符串
       ├── 找 context.txt             → 直接读取
       └── 无文件                     → 返回 ""
```

#### 无内存缓存

ContextManager **不缓存**内容，每次 load/save 直接读写文件系统。对话上下文通常较大（可达数 MB），缓存收益低，避免内存膨胀。

---

### 4. 压缩模块 — `context/compression.py`

#### 算法选择策略

```python
def choose_algorithm(size: int, settings: Settings) -> CompressionAlgorithm:
    if brotli_available():                  # 安装了 brotli 包 → 最优文本压缩
        return BROTLI
    if settings.context_compression != "brotli":
        return settings.context_compression  # 用户配置优先（zlib / lzma）
    # 回退：< 500 KB → zlib（速度），>= 500 KB → lzma（压缩率）
    return ZLIB if size < 500_000 else LZMA
```

| 算法 | 依赖 | 压缩级别 | 适用场景 |
|------|------|----------|----------|
| ZLIB | 标准库 | level=6 | 速度/压缩均衡，< 500 KB |
| LZMA | 标准库 | preset=6 | 最高压缩率，>= 500 KB |
| BROTLI | 可选安装 | quality=6 | 文本最优，安装后自动启用 |

#### 元数据文件（context.meta.json）

```json
{
    "algorithm": "zlib",
    "original_size": 123456,
    "compressed_size": 45678
}
```

---

### 5. ACL 数据库 — `acl/db.py`

**职责：** 管理用户访问控制列表（Owner / Collaborator）和审批申请，持久化到 `~/.nextme/nextme.db`（SQLite）。

#### 表结构

```sql
-- 已批准用户
CREATE TABLE acl_users (
    open_id      TEXT PRIMARY KEY,
    role         TEXT NOT NULL CHECK(role IN ('owner', 'collaborator')),
    display_name TEXT NOT NULL DEFAULT '',
    added_by     TEXT NOT NULL,
    added_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 权限申请（含审批历史）
CREATE TABLE acl_applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_id    TEXT NOT NULL,
    applicant_name  TEXT NOT NULL DEFAULT '',
    requested_role  TEXT NOT NULL CHECK(requested_role IN ('owner', 'collaborator')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected')),
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at    TEXT,
    processed_by    TEXT
);

-- 防止同一 applicant_id 有多条 pending 申请
CREATE UNIQUE INDEX uq_one_pending_per_applicant
    ON acl_applications(applicant_id) WHERE status = 'pending';
```

#### 角色层级

```
Admin   ← settings.json admin_users（内存，不入库）
  └── Owner      ← acl_users.role = 'owner'
        └── Collaborator  ← acl_users.role = 'collaborator'
```

- **Admin**：超管，绕过所有 ACL 检查，可审批 Owner 申请
- **Owner**：可添加/删除 Collaborator，可审批 Collaborator 申请
- **Collaborator**：有使用权，无管理权

#### 读写生命周期

```
启动
 └── AclDb.open() → 连接 SQLite → CREATE TABLE IF NOT EXISTS → PRAGMA WAL

运行中
 ├── add_user(open_id, role, ...)      → INSERT OR REPLACE
 ├── remove_user(open_id)              → DELETE
 ├── get_user(open_id)                 → SELECT
 ├── list_users(role)                  → SELECT WHERE role = ?
 ├── create_application(...)          → INSERT（检查重复 pending）
 ├── get_application(app_id)          → SELECT
 ├── update_application_status(...)   → UPDATE WHERE status = 'pending'
 └── list_pending_applications(role)  → SELECT WHERE status = 'pending'

关闭
 └── AclDb.close() → 关闭连接
```

#### WAL 模式

数据库以 `PRAGMA journal_mode=WAL` 打开，允许并发读取不阻塞写入，适合 asyncio 下的异步访问模式。

---

## 配置参数

所有持久化相关参数均在 `Settings` 中定义，可通过 `~/.nextme/settings.json` 覆盖：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `memory_debounce_seconds` | 30 | StateStore / MemoryManager 去抖写入间隔（秒）|
| `memory_max_facts` | 100 | 每个 context 最多保存的事实条数（超出淘汰低置信度）|
| `context_max_bytes` | 1,000,000 | 对话上下文压缩触发阈值（字节）|
| `context_compression` | `"zlib"` | 压缩算法偏好（`zlib` / `lzma` / `brotli`）|
| `max_active_threads_per_chat` | 100 | 每个群聊最大并发话题数 |

---

## 原子写入模式

所有 JSON 文件写入均通过**临时文件 + os.replace** 实现，防止崩溃导致文件损坏：

```
write(content) → .xxx_tmp_XXXX.json  →  os.replace(tmp, target)
                                      ↑
                             POSIX rename(2)（同文件系统，原子）
```

| 组件 | 临时文件前缀 |
|------|-------------|
| StateStore | `.state_tmp_` |
| MemoryManager | `.mem_tmp_` |
| ContextManager | `.ctx_tmp_` |

错误处理：
- 写入异常 → 临时文件自动 `unlink` 清理，旧文件完好
- 读取失败 → 返回空默认值（`GlobalState()` / `{}` / `""`），进程继续运行

---

## 启动与关闭顺序

### 启动

```
1. load_settings() + load_app_config()         # 配置加载（settings.json + nextme.json）
2. StateStore.load()                            # state.json → 内存
3. AclDb.open()                                 # 连接 nextme.db，建表
4. SkillRegistry.load()                         # 技能文件扫描
5. StateStore.start_debounce_loop()             # 去抖写入后台任务
6. MemoryManager.start_debounce_loop()          # 去抖写入后台任务
7. write PID → ~/.nextme/nextme.pid
8. 恢复活跃话题列表 → MessageHandler.restore_active_threads()
```

### 关闭（SIGTERM / nextme down）

```
1. 停止 Feishu WebSocket
2. 等待进行中任务完成（最长 30s）
3. 停止所有 ACP/Claude 子进程
4. MemoryManager.stop()   → flush_all() + 取消去抖任务
5. StateStore.stop()      → flush() + 取消去抖任务
6. AclDb.close()          → 关闭 SQLite 连接
7. 删除 ~/.nextme/nextme.pid
```

---

## 重启恢复机制

重启后 NextMe 从持久化状态中恢复：

| 恢复内容 | 数据来源 | 机制 |
|----------|----------|------|
| Claude 会话 ID | `state.json` `ProjectState.actual_id` | `--resume <id>` 重新接入已有会话 |
| 群聊项目绑定 | `state.json` `GlobalState.bindings` | 启动时读取，`/project bind` 动态更新 |
| 活跃话题列表 | `state.json` `GlobalState.thread_records` | `MessageHandler.restore_active_threads()` |
| 用户长期记忆 | `memory/{md5}/facts.json` 等 | 首次访问时懒加载 |
| 对话上下文 | `threads/{session_id}/context.*` | `ACPRuntime` / `DirectClaudeRuntime` 启动时传入 |
| 用户权限 | `nextme.db` `acl_users` | 每次请求实时查询 |

---

## 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| 运行状态 / 记忆存储 | 纯 JSON 文件 | 无依赖、易调试、可直接编辑 |
| 权限数据存储 | SQLite | 需要事务、索引、唯一约束，文件 JSON 不适合 |
| 写入策略 | 去抖（30s）+ 关闭强制 | 减少 I/O，不丢最终状态 |
| 原子性 | tempfile + os.replace | POSIX 标准，无需文件锁 |
| 上下文缓存 | 不缓存 | 内容大，缓存收益低，避免内存膨胀 |
| 记忆缓存 | 全量内存 | 数据小，频繁读取，进程内不淘汰 |
| 压缩触发 | 写入时按字节大小判断 | 读取无额外开销（< 1 MB 明文）|
| 压缩算法 | 动态选择 | Brotli 优先（文本最优），stdlib 回退 |
| 目录名 | md5(context_id) | 避免 ":" 等特殊字符，隔离用户数据 |
