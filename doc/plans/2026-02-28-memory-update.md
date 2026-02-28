# Memory Update Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend the memory system so the agent can actively replace and delete existing facts via extended `<memory>` tag syntax, with difflib dedup on add and a Jinja2 prompt template for the memory injection block.

**Architecture:** Extend `<memory>` tags to carry `op` and `idx` attributes (replace/forget). `_extract_and_strip_memory` is refactored to return structured `_MemoryOp` objects instead of raw strings. A new `prompt_loader.py` reads a Jinja2 template from `~/.nextme/prompts/memory.md` (user override) or the bundled `src/nextme/prompts/memory.md` (default) and renders the numbered fact list + usage instructions injected into new sessions.

**Tech Stack:** Python 3.12+, `difflib` (stdlib), `jinja2>=3.1`, `importlib.resources`, pytest-asyncio

**Design doc:** `doc/2026-02-28-memory-update-design.zh.md`

---

## Task 1: Schema + Settings

**Files:**
- Modify: `src/nextme/memory/schema.py`
- Modify: `src/nextme/config/schema.py`
- Test: `tests/test_memory_schema.py` (new)

### Step 1: Write the failing tests

Create `tests/test_memory_schema.py`:

```python
"""Tests for updated Fact schema and Settings.memory_max_facts."""
from datetime import datetime
from nextme.memory.schema import Fact
from nextme.config.schema import Settings


def test_fact_has_updated_at_field_defaulting_to_none():
    f = Fact(text="hello")
    assert f.updated_at is None


def test_fact_updated_at_can_be_set():
    now = datetime.now()
    f = Fact(text="hello", updated_at=now)
    assert f.updated_at == now


def test_settings_has_memory_max_facts_default_100():
    s = Settings()
    assert s.memory_max_facts == 100


def test_settings_memory_max_facts_configurable():
    s = Settings(memory_max_facts=50)
    assert s.memory_max_facts == 50
```

### Step 2: Run to verify they fail

```bash
uv run python -m pytest tests/test_memory_schema.py -v --no-cov
```
Expected: `FAILED` — `Fact` has no `updated_at`, `Settings` has no `memory_max_facts`.

### Step 3: Add `updated_at` to `Fact` in `src/nextme/memory/schema.py`

Current file (lines 10–17):
```python
class Fact(BaseModel):
    """A single remembered fact about a user or their environment."""

    text: str
    confidence: float = 0.9
    created_at: datetime = Field(default_factory=datetime.now)
    source: str = "conversation"
```

Replace with:
```python
class Fact(BaseModel):
    """A single remembered fact about a user or their environment."""

    text: str
    confidence: float = 0.9
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime | None = None
    source: str = "conversation"
```

### Step 4: Add `memory_max_facts` to `Settings` in `src/nextme/config/schema.py`

Find the `Settings` class and add after the last memory-related field (search for `memory_debounce_seconds`):

```python
memory_max_facts: int = 100
```

### Step 5: Run tests to verify they pass

```bash
uv run python -m pytest tests/test_memory_schema.py -v --no-cov
```
Expected: 4 PASSED.

### Step 6: Run full suite to confirm no regressions

```bash
uv run python -m pytest tests/ -q
```
Expected: all pass, ≥ 85% coverage.

### Step 7: Commit

```bash
git add src/nextme/memory/schema.py src/nextme/config/schema.py tests/test_memory_schema.py
git commit -m "feat(memory): add Fact.updated_at field and Settings.memory_max_facts"
```

---

## Task 2: MemoryManager — difflib dedup + replace_fact + forget_fact

**Files:**
- Modify: `src/nextme/memory/manager.py`
- Test: `tests/test_memory_manager.py` (new, separate from existing manager tests if any)

Check if `tests/test_memory_manager.py` already exists; if so, append to it.

### Step 1: Write the failing tests

Create / append to `tests/test_memory_manager.py`:

