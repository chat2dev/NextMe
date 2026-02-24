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

        # Order: built-in (lowest) → user-global → project-local (highest)
        directories: list[Path] = [
            _BUILTIN_SKILLS_DIR,
            _NEXTME_HOME / "skills",
        ]
        if project_path is not None:
            directories.append(project_path / ".nextme" / "skills")

        for directory in directories:
            self._load_directory(directory)

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

    def _load_directory(self, directory: Path) -> None:
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
                skill = load_skill_file(path)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "SkillRegistry: failed to load skill file %r: %s", str(path), exc
                )
                continue

            trigger = skill.meta.trigger
            if trigger in self._skills:
                logger.debug(
                    "SkillRegistry: overriding trigger %r with skill from %s",
                    trigger,
                    path,
                )
            else:
                logger.debug(
                    "SkillRegistry: registered skill %r (trigger=%r) from %s",
                    skill.meta.name,
                    trigger,
                    path,
                )
            self._skills[trigger] = skill

        logger.info(
            "SkillRegistry: %d skill(s) registered after loading %s",
            len(self._skills),
            directory,
        )
