"""SkillRegistry — scan and register skill ``.md`` files.

Discovery priority (high to low)
---------------------------------
1. ``{project_path}/.nextme/skills/*.md``  — project-local overrides
2. ``~/.nextme/skills/*.md``               — user-global skills
3. ``{package_root}/skills/*.md``          — built-in skills bundled with nextme

A skill loaded from a higher-priority directory *shadows* any skill with the
same trigger from a lower-priority directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .loader import Skill, load_skill_file

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()
_CLAUDE_HOME = Path("~/.claude").expanduser()

# Built-in skills directory: go up four levels from this file
# (src/nextme/skills/registry.py → src/nextme/skills → src/nextme → src → project root)
# then into skills/.
_BUILTIN_SKILLS_DIR: Path = Path(__file__).parent.parent.parent.parent / "skills"


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Scan and register skill ``.md`` files from multiple directories.

    Usage::

        registry = SkillRegistry()
        registry.load(project_path=Path("/my/project"))

        skill = registry.get("review")
        if skill:
            print(skill.meta.description)
    """

    def __init__(self) -> None:
        # Maps trigger → Skill.
        self._skills: dict[str, Skill] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, project_path: Path | None = None) -> None:
        """Scan all skill directories and register skills by trigger.

        Directories are scanned in order from *lowest* to *highest*
        priority so that higher-priority skills overwrite lower-priority
        ones in the registry.

        Parameters
        ----------
        project_path:
            Optional project root directory.  When provided, the
            ``.nextme/skills/`` subdirectory within it is also scanned.
        """
        self._skills.clear()

        # Order: built-in (lowest) → claude-global → nextme-global → project-local (highest)
        directories: list[tuple[Path, str]] = [
            (_BUILTIN_SKILLS_DIR, "builtin"),
            (_NEXTME_HOME / "skills", "nextme"),
        ]
        if project_path is not None:
            directories.append((project_path / ".nextme" / "skills", "project"))

        for directory, source in directories:
            self._load_directory(directory, source=source)

        # Claude global skills live in subdirectories: ~/.claude/skills/<name>/SKILL.md
        self._load_claude_skills()

    def get(self, trigger: str) -> Optional[Skill]:
        """Return the :class:`Skill` registered for *trigger*, or ``None``.

        Parameters
        ----------
        trigger:
            The trigger word **without** a leading ``/``.
        """
        return self._skills.get(trigger)

    def list_all(self) -> list[Skill]:
        """Return all registered skills (order is not guaranteed)."""
        return list(self._skills.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_directory(self, directory: Path, *, source: str = "") -> None:
        """Load all ``.md`` files from *directory* into the registry.

        Non-existent directories are silently skipped.  Files that fail
        to parse are logged as warnings and skipped.
        """
        if not directory.is_dir():
            logger.debug("SkillRegistry: directory not found, skipping: %s", directory)
            return

        skill_files = sorted(directory.glob("*.md"))
        if not skill_files:
            logger.debug("SkillRegistry: no skill files in %s", directory)
            return

        logger.debug(
            "SkillRegistry: loading %d skill file(s) from %s",
            len(skill_files),
            directory,
        )

        for path in skill_files:
            try:
                skill = load_skill_file(path, source=source)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "SkillRegistry: failed to load skill file %r: %s", str(path), exc
                )
                continue

            self._register(skill, path)

        logger.info(
            "SkillRegistry: %d skill(s) registered after loading %s",
            len(self._skills),
            directory,
        )

    def _load_claude_skills(self) -> None:
        """Load Claude global skills from ``~/.claude/skills/<name>/SKILL.md``.

        Each subdirectory represents one skill; the directory name is used as
        the trigger.  Skills loaded here have source ``"claude"`` and are
        inserted between built-in and nextme-global in priority, so they can
        be overridden by user / project skills.
        """
        claude_skills_dir = _CLAUDE_HOME / "skills"
        if not claude_skills_dir.is_dir():
            logger.debug("SkillRegistry: Claude skills dir not found: %s", claude_skills_dir)
            return

        loaded = 0
        for skill_dir in sorted(claude_skills_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            trigger = skill_dir.name
            try:
                skill = load_skill_file(
                    skill_file,
                    trigger_override=trigger,
                    source="claude",
                )
            except (ValueError, OSError) as exc:
                logger.warning(
                    "SkillRegistry: failed to load Claude skill %r: %s", trigger, exc
                )
                continue
            self._register(skill, skill_file)
            loaded += 1

        if loaded:
            logger.info("SkillRegistry: %d Claude global skill(s) loaded", loaded)

    def _register(self, skill: "Skill", path: Path) -> None:
        """Insert *skill* into the registry, logging overrides."""
        trigger = skill.meta.trigger
        if trigger in self._skills:
            logger.debug(
                "SkillRegistry: overriding trigger %r (source=%r) with skill from %s",
                trigger,
                skill.source,
                path,
            )
        else:
            logger.debug(
                "SkillRegistry: registered skill %r (trigger=%r, source=%r) from %s",
                skill.meta.name,
                trigger,
                skill.source,
                path,
            )
        self._skills[trigger] = skill