```python
"""Tests for MemoryManager.add_fact dedup, replace_fact, forget_fact."""
import pytest
from nextme.config.schema import Settings
from nextme.memory.manager import MemoryManager
from nextme.memory.schema import Fact


@pytest.fixture
def mm(tmp_path):
    settings = Settings(memory_debounce_seconds=9999)
    mgr = MemoryManager(settings, base_dir=tmp_path / "memory")
    return mgr


async def _load(mm, ctx="user1"):
    await mm.load(ctx)
    return ctx


# --- add_fact dedup ---

async def test_add_fact_dedup_merges_similar_fact(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="use uv not pip", confidence=0.8))
    mm.add_fact(ctx, Fact(text="use uv not pip!", confidence=0.9))  # ratio > 0.85
    facts = mm.get_top_facts(ctx, n=10)
    assert len(facts) == 1
    assert facts[0].text == "use uv not pip!"   # newer text wins (higher confidence)
    assert facts[0].confidence == 0.9


async def test_add_fact_dedup_keeps_old_text_when_lower_confidence(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="use uv not pip", confidence=0.9))
    mm.add_fact(ctx, Fact(text="use uv not pip!", confidence=0.7))  # lower confidence
    facts = mm.get_top_facts(ctx, n=10)
    assert len(facts) == 1
    assert facts[0].text == "use uv not pip"   # old text kept
    assert facts[0].confidence == 0.9


async def test_add_fact_different_facts_both_kept(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="use uv not pip"))
    mm.add_fact(ctx, Fact(text="coverage must be 85%"))
    assert len(mm.get_top_facts(ctx, n=10)) == 2


async def test_add_fact_max_facts_evicts_lowest_confidence(mm):
    """When facts exceed memory_max_facts, lowest-confidence ones are dropped."""
    settings = Settings(memory_debounce_seconds=9999, memory_max_facts=3)
    mgr = MemoryManager(settings, base_dir=mm._base_dir)
    ctx = await _load(mgr)
    mgr.add_fact(ctx, Fact(text="fact a", confidence=0.9))
    mgr.add_fact(ctx, Fact(text="fact b", confidence=0.5))
    mgr.add_fact(ctx, Fact(text="fact c", confidence=0.8))
    mgr.add_fact(ctx, Fact(text="fact d", confidence=0.7))  # triggers eviction
    facts = mgr.get_top_facts(ctx, n=10)
    assert len(facts) == 3
    texts = {f.text for f in facts}
    assert "fact b" not in texts   # lowest confidence evicted


# --- replace_fact ---

async def test_replace_fact_updates_text_and_sets_updated_at(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="use uv not pip", confidence=0.9))
    ok = mm.replace_fact(ctx, idx=0, new_text="use uv 2.0, uv 1.x deprecated")
    assert ok is True
    facts = mm.get_top_facts(ctx, n=10)
    assert facts[0].text == "use uv 2.0, uv 1.x deprecated"
    assert facts[0].updated_at is not None


async def test_replace_fact_returns_false_for_out_of_range_idx(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="only fact"))
    assert mm.replace_fact(ctx, idx=5, new_text="new") is False


async def test_replace_fact_returns_false_for_unloaded_context(mm):
    assert mm.replace_fact("not_loaded", idx=0, new_text="x") is False


# --- forget_fact ---

async def test_forget_fact_removes_the_fact(mm):
    ctx = await _load(mm)
    mm.add_fact(ctx, Fact(text="fact to keep", confidence=0.9))
    mm.add_fact(ctx, Fact(text="fact to delete", confidence=0.5))
    ok = mm.forget_fact(ctx, idx=1)   # idx=1 is lowest confidence
    assert ok is True
    facts = mm.get_top_facts(ctx, n=10)
    assert len(facts) == 1
    assert facts[0].text == "fact to keep"


async def test_forget_fact_returns_false_for_out_of_range_idx(mm):
    ctx = await _load(mm)
    assert mm.forget_fact(ctx, idx=0) is False   # empty store


async def test_forget_fact_returns_false_for_unloaded_context(mm):
    assert mm.forget_fact("not_loaded", idx=0) is False
```

### Step 2: Run to verify they fail

```bash
uv run python -m pytest tests/test_memory_manager.py -v --no-cov
```
Expected: all FAIL — methods don't exist yet, dedup not implemented.

### Step 3: Add `import difflib` to `src/nextme/memory/manager.py`

Add at the top with other imports:
```python
import difflib
```

### Step 4: Replace `add_fact` in `src/nextme/memory/manager.py`

Find the existing `add_fact` method (currently lines ~176–189) and replace its body:

