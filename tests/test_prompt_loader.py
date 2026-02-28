"""Tests for prompt_loader.load_memory_template."""
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
    assert 'op="replace"' in rendered
    assert 'op="forget"' in rendered


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
    assert rendered.strip()
