"""Comprehensive tests for nextme.skills: loader, registry, invoker."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nextme.skills.loader import (
    Skill,
    SkillMeta,
    load_skill_file,
    _parse_inline_list,
    _parse_frontmatter,
)
from nextme.skills.registry import SkillRegistry
from nextme.skills.invoker import SkillInvoker


# ---------------------------------------------------------------------------
# _parse_inline_list tests
# ---------------------------------------------------------------------------


class TestParseInlineList:
    def test_empty_list(self):
        result = _parse_inline_list("[]")
        assert result == []

    def test_two_items(self):
        result = _parse_inline_list("[item1, item2]")
        assert result == ["item1", "item2"]

    def test_mixed_quoted_items(self):
        result = _parse_inline_list('[item1, "item2", \'item3\']')
        assert result == ["item1", "item2", "item3"]

    def test_not_a_list_returns_empty(self):
        result = _parse_inline_list("not a list")
        assert result == []

    def test_spaced_items_stripped(self):
        result = _parse_inline_list("[  spaced  ]")
        assert result == ["spaced"]

    def test_single_item(self):
        result = _parse_inline_list("[only_one]")
        assert result == ["only_one"]

    def test_items_with_extra_spaces(self):
        result = _parse_inline_list("[  a  ,  b  ,  c  ]")
        assert result == ["a", "b", "c"]

    def test_double_quoted_strings(self):
        result = _parse_inline_list('["bash", "write"]')
        assert result == ["bash", "write"]

    def test_single_quoted_strings(self):
        result = _parse_inline_list("['bash', 'write']")
        assert result == ["bash", "write"]


# ---------------------------------------------------------------------------
# _parse_frontmatter tests
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_parses_key_value_lines(self):
        text = "name: Code Review\ntrigger: review"
        result = _parse_frontmatter(text)
        assert result["name"] == "Code Review"
        assert result["trigger"] == "review"

    def test_lowercases_keys(self):
        text = "Name: Test\nTRIGGER: test"
        result = _parse_frontmatter(text)
        assert "name" in result
        assert "trigger" in result

    def test_handles_inline_lists(self):
        text = "tools_allowlist: []\ntools_denylist: [bash, write]"
        result = _parse_frontmatter(text)
        assert result["tools_allowlist"] == []
        assert result["tools_denylist"] == ["bash", "write"]

    def test_skips_blank_lines(self):
        text = "name: Test\n\ntrigger: test\n\n"
        result = _parse_frontmatter(text)
        assert result["name"] == "Test"
        assert result["trigger"] == "test"

    def test_skips_comment_lines(self):
        text = "# This is a comment\nname: Test\n# Another comment\ntrigger: test"
        result = _parse_frontmatter(text)
        assert "name" in result
        assert "trigger" in result
        assert len(result) == 2  # Only name and trigger

    def test_strips_quotes_from_string_values(self):
        text = 'name: "Quoted Name"\ntrigger: \'single_quoted\''
        result = _parse_frontmatter(text)
        assert result["name"] == "Quoted Name"
        assert result["trigger"] == "single_quoted"

    def test_description_parsed(self):
        text = "name: Test\ntrigger: test\ndescription: This is a description"
        result = _parse_frontmatter(text)
        assert result["description"] == "This is a description"

    def test_empty_text(self):
        result = _parse_frontmatter("")
        assert result == {}

    def test_only_comments(self):
        text = "# comment1\n# comment2"
        result = _parse_frontmatter(text)
        assert result == {}


# ---------------------------------------------------------------------------
# load_skill_file tests
# ---------------------------------------------------------------------------


class TestLoadSkillFile:
    def test_load_valid_skill_file(self, tmp_path):
        skill_file = tmp_path / "review.md"
        skill_file.write_text(
            "---\nname: Code Review\ntrigger: review\ndescription: Review code\n"
            "tools_allowlist: []\ntools_denylist: [bash]\n---\n\nReview the code: {user_input}\n"
        )
        skill = load_skill_file(skill_file)
        assert skill.meta.name == "Code Review"
        assert skill.meta.trigger == "review"
        assert skill.meta.description == "Review code"
        assert skill.meta.tools_denylist == ["bash"]
        assert skill.meta.tools_allowlist == []
        assert "{user_input}" in skill.template

    def test_raises_value_error_for_file_without_frontmatter(self, tmp_path):
        skill_file = tmp_path / "no_frontmatter.md"
        skill_file.write_text("This file has no frontmatter block.\n")

        with pytest.raises(ValueError, match="frontmatter"):
            load_skill_file(skill_file)

    def test_raises_value_error_for_missing_name_field(self, tmp_path):
        skill_file = tmp_path / "missing_name.md"
        skill_file.write_text("---\ntrigger: test\n---\n\nTemplate here.\n")

        with pytest.raises(ValueError, match="name"):
            load_skill_file(skill_file)

    def test_raises_value_error_for_missing_trigger_field(self, tmp_path):
        skill_file = tmp_path / "missing_trigger.md"
        skill_file.write_text("---\nname: Test Skill\n---\n\nTemplate here.\n")

        with pytest.raises(ValueError, match="trigger"):
            load_skill_file(skill_file)

    def test_handles_empty_tools_lists(self, tmp_path):
        skill_file = tmp_path / "empty_tools.md"
        skill_file.write_text(
            "---\nname: Empty Tools\ntrigger: empty\ntools_allowlist: []\ntools_denylist: []\n---\n\nTemplate.\n"
        )
        skill = load_skill_file(skill_file)
        assert skill.meta.tools_allowlist == []
        assert skill.meta.tools_denylist == []

    def test_template_is_stripped(self, tmp_path):
        skill_file = tmp_path / "template_test.md"
        skill_file.write_text(
            "---\nname: Test\ntrigger: test\n---\n\nActual template content.\n\n"
        )
        skill = load_skill_file(skill_file)
        # Should strip leading/trailing whitespace from template
        assert skill.template.strip() == "Actual template content."

    def test_loads_skill_with_multiple_denylist_tools(self, tmp_path):
        skill_file = tmp_path / "multi_deny.md"
        skill_file.write_text(
            "---\nname: Restricted\ntrigger: restricted\ntools_denylist: [bash, write, read]\n---\n\nTemplate.\n"
        )
        skill = load_skill_file(skill_file)
        assert skill.meta.tools_denylist == ["bash", "write", "read"]

    def test_skill_without_description_defaults_to_empty(self, tmp_path):
        skill_file = tmp_path / "no_desc.md"
        skill_file.write_text(
            "---\nname: No Desc\ntrigger: nodesc\n---\n\nTemplate.\n"
        )
        skill = load_skill_file(skill_file)
        assert skill.meta.description == ""


# ---------------------------------------------------------------------------
# SkillRegistry tests
# ---------------------------------------------------------------------------


class TestSkillRegistry:
    def test_load_with_no_args_loads_builtin_skills(self, tmp_path, monkeypatch):
        # Patch _BUILTIN_SKILLS_DIR to a tmp dir with known skill files so the
        # test does not depend on which files happen to exist in the real skills/.
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        for trigger in ("review", "commit", "explain"):
            (builtin / f"{trigger}.md").write_text(
                f"---\nname: {trigger.title()}\ntrigger: {trigger}\n---\nTemplate.\n"
            )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        skills = registry.list_all()
        triggers = {s.meta.trigger for s in skills}
        assert "review" in triggers
        assert "commit" in triggers
        assert "explain" in triggers

    def test_load_with_project_path_loads_project_level_skills(self, tmp_path):
        # Create a project-local skill that overrides built-in review
        skill_dir = tmp_path / ".nextme" / "skills"
        skill_dir.mkdir(parents=True)
        override_file = skill_dir / "review.md"
        override_file.write_text(
            "---\nname: Custom Review\ntrigger: review\ndescription: Override\n---\n\nCustom template.\n"
        )

        registry = SkillRegistry()
        registry.load(project_path=tmp_path)

        skill = registry.get("review")
        assert skill is not None
        assert skill.meta.name == "Custom Review"

    def test_get_returns_skill_by_trigger(self, tmp_path, monkeypatch):
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "review.md").write_text(
            "---\nname: Review\ntrigger: review\n---\nTemplate.\n"
        )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        skill = registry.get("review")
        assert skill is not None
        assert skill.meta.trigger == "review"

    def test_get_returns_none_for_unknown_trigger(self):
        registry = SkillRegistry()
        registry.load()

        skill = registry.get("this_trigger_does_not_exist_xyz")
        assert skill is None

    def test_list_all_returns_all_registered_skills(self, tmp_path, monkeypatch):
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        for trigger in ("review", "commit", "explain"):
            (builtin / f"{trigger}.md").write_text(
                f"---\nname: {trigger.title()}\ntrigger: {trigger}\n---\nTemplate.\n"
            )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        skills = registry.list_all()
        assert isinstance(skills, list)
        assert len(skills) >= 3

    def test_non_existent_directory_silently_skipped(self, tmp_path, caplog, monkeypatch):
        # Use a project path with no .nextme/skills dir.
        # Patch _BUILTIN_SKILLS_DIR to empty dir so test is self-contained.
        empty = tmp_path / "empty_builtin"
        empty.mkdir()
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", empty)

        non_existent_project = tmp_path / "no_project_here"
        registry = SkillRegistry()
        with caplog.at_level(logging.DEBUG):
            registry.load(project_path=non_existent_project)
        # Should not raise and may have loaded global skills only
        assert isinstance(registry.list_all(), list)

    def test_load_nested_skill_via_skill_md(self, tmp_path, monkeypatch):
        """Tier-2 NextMe built-in: SKILL.md inside a subdirectory is loaded,
        trigger defaults to the parent directory name."""
        builtin = tmp_path / "builtin"
        skill_dir = builtin / "my-tool"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: My Tool\ndescription: A test tool\n---\nTemplate.\n"
        )
        # Add a doc file that must be ignored.
        (skill_dir / "README.md").write_text("# My Tool\nDocumentation.\n")
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        skill = registry.get("my-tool")
        assert skill is not None
        assert skill.meta.name == "My Tool"
        assert skill.meta.trigger == "my-tool"
        assert skill.source == "nextme"

    def test_load_deeply_nested_skill_md(self, tmp_path, monkeypatch):
        """SKILL.md two levels deep (e.g. skills/public/<name>/SKILL.md) is found."""
        builtin = tmp_path / "builtin"
        skill_dir = builtin / "public" / "deep-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: Deep Skill\ndescription: deep\n---\nTemplate.\n"
        )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        skill = registry.get("deep-skill")
        assert skill is not None
        assert skill.meta.trigger == "deep-skill"

    def test_readme_md_inside_subdir_is_not_loaded_as_skill(self, tmp_path, monkeypatch):
        """Non-SKILL.md files inside subdirectories are ignored."""
        builtin = tmp_path / "builtin"
        subdir = builtin / "some-tool"
        subdir.mkdir(parents=True)
        (subdir / "README.md").write_text(
            "---\nname: Readme\ntrigger: readme\n---\nDoc.\n"
        )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        registry = SkillRegistry()
        registry.load()

        assert registry.get("readme") is None
        assert registry.get("some-tool") is None

    def test_project_nested_skill_overrides_builtin(self, tmp_path, monkeypatch):
        """Tier-3 project skill (nested SKILL.md) overrides tier-2 built-in."""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "review.md").write_text(
            "---\nname: Builtin Review\ntrigger: review\n---\nBuiltin.\n"
        )
        monkeypatch.setattr("nextme.skills.registry._BUILTIN_SKILLS_DIR", builtin)

        # Project skill in nested dir.
        proj_skill_dir = tmp_path / "project" / ".nextme" / "skills" / "review"
        proj_skill_dir.mkdir(parents=True)
        (proj_skill_dir / "SKILL.md").write_text(
            "---\nname: Project Review\ndescription: Override\n---\nProject.\n"
        )

        registry = SkillRegistry()
        registry.load(project_path=tmp_path / "project")

        skill = registry.get("review")
        assert skill is not None
        assert skill.meta.name == "Project Review"
        assert skill.source == "project"

    def test_invalid_skill_file_silently_skipped(self, tmp_path, caplog):
        skill_dir = tmp_path / ".nextme" / "skills"
        skill_dir.mkdir(parents=True)

        # Create an invalid skill file (no frontmatter)
        invalid_file = skill_dir / "invalid.md"
        invalid_file.write_text("This is not a valid skill file.")

        registry = SkillRegistry()
        with caplog.at_level(logging.WARNING):
            registry.load(project_path=tmp_path)

        # Should have logged a warning about the invalid file
        assert any("invalid.md" in r.message or "failed to load" in r.message.lower()
                   for r in caplog.records)

    def test_higher_priority_overrides_lower_priority(self, tmp_path):
        skill_dir = tmp_path / ".nextme" / "skills"
        skill_dir.mkdir(parents=True)

        # Project-level skill with same trigger as built-in review
        override_file = skill_dir / "review.md"
        override_file.write_text(
            "---\nname: Override Review\ntrigger: review\n---\n\nOverride template.\n"
        )

        registry = SkillRegistry()
        registry.load(project_path=tmp_path)

        skill = registry.get("review")
        assert skill is not None
        assert skill.meta.name == "Override Review"

    def test_load_clears_previous_skills(self, tmp_path):
        registry = SkillRegistry()
        registry.load()
        first_skills = set(s.meta.trigger for s in registry.list_all())

        # Load again — should be the same
        registry.load()
        second_skills = set(s.meta.trigger for s in registry.list_all())

        assert first_skills == second_skills

    def test_load_with_custom_project_skill(self, tmp_path):
        skill_dir = tmp_path / ".nextme" / "skills"
        skill_dir.mkdir(parents=True)

        custom_file = skill_dir / "myskill.md"
        custom_file.write_text(
            "---\nname: My Custom Skill\ntrigger: myskill\ndescription: A custom skill\n---\n\nDo something: {user_input}\n"
        )

        registry = SkillRegistry()
        registry.load(project_path=tmp_path)

        skill = registry.get("myskill")
        assert skill is not None
        assert skill.meta.name == "My Custom Skill"
        assert skill.meta.description == "A custom skill"


# ---------------------------------------------------------------------------
# SkillInvoker tests
# ---------------------------------------------------------------------------


class TestSkillInvoker:
    def test_build_prompt_substitutes_user_input_and_context(self):
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="Input: {user_input}\nContext: {context}",
        )
        result = invoker.build_prompt(skill, "hello world", "ctx text")
        assert result == "Input: hello world\nContext: ctx text"

    def test_build_prompt_default_context_is_empty(self):
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="Input: {user_input}\nContext: {context}",
        )
        result = invoker.build_prompt(skill, "my input")
        assert result == "Input: my input\nContext: "

    def test_build_prompt_appends_user_input_when_no_placeholder(self):
        # Claude global skills have no {user_input} placeholder; user's request
        # should be appended so the agent knows what to do.
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="This template has no placeholders at all.",
        )
        result = invoker.build_prompt(skill, "some input", "some context")
        assert result == "This template has no placeholders at all.\n\nUser request: some input"

    def test_build_prompt_no_append_when_user_input_empty(self):
        # If user_input is empty, nothing should be appended even without placeholder.
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="This template has no placeholders at all.",
        )
        result = invoker.build_prompt(skill, "", "some context")
        assert result == "This template has no placeholders at all."

    def test_build_prompt_replaces_all_occurrences(self):
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="{user_input} and {user_input} again",
        )
        result = invoker.build_prompt(skill, "echo")
        assert result == "echo and echo again"

    def test_build_prompt_with_empty_user_input(self):
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="Input: '{user_input}'",
        )
        result = invoker.build_prompt(skill, "")
        assert result == "Input: ''"

    def test_build_prompt_with_multiline_template(self):
        invoker = SkillInvoker()
        skill = Skill(
            meta=SkillMeta(name="Test", trigger="test"),
            template="Line 1: {user_input}\nLine 2: {context}\nLine 3: done",
        )
        result = invoker.build_prompt(skill, "input_text", "context_text")
        assert result == "Line 1: input_text\nLine 2: context_text\nLine 3: done"