```python
def add_fact(self, context_id: str, fact: Fact) -> None:
    """Append *fact* to the in-memory store, merging near-duplicates.

    If an existing fact has a difflib similarity ratio > 0.85, the two are
    merged in-place (higher-confidence text wins).  Otherwise the fact is
    appended.  When the store exceeds ``settings.memory_max_facts`` the
    lowest-confidence facts are evicted.

    If the context has not been loaded, the fact is silently dropped.
    """
    data = self._cache.get(context_id)
    if data is None:
        logger.warning(
            "MemoryManager.add_fact: context %r not loaded; skipping", context_id
        )
        return

    # Dedup: merge with any existing fact that is very similar.
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

    # Eviction: drop lowest-confidence facts when over the limit.
    max_facts: int = getattr(self._settings, "memory_max_facts", 100)
    if len(data.fact_store.facts) > max_facts:
        data.fact_store.facts.sort(key=lambda f: f.confidence, reverse=True)
        data.fact_store.facts = data.fact_store.facts[:max_facts]

    self._dirty.add(context_id)
```

Note: add `from datetime import datetime` import at top if not already present (it is, via the schema import chain — but add an explicit `from datetime import datetime` to `manager.py` to be safe).

### Step 5: Add `replace_fact` and `forget_fact` after `add_fact`

```python
def replace_fact(self, context_id: str, idx: int, new_text: str) -> bool:
    """Replace the text of the *idx*-th fact (sorted by confidence desc).

    Returns ``True`` on success, ``False`` if context not loaded or *idx*
    is out of range.
    """
    data = self._cache.get(context_id)
    if data is None:
        return False
    sorted_facts = sorted(
        data.fact_store.facts, key=lambda f: f.confidence, reverse=True
    )
    if idx < 0 or idx >= len(sorted_facts):
        return False
    from datetime import datetime as _dt
    sorted_facts[idx].text = new_text
    sorted_facts[idx].updated_at = _dt.now()
    self._dirty.add(context_id)
    return True

def forget_fact(self, context_id: str, idx: int) -> bool:
    """Remove the *idx*-th fact (sorted by confidence desc).

    Returns ``True`` on success, ``False`` if context not loaded or *idx*
    is out of range.
    """
    data = self._cache.get(context_id)
    if data is None:
        return False
    sorted_facts = sorted(
        data.fact_store.facts, key=lambda f: f.confidence, reverse=True
    )
    if idx < 0 or idx >= len(sorted_facts):
        return False
    data.fact_store.facts.remove(sorted_facts[idx])
    self._dirty.add(context_id)
    return True
```

### Step 6: Run new tests

```bash
uv run python -m pytest tests/test_memory_manager.py -v --no-cov
```
Expected: all PASS.

### Step 7: Run full suite

```bash
uv run python -m pytest tests/ -q
```
Expected: all pass, ≥ 85% coverage.

### Step 8: Commit

```bash
git add src/nextme/memory/manager.py tests/test_memory_manager.py
git commit -m "feat(memory): add difflib dedup, max_facts eviction, replace_fact, forget_fact"
```

---

## Task 3: Prompt Template System (jinja2 + prompt_loader)

**Files:**
- Modify: `pyproject.toml`
- Create: `src/nextme/prompts/__init__.py` (empty)
- Create: `src/nextme/prompts/memory.md`
- Create: `src/nextme/core/prompt_loader.py`
- Test: `tests/test_prompt_loader.py` (new)

### Step 1: Add jinja2 dependency and package data to `pyproject.toml`

In the `[project]` `dependencies` list, add:
```toml
"jinja2>=3.1",
```

Add a new section for package data (so `memory.md` is bundled in the wheel):
```toml
[tool.hatch.build.targets.wheel]
packages = ["src/nextme"]
artifacts = ["src/nextme/prompts/*.md"]
```

Also add to `[tool.hatch.build.targets.wheel]` — hatchling includes non-Python files automatically when they're inside the package directory. Verify with:
```bash
uv run python -c "from importlib.resources import files; print(list(files('nextme.prompts').iterdir()))"
```

### Step 2: Create `src/nextme/prompts/__init__.py`

```python
"""Bundled prompt templates for NextMe."""
```

### Step 3: Create the default template `src/nextme/prompts/memory.md`

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

### Step 4: Write failing tests for `tests/test_prompt_loader.py`

