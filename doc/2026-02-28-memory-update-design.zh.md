# NextMe 长期 Memory 更新方案设计

> **日期：** 2026-02-28
> **状态：** 设计定稿，待实现
> **关联文档：** `memory-and-persistence-design.zh.md`

---

## 1. 背景与问题

当前 memory 系统（已实现）：

| 能力 | 状态 |
|------|------|
| `MemoryManager` 存储/加载/去抖写盘 | ✅ |
| Worker 注入 top-10 facts 到新会话 | ✅ |
| Agent 通过 `<memory>` tag 写入 facts | ✅ |
| `confidence` 字段支持排序 | ✅ |
| `add_fact()` 去重/合并 | ❌ |
| Agent 主动 UPDATE / DELETE 旧事实 | ❌ |
| 事实数量上限保护 | ❌ |
| 注入格式带编号（支持 idx 引用） | ❌ |
| Prompt 模板化（可用户自定义） | ❌ |

**核心痛点：** `add_fact()` 只追加，旧的冲突事实永久残留，事实列表无限增长。

---

## 2. 设计目标

- Agent 在回复时能主动 **新增 / 替换 / 删除** 已有事实
- `add_fact()` 自动对相似事实去重合并（difflib 兜底）
- 注入 prompt 来自可自定义的 Jinja2 模板
- 无额外 LLM 调用，无外部数据库依赖

---

## 3. 方案选择

| 方案 | 描述 | 结论 |
|------|------|------|
| **A：扩展 `<memory>` tag 语法** | Agent 写带 op/idx 属性的 tag，worker 解析执行 | ✅ 采用 |
| B：Key-based Upsert | Agent 给每条事实指定 key，upsert 语义 | 跨 session key 一致性难保证 |
| C：仅 difflib 去重 | add_fact 时自动合并 | 作为兜底叠加到方案 A |

最终方案：**A + C 叠加**。

---

## 4. Tag 语法

```
# 新增（现有行为不变）
<memory>用 uv 管理依赖，不要用 pip</memory>

# 替换第 idx 条事实（0-based，对应注入列表编号）
<memory op="replace" idx="0">用 uv 2.0 管理依赖，uv 1.x 已弃用</memory>

# 删除第 idx 条事实
<memory op="forget" idx="1"></memory>
```

**大块内容保护（已有）：** `len(text) > 500` 的 ADD 操作，内容保留在显示中并同时记录。

### Regex

```python
_MEMORY_TAG_RE = re.compile(r'<memory([^>]*)>(.*?)</memory>', re.DOTALL)

def _parse_attrs(attr_str: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', attr_str))
```

---

## 5. MemoryManager 变更

### 5.1 新增方法

```python
def replace_fact(self, context_id: str, idx: int, new_text: str) -> bool:
    """替换 get_top_facts() 排序后第 idx 条事实的文本。"""
    data = self._cache.get(context_id)
    if data is None:
        return False
    sorted_facts = sorted(data.fact_store.facts, key=lambda f: f.confidence, reverse=True)
    if idx < 0 or idx >= len(sorted_facts):
        return False
    sorted_facts[idx].text = new_text
    sorted_facts[idx].updated_at = datetime.now()
    self._dirty.add(context_id)
    return True

def forget_fact(self, context_id: str, idx: int) -> bool:
    """删除 get_top_facts() 排序后第 idx 条事实。"""
    data = self._cache.get(context_id)
    if data is None:
        return False
    sorted_facts = sorted(data.fact_store.facts, key=lambda f: f.confidence, reverse=True)
    if idx < 0 or idx >= len(sorted_facts):
        return False
    data.fact_store.facts.remove(sorted_facts[idx])
    self._dirty.add(context_id)
    return True
```

### 5.2 add_fact 加 difflib 去重

