# NextMe 持久化设计文档

## 概述

NextMe 采用**纯文件 JSON 持久化**，无数据库依赖。所有持久数据存储在 `~/.nextme/` 目录下，通过 **asyncio 去抖写入 + 原子 rename** 保证性能与一致性。

---

## 存储布局

```
~/.nextme/
├── nextme.json                    # AppConfig（用户全局配置）
├── settings.json                  # Settings（行为调参，可选）
├── state.json                     # GlobalState（per-context 运行状态）
├── nextme.pid                     # PID 文件（启动写入，关闭删除）
├── logs/
│   └── nextme.log                 # RotatingFileHandler（10 MB × 5 备份）
├── memory/
│   └── {md5(context_id)}/         # Per-user 记忆目录
│       ├── user_context.json      # 沟通风格、语言偏好
│       ├── personal.json          # 姓名、时区、角色
│       └── facts.json             # 事实列表（带置信度）
└── threads/
    └── {session_id}/              # Per-session 对话上下文目录
        ├── context.txt            # 明文（未压缩）
        └── context.{zlib|lzma|br} # 压缩格式（超出阈值后）
        └── context.meta.json      # 压缩元数据（仅压缩时存在）
```

---

## 组件详解

### 1. StateStore — `config/state_store.py`

**职责：** 管理全局运行状态（当前活跃项目、会话 UUID 等），持久化到 `state.json`。

#### 数据结构

```
GlobalState
└── contexts: dict[context_id → UserState]
    └── UserState
        ├── last_active_project: str     # 最近使用的项目名
        └── projects: dict[name → ProjectState]
            └── ProjectState
                ├── salt: str            # session ID 生成随机盐
                ├── actual_id: str       # ACP/Claude 分配的会话 UUID
                └── executor: str        # 运行时类型（"claude" / "cc-acp"）
```

#### 读写生命周期

```
启动
 └── load() → 读 state.json → 解析到 _state（in-memory）
                      ↓ 文件不存在 → GlobalState()

运行中
 ├── get_user_state(ctx) → 直接返回 _state.contexts[ctx]（内存读）
 ├── set_user_state(ctx, s) → 更新内存 + _dirty = True
 └── start_debounce_loop() → 每 30s（可配置）若 _dirty → flush()

关闭
 └── stop() → 取消去抖任务 → flush()（最后一次强制写入）
```

#### 原子写入

```python
# 写临时文件 → os.replace（POSIX 原子）
tmp = Path(state_path).with_suffix(".tmp")
tmp.write_text(json.dumps(data), encoding="utf-8")
os.replace(tmp, state_path)
```

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
            └── source: str = "conversation"
```

#### 读写生命周期

```
首次访问
 └── load(context_id) → 读三个文件 → 缓存到 _cache[context_id]
                              ↓ 文件不存在 → 默认空对象

运行中
 ├── add_fact() / update_user_context() / update_personal_info()
 │    └── 更新 _cache + 加入 _dirty set
 ├── get_top_facts(ctx, n=15) → 按 confidence 排序，返回前 N 条
 └── start_debounce_loop() → 每 30s 若 _dirty → flush_all()

关闭
 └── stop() → 取消去抖任务 → flush_all()
```

#### 缓存策略

- `_cache: dict[str, _ContextData]` — 全量内存缓存，进程生命周期内不淘汰
- `_dirty: set[str]` — 仅脏数据才写盘，减少 I/O

---

### 3. ContextManager — `context/manager.py`

**职责：** 管理 per-session 对话上下文文件，支持透明压缩。

#### 读写生命周期

```
每次 prompt 执行后
 └── save(session_id, content)
       ├── 计算 UTF-8 字节长度
       ├── < context_max_bytes (默认 1 MB) → 写 context.txt（明文）
       └── >= context_max_bytes            → 选择算法 → 写 context.{ext} + meta.json

读取时
 └── load(session_id)
       ├── 检查目录内文件
       ├── context.txt → 直接读取
       └── context.{zlib|lzma|br} → 读 meta.json 取算法 → 解压 → 返回字符串
```

#### 无内存缓存

ContextManager **不缓存**内容，每次 load/save 直接读写文件系统。对话上下文通常较大（可达数 MB），缓存收益低，避免内存膨胀。

---

### 4. 压缩模块 — `context/compression.py`

#### 算法选择策略

```python
def choose_algorithm(size: int, settings: Settings) -> CompressionAlgorithm:
    if brotli_available():          # 安装了 brotli 包 → 最优文本压缩
        return BROTLI
    if settings.context_compression != "brotli":
        return settings.context_compression  # 用户配置优先
    # 回退：小文件用 zlib（速度优先），大文件用 lzma（压缩率优先）
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

## 配置参数

所有持久化相关参数均在 `Settings` 中定义，可通过 `~/.nextme/settings.json` 或环境变量覆盖：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `memory_debounce_seconds` | 30 | StateStore / MemoryManager 去抖写入间隔（秒）|
| `context_max_bytes` | 1,000,000 | 对话上下文压缩触发阈值（字节）|
| `context_compression` | `"zlib"` | 压缩算法偏好（`zlib` / `lzma` / `brotli`）|

---

## 原子写入模式

所有写入均通过**临时文件 + os.replace**实现，防止崩溃导致文件损坏：

```
write(content) → tmp_file  →  os.replace(tmp_file, target)
                           ↑
                    POSIX 原子操作（同目录内）
```

错误处理：
- 写入失败 → 记录日志 → 保留旧文件不变
- 读取失败 → 返回默认空值 → 进程继续运行

---

## 启动与关闭顺序

### 启动

```
1. load_app_config() + load_settings()    # 配置加载
2. StateStore.load()                       # state.json → 内存
3. SkillRegistry.load()                    # 技能文件扫描
4. StateStore.start_debounce_loop()        # 去抖写入后台任务
5. MemoryManager.start_debounce_loop()     # 去抖写入后台任务
6. write PID → ~/.nextme/nextme.pid
```

### 关闭（SIGTERM / nextme down）

```
1. 停止 Feishu WebSocket
2. 等待进行中任务完成（最长 30s）
3. 停止所有 ACP 子进程
4. MemoryManager.flush_all()              # 强制写入脏记忆
5. StateStore.stop()                      # 强制写入脏状态 + 取消去抖任务
6. 删除 ~/.nextme/nextme.pid
```

---

## 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| 存储后端 | 纯 JSON 文件 | 无依赖、易调试、单机部署 |
| 写入策略 | 去抖（30s）+ 关闭强制 | 减少 I/O，不丢最终状态 |
| 原子性 | tempfile + os.replace | POSIX 标准，无需锁 |
| 上下文缓存 | 不缓存 | 内容大，缓存收益低 |
| 记忆缓存 | 全量内存 | 数据小，频繁读取 |
| 压缩触发 | 写入时按大小判断 | 读取零开销（<1 MB 无压缩）|
| 压缩算法 | 动态选择 | Brotli 优先，stdlib 回退 |
| 数据库 | 不引入 | 单机场景 JSON 足够 |