```python
"""Tests for prompt_loader.load_memory_template."""
import pytest
from pathlib import Path
import jinja2

from nextme.memory.schema import Fact
from nextme.core.prompt_loader import load_memory_template


def _make_facts(texts):
    return [Fact(text=t) for t in texts]


def test_load_memory_template_returns_jinja2_template(tmp_path):
    tmpl = load_memory_template(user_prompts_dir=tmp_path / "prompts")
    assert isinstance(tmpl, jinja2.Template)


def test_default_template_renders_numbered_facts(tmp_path):
    tmpl = load_memory_template(user_prompts_dir=tmp_path / "prompts")
    facts = _make_facts(["use uv not pip", "coverage >= 85%"])
    rendered = tmpl.render(count=len(facts), facts=facts)
    assert "0. use uv not pip" in rendered
    assert "1. coverage >= 85%" in rendered
    assert "共 2 条" in rendered


def test_default_template_includes_usage_instructions(tmp_path):
    tmpl = load_memory_template(user_prompts_dir=tmp_path / "prompts")
    rendered = tmpl.render(count=1, facts=_make_facts(["x"]))
    assert '<memory op="replace"' in rendered
    assert '<memory op="forget"' in rendered


def test_user_template_overrides_default(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "memory.md").write_text("CUSTOM {{ count }}", encoding="utf-8")
    tmpl = load_memory_template(user_prompts_dir=prompts_dir)
    rendered = tmpl.render(count=5, facts=[])
    assert rendered == "CUSTOM 5"


def test_default_template_empty_facts_renders_cleanly(tmp_path):
    tmpl = load_memory_template(user_prompts_dir=tmp_path / "prompts")
    rendered = tmpl.render(count=0, facts=[])
    assert "共 0 条" in rendered
    assert rendered.strip()  # not empty
```

### Step 5: Run to verify they fail

```bash
uv run python -m pytest tests/test_prompt_loader.py -v --no-cov
```
Expected: ImportError — `prompt_loader` doesn't exist yet.

### Step 6: Create `src/nextme/core/prompt_loader.py`

```python
"""Jinja2 prompt template loader for NextMe.

Load order for each template:
1. ``~/.nextme/prompts/<name>.md`` — user override
2. ``src/nextme/prompts/<name>.md`` — bundled default (via importlib.resources)
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import jinja2

_NEXTME_HOME = Path("~/.nextme").expanduser()


def load_memory_template(
    *,
    user_prompts_dir: Path | None = None,
) -> jinja2.Template:
    """Return a compiled Jinja2 template for the memory injection block.

    Args:
        user_prompts_dir: Override directory for tests (defaults to
            ``~/.nextme/prompts/``).
    """
    prompts_dir = user_prompts_dir if user_prompts_dir is not None else (
        _NEXTME_HOME / "prompts"
    )
    user_path = prompts_dir / "memory.md"
    if user_path.is_file():
        source = user_path.read_text(encoding="utf-8")
    else:
        source = (
            files("nextme.prompts").joinpath("memory.md").read_text(encoding="utf-8")
        )
    return jinja2.Template(source)
```

### Step 7: Install jinja2 and run tests

```bash
uv add jinja2
uv run python -m pytest tests/test_prompt_loader.py -v --no-cov
```
Expected: all PASS.

### Step 8: Verify importlib.resources can find bundled template

```bash
uv run python -c "from nextme.core.prompt_loader import load_memory_template; t = load_memory_template(); print(t.render(count=1, facts=[]))"
```
Expected: prints the rendered template with `共 1 条`.

### Step 9: Run full suite

```bash
uv run python -m pytest tests/ -q
```
Expected: all pass, ≥ 85% coverage.

### Step 10: Commit

```bash
git add pyproject.toml src/nextme/prompts/ src/nextme/core/prompt_loader.py tests/test_prompt_loader.py
git commit -m "feat(memory): add Jinja2 prompt template loader with bundled memory.md default"
```

---

## Task 4: Refactor `_extract_and_strip_memory` → multi-op parser

**Files:**
- Modify: `src/nextme/core/worker.py`
- Modify: `tests/test_core_worker.py`

This task changes the return type of `_extract_and_strip_memory` from `tuple[list[str], str]` to `tuple[list[_MemoryOp], str]`. Existing tests that call this method must be updated.

### Step 1: Update existing tests first

In `tests/test_core_worker.py`, find all tests that call `_extract_and_strip_memory` directly (search for `_extract_and_strip_memory`). They currently assert `facts` is a `list[str]`. Update them to use the new `_MemoryOp` structure.