```python
import difflib

def add_fact(self, context_id: str, fact: Fact) -> None:
    data = self._cache.get(context_id)
    if data is None:
        logger.warning("MemoryManager.add_fact: context %r not loaded; skipping", context_id)
        return

    # 去重：与已有事实字符串相似度 > 0.85 则合并
    for existing in data.fact_store.facts:
        ratio = difflib.SequenceMatcher(
            None, existing.text.lower(), fact.text.lower()
        ).ratio()
        if ratio > 0.85:
            if fact.confidence >= existing.confidence:
                existing.text = fact.text
                existing.confidence = fact.confidence
                existing.updated_at = datetime.now()
            self._dirty.add(context_id)
            return

    data.fact_store.facts.append(fact)

    # 上限保护：超过 max_facts 淘汰最低 confidence
    max_facts = getattr(self._settings, 'memory_max_facts', 100)
    if len(data.fact_store.facts) > max_facts:
        data.fact_store.facts.sort(key=lambda f: f.confidence, reverse=True)
        data.fact_store.facts = data.fact_store.facts[:max_facts]

    self._dirty.add(context_id)
```

---

## 6. Schema 变更

### Fact

```python
class Fact(BaseModel):
    text: str
    confidence: float = 0.9
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime | None = None   # 新增
    source: str = "conversation"
```

### Settings

```python
memory_max_facts: int = 100   # 新增
```

---

## 7. Prompt 模板系统

### 文件结构

```
~/.nextme/
└── prompts/
    └── memory.md          # 用户自定义（优先）

src/nextme/
└── prompts/
    └── memory.md          # 内置默认（fallback）
```

### 默认模板 `src/nextme/prompts/memory.md`

```jinja2
[用户记忆] (共 {{ count }} 条，可在回复末尾用 <memory> 标签更新)
{% for fact in facts %}{{ loop.index0 }}. {{ fact.text }}
{% endfor %}
记忆操作（仅在有必要时使用）：
- 新增: <memory>内容</memory>
- 更新: <memory op="replace" idx="0">新内容</memory>
- 删除: <memory op="forget" idx="1"></memory>
注意：<memory> 标签内容不会展示给用户，仅用于记录简短事实（< 500 字）。
```

### 模板加载器 `src/nextme/core/prompt_loader.py`

```python
from importlib.resources import files
from pathlib import Path
import jinja2

_NEXTME_HOME = Path("~/.nextme").expanduser()

def load_memory_template() -> jinja2.Template:
    """加载顺序：用户自定义 → 内置默认。"""
    user_path = _NEXTME_HOME / "prompts" / "memory.md"
    if user_path.is_file():
        source = user_path.read_text(encoding="utf-8")
    else:
        source = (files("nextme.prompts") / "memory.md").read_text(encoding="utf-8")
    return jinja2.Template(source)
```

模板在 `SessionWorker.__init__` 中加载一次并缓存为 `self._memory_template`。

---

## 8. Worker 集成

### 8.1 _MemoryOp 数据类

```python
@dataclasses.dataclass
class _MemoryOp:
    op: str        # "add" | "replace" | "forget"
    text: str
    idx: int = -1
```

### 8.2 _extract_and_strip_memory 重构

```python
def _extract_and_strip_memory(content: str) -> tuple[list[_MemoryOp], str]:
    ops: list[_MemoryOp] = []

    def _collect(m: re.Match) -> str:
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        text = m.group(2).strip()
        op = attrs.get("op", "add")
        idx = int(attrs.get("idx", -1))

        if op == "add":
            if len(text) > _MAX_MEMORY_FACT_CHARS:
                logger.warning(
                    "worker: oversized <memory> block (%d chars) kept in display", len(text)
                )
                ops.append(_MemoryOp(op="add", text=text))
                return text
            ops.append(_MemoryOp(op="add", text=text))
            return ""
        elif op == "replace" and idx >= 0 and text:
            ops.append(_MemoryOp(op="replace", text=text, idx=idx))
            return ""
        elif op == "forget" and idx >= 0:
            ops.append(_MemoryOp(op="forget", text="", idx=idx))
            return ""
        return ""

    stripped = _MEMORY_TAG_RE.sub(_collect, content)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return ops, stripped
```

### 8.3 _execute_task 操作派发

