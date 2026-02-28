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
    mm._dirty.discard(ctx)  # clear dirty from the first add
    mm.add_fact(ctx, Fact(text="use uv not pip!", confidence=0.7))  # lower confidence
    facts = mm.get_top_facts(ctx, n=10)
    assert len(facts) == 1
    assert facts[0].text == "use uv not pip"   # old text kept
    assert facts[0].confidence == 0.9
    assert ctx not in mm._dirty  # no mutation → not dirty


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