**Existing tests to update** (around lines 1116–1156):

Replace every occurrence of `facts, stripped = SessionWorker._extract_and_strip_memory(content)` assertion blocks. The new structure: `ops, stripped = SessionWorker._extract_and_strip_memory(content)` where `ops` is `list[_MemoryOp]` and ops with `op="add"` have `.text`.

Example — `test_extract_and_strip_memory_single_fact`:
```python
def test_extract_and_strip_memory_single_fact():
    content = "Here is my answer.\n<memory>User prefers dark mode</memory>\nDone."
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "add"
    assert ops[0].text == "User prefers dark mode"
    assert "<memory>" not in stripped
    assert "Here is my answer." in stripped
    assert "Done." in stripped
```

Update ALL 6 existing `_extract_and_strip_memory` unit tests similarly (single_fact, multiple_facts, no_tags, strips_blank_lines, oversized_kept_in_display, short_fact_is_stripped).

Also update the two integration tests (`test_worker_saves_memory_facts_after_task`, `test_worker_result_content_excludes_memory_tags`) — these test behaviour through `_execute_task`, not directly, so they should not need changes unless the dispatcher logic changes (handled in Task 5).

Add new tests for the replace/forget operations:

```python
def test_extract_and_strip_memory_replace_op():
    """<memory op="replace" idx="0"> yields a replace op."""
    content = 'Done.\n<memory op="replace" idx="0">updated fact</memory>'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "replace"
    assert ops[0].idx == 0
    assert ops[0].text == "updated fact"
    assert "<memory" not in stripped


def test_extract_and_strip_memory_forget_op():
    """<memory op="forget" idx="2"> yields a forget op."""
    content = 'Done.\n<memory op="forget" idx="2"></memory>'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 1
    assert ops[0].op == "forget"
    assert ops[0].idx == 2
    assert "<memory" not in stripped


def test_extract_and_strip_memory_mixed_ops():
    """Multiple ops of different types parsed in order."""
    content = (
        '<memory>new fact</memory>\n'
        '<memory op="replace" idx="1">replacement</memory>\n'
        '<memory op="forget" idx="3"></memory>'
    )
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert len(ops) == 3
    assert ops[0].op == "add"
    assert ops[1].op == "replace" and ops[1].idx == 1
    assert ops[2].op == "forget" and ops[2].idx == 3


def test_extract_and_strip_memory_replace_missing_idx_ignored():
    """replace op without idx is silently dropped (malformed tag)."""
    content = '<memory op="replace">no idx</memory> text'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops == []
    assert "text" in stripped


def test_extract_and_strip_memory_forget_missing_idx_ignored():
    """forget op without idx is silently dropped."""
    content = '<memory op="forget"></memory> text'
    ops, stripped = SessionWorker._extract_and_strip_memory(content)
    assert ops == []
```

### Step 2: Run to see existing tests fail with new assertions

```bash
uv run python -m pytest tests/test_core_worker.py -k "extract_and_strip" --no-cov -v
```
Expected: failures (method not yet changed).

### Step 3: Refactor `_extract_and_strip_memory` in `src/nextme/core/worker.py`

**3a.** Add `_MemoryOp` dataclass just before `class SessionWorker` (after the constants):

```python
@dataclasses.dataclass
class _MemoryOp:
    """A parsed memory operation from an agent <memory> tag."""
    op: str       # "add" | "replace" | "forget"
    text: str     # new text (add / replace)
    idx: int = -1  # target index in sorted facts list (replace / forget)
```

**3b.** Update `_MEMORY_TAG_RE` (line ~60):

```python
_MEMORY_TAG_RE = re.compile(r"<memory([^>]*)>(.*?)</memory>", re.DOTALL)
```

**3c.** Replace the `_extract_and_strip_memory` static method body:

```python
@staticmethod
def _extract_and_strip_memory(content: str) -> tuple[list[_MemoryOp], str]:
    """Parse ``<memory ...>...</memory>`` tags from agent output.

    Returns ``(ops, stripped_content)`` where *ops* is a list of
    :class:`_MemoryOp` objects and *stripped_content* has all memory
    tags removed (oversized ADD blocks are kept visible).

    Supported ops:
    - ``<memory>text</memory>``                       → ADD
    - ``<memory op="replace" idx="N">text</memory>``  → REPLACE fact N
    - ``<memory op="forget" idx="N"></memory>``        → FORGET fact N
    """
    ops: list[_MemoryOp] = []

    def _collect(m: re.Match) -> str:
        attr_str: str = m.group(1)
        text: str = m.group(2).strip()
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', attr_str))
        op = attrs.get("op", "add")
        raw_idx = attrs.get("idx", "")
        idx = int(raw_idx) if raw_idx.lstrip("-").isdigit() else -1

        if op == "add":
            if len(text) > _MAX_MEMORY_FACT_CHARS:
                logger.warning(
                    "worker: oversized <memory> block (%d chars) kept in display; "
                    "use <memory> only for short, discrete facts",
                    len(text),
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
        # Malformed tag — strip it silently.
        return ""

    stripped = _MEMORY_TAG_RE.sub(_collect, content)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return ops, stripped
```

### Step 4: Run extract_and_strip tests

```bash
uv run python -m pytest tests/test_core_worker.py -k "extract_and_strip" --no-cov -v
```
Expected: all PASS.

### Step 5: Run full suite

```bash
uv run python -m pytest tests/ -q
```
Some integration tests that test `_execute_task` behaviour may fail if they inspect `memory_facts` directly — fix them in Task 5 once the dispatcher is updated.

### Step 6: Commit

```bash
git add src/nextme/core/worker.py tests/test_core_worker.py
git commit -m "refactor(memory): _extract_and_strip_memory returns _MemoryOp list with replace/forget support"
```

---

## Task 5: Worker — ops dispatch + Jinja2 template injection

**Files:**
- Modify: `src/nextme/core/worker.py`
- Modify: `tests/test_core_worker.py`

### Step 1: Write failing integration tests

Append to `tests/test_core_worker.py`:

```python
async def test_worker_dispatches_replace_op_to_memory_manager(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker calls replace_fact() when agent outputs a replace tag."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='Done.\n<memory op="replace" idx="0">updated fact</memory>'
    )
    memory_manager_mock.replace_fact = MagicMock(return_value=True)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("update memory")
    await worker._execute_task(task)
    memory_manager_mock.replace_fact.assert_called_once_with(
        "ou_user", 0, "updated fact"
    )


async def test_worker_dispatches_forget_op_to_memory_manager(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """Worker calls forget_fact() when agent outputs a forget tag."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='Done.\n<memory op="forget" idx="2"></memory>'
    )
    memory_manager_mock.forget_fact = MagicMock(return_value=True)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    task, _ = make_task("forget memory")
    await worker._execute_task(task)
    memory_manager_mock.forget_fact.assert_called_once_with("ou_user", 2)


async def test_worker_logs_warning_when_replace_idx_out_of_range(
    session, acp_registry, replier, settings, path_lock_registry,
    memory_manager_mock, caplog
):
    """Worker logs a warning when replace_fact returns False."""
    import logging
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = "sess-1"
    mock_runtime.execute = AsyncMock(
        return_value='<memory op="replace" idx="99">x</memory>'
    )
    memory_manager_mock.replace_fact = MagicMock(return_value=False)
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    with caplog.at_level(logging.WARNING, logger="nextme.core.worker"):
        await worker._execute_task(make_task("x")[0])
    assert "replace_fact" in caplog.text


async def test_worker_memory_injection_uses_numbered_format(
    session, acp_registry, replier, settings, path_lock_registry, memory_manager_mock
):
    """New-session injection uses '0. fact' numbered format from template."""
    registry, mock_runtime = acp_registry
    mock_runtime.actual_id = None   # new session
    mock_runtime.execute = AsyncMock(return_value="answer")
    memory_manager_mock.get_top_facts = MagicMock(
        return_value=[
            __import__("nextme.memory.schema", fromlist=["Fact"]).Fact(text="use uv"),
        ]
    )
    captured_task = {}
    original_execute = mock_runtime.execute

    async def capture(task, **kwargs):
        captured_task["content"] = task.content
        return "answer"

    mock_runtime.execute = capture
    worker = SessionWorker(
        session, registry, replier, settings, path_lock_registry,
        memory_manager=memory_manager_mock,
    )
    await worker._execute_task(make_task("hello")[0])
    assert "0. use uv" in captured_task.get("content", "")
```

### Step 2: Run to see failures