```python
# Step 5 完成后
user_id = self._session.context_id.rsplit(":", 1)[-1]
ops, final_content = self._extract_and_strip_memory(final_content)

if ops and self._memory_manager is not None:
    for op in ops:
        if op.op == "add":
            self._memory_manager.add_fact(
                user_id, Fact(text=op.text, source="agent_output")
            )
        elif op.op == "replace":
            if not self._memory_manager.replace_fact(user_id, op.idx, op.text):
                logger.warning("worker: replace_fact idx=%d out of range", op.idx)
        elif op.op == "forget":
            if not self._memory_manager.forget_fact(user_id, op.idx):
                logger.warning("worker: forget_fact idx=%d out of range", op.idx)
```

### 8.4 注入逻辑更新

```python
if not runtime.actual_id and self._memory_manager is not None:
    await self._memory_manager.load(user_id)
    facts = self._memory_manager.get_top_facts(user_id, n=10)
    if facts:
        rendered = self._memory_template.render(count=len(facts), facts=facts)
        task = dataclasses.replace(
            task,
            content=f"{rendered}\n\n[用户消息]\n{task.content}",
        )
```

---

## 9. 完整数据流

```
新会话开始
  └─ load memory → get_top_facts(n=10)
  └─ render memory_template（带编号 + 操作说明）→ 注入 task.content

Agent 执行
  └─ agent 在回复末尾写 <memory ...> tags

任务完成
  └─ _extract_and_strip_memory(final_content)
       ├─ op="add"     → add_fact()     [difflib 去重兜底]
       ├─ op="replace" → replace_fact() [按 idx 替换]
       └─ op="forget"  → forget_fact()  [按 idx 删除]
  └─ 过滤后的 final_content → build_result_card()
  └─ memory_manager 去抖写盘（30s debounce）
```

---

## 10. 边界情况

| 情况 | 处理 |
|------|------|
| `replace/forget` idx 越界 | log warning，忽略该操作 |
| 继续会话中 agent 写 replace/forget | 执行操作（agent 无 idx 上下文时可能越界 → warning 兜底） |
| difflib 误判（相似但不同义） | confidence 取较高值，文本以新 fact 为准 |
| max_facts=100 触发淘汰 | 按 confidence 升序淘汰末尾 |
| 大块内容误用 `<memory>`（> 500 字） | 内容保留在显示中，同时记录为 fact |
| 用户未创建 `~/.nextme/prompts/memory.md` | fallback 到内置默认模板 |

---

## 11. 实现计划（分步）

### Step 1：Schema + Settings
- `Fact` 新增 `updated_at` 字段
- `Settings` 新增 `memory_max_facts: int = 100`

### Step 2：MemoryManager
- `add_fact()` 加 difflib 去重 + max_facts 保护
- 新增 `replace_fact()` / `forget_fact()`

### Step 3：Prompt 模板系统
- 新增 `src/nextme/prompts/memory.md`（内置默认）
- 新增 `src/nextme/core/prompt_loader.py`
- `pyproject.toml` 新增 `jinja2` 依赖
- `pyproject.toml` 新增 `[tool.setuptools.package-data]` 包含 prompts/*.md

### Step 4：Worker 重构
- `_extract_and_strip_memory` 重构为多操作解析，返回 `list[_MemoryOp]`
- `_execute_task` 中操作派发（add/replace/forget）
- 注入逻辑改用模板渲染

### Step 5：测试
- `MemoryManager`: replace_fact / forget_fact / add_fact 去重 / max_facts 淘汰
- `_extract_and_strip_memory`: replace/forget tag 解析、idx 越界、大块保护
- Worker 集成：replace/forget 操作派发、模板注入格式
- 模板加载器：用户自定义优先、fallback 内置

---

## 12. 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| 触发主体 | Agent 主动（tag 语法） | 无额外 LLM 调用，低延迟 |
| idx 引用方式 | 注入时带编号，agent 引用编号 | 明确无歧义，无需模糊匹配 |
| 去重策略 | difflib SequenceMatcher > 0.85 | 无 embedding 依赖，stdlib 原生 |
| 模板系统 | Jinja2 + 文件 fallback | 用户可完整自定义，无硬编码 |
| 存储后端 | JSON 文件（不变） | 无外部依赖 |
| 语义搜索 | 不引入（按 confidence 排序） | 避免 embedding 依赖 |