```bash
uv run python -m pytest tests/test_core_worker.py -k "replace_op or forget_op or out_of_range or numbered_format" --no-cov -v
```
Expected: FAIL.

### Step 3: Update `SessionWorker.__init__` to load memory template

In `src/nextme/core/worker.py`, add the import at the top:
```python
from .prompt_loader import load_memory_template
```

In `__init__`, after the existing assignments, add:
```python
self._memory_template = load_memory_template()
```

### Step 4: Replace ops-dispatch block in `_execute_task`

Find the current block (~lines 439–453):
```python
# Extract <memory> facts written by the agent …
user_id = self._session.context_id.rsplit(":", 1)[-1]
memory_facts, final_content = self._extract_and_strip_memory(final_content)
if memory_facts and self._memory_manager is not None:
    for fact_text in memory_facts:
        self._memory_manager.add_fact(
            user_id, Fact(text=fact_text, source="agent_output")
        )
    logger.debug(...)
```

Replace with:
```python
# Process <memory> operations written by the agent.
user_id = self._session.context_id.rsplit(":", 1)[-1]
memory_ops, final_content = self._extract_and_strip_memory(final_content)
if memory_ops and self._memory_manager is not None:
    for op in memory_ops:
        if op.op == "add":
            self._memory_manager.add_fact(
                user_id, Fact(text=op.text, source="agent_output")
            )
        elif op.op == "replace":
            if not self._memory_manager.replace_fact(user_id, op.idx, op.text):
                logger.warning(
                    "SessionWorker[%s]: replace_fact idx=%d out of range",
                    self._session.context_id,
                    op.idx,
                )
        elif op.op == "forget":
            if not self._memory_manager.forget_fact(user_id, op.idx):
                logger.warning(
                    "SessionWorker[%s]: forget_fact idx=%d out of range",
                    self._session.context_id,
                    op.idx,
                )
    logger.debug(
        "SessionWorker[%s]: processed %d memory ops from agent output",
        self._session.context_id,
        len(memory_ops),
    )
```

### Step 5: Update the memory injection block to use template rendering

Find the existing injection block (~lines 383–404):
```python
facts = self._memory_manager.get_top_facts(user_id, n=10)
if facts:
    fact_lines = "\n".join(f"- {f.text}" for f in facts)
    task = dataclasses.replace(
        task,
        content=f"[用户记忆]\n{fact_lines}\n\n[用户消息]\n{task.content}",
    )
    logger.debug(...)
```

Replace with:
```python
facts = self._memory_manager.get_top_facts(user_id, n=10)
if facts:
    rendered = self._memory_template.render(
        count=len(facts),
        facts=facts,
    )
    task = dataclasses.replace(
        task,
        content=f"{rendered}\n\n[用户消息]\n{task.content}",
    )
    logger.debug(
        "SessionWorker[%s]: injected %d memory facts via template",
        self._session.context_id,
        len(facts),
    )
```

### Step 6: Update existing integration tests that check injection format

In `tests/test_core_worker.py`, find the test `test_worker_memory_injection_prepends_facts` (or similar name). It currently asserts `"[用户记忆]"` and `"- fact"` format. Update it to assert the new numbered format: `"0. "` instead of `"- "`.

If no such test exists, skip this step.

### Step 7: Run new integration tests

```bash
uv run python -m pytest tests/test_core_worker.py -k "replace_op or forget_op or out_of_range or numbered_format" --no-cov -v
```
Expected: all PASS.

### Step 8: Run full suite

```bash
uv run python -m pytest tests/ -q
```
Expected: all pass, ≥ 85% coverage.

### Step 9: Commit

```bash
git add src/nextme/core/worker.py tests/test_core_worker.py
git commit -m "feat(memory): dispatch replace/forget ops in worker, inject via Jinja2 template"
```

---

## Task 6: Final verification + push

### Step 1: Run full suite one last time

```bash
uv run python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```
Expected: all pass, ≥ 85% coverage.

### Step 2: Smoke-test template rendering end-to-end

```bash
uv run python -c "
from nextme.core.prompt_loader import load_memory_template
from nextme.memory.schema import Fact
t = load_memory_template()
facts = [Fact(text='use uv not pip'), Fact(text='coverage >= 85%')]
print(t.render(count=len(facts), facts=facts))
"
```
Expected: numbered list with `0. use uv not pip` and usage instructions.

### Step 3: Push

```bash
git push origin main
```
