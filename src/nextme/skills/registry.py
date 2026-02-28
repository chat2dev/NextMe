"""SkillRegistry — scan and register skill ``.md`` files.

Three-tier discovery (low → high priority; higher tier shadows lower)
---------------------------------------------------------------------
1. **Global** (``source="global"``)
   ``~/.claude/skills/<name>/SKILL.md`` — Claude built-in or installed skills.
   Trigger is taken from the subdirectory name (or frontmatter if present).

2. **NextMe built-in** (``source="nextme"``)
   ``{package_root}/skills/`` — skills bundled with NextMe, including nested
   subdirectories (e.g. ``skills/public/<name>/SKILL.md``).
   Also scans ``~/.nextme/skills/`` for user-installed NextMe skills.

3. **Project** (``source="project"``)
   ``{project_path}/.nextme/skills/`` — project-local skills, including
   nested subdirectories.  Highest priority; overrides all other tiers.

Within each tier, skills are loaded in alphabetical order.  A skill loaded
from a higher-priority tier *shadows* any skill with the same trigger from
a lower-priority tier.

File patterns recognised inside each directory
----------------------------------------------
* **Flat**  ``{dir}/<name>.md``            — trigger defaults to file stem.
* **Nested** ``{dir}/**/<name>/SKILL.md``  — trigger defaults to the
  immediate parent directory name.  Other ``.md`` files inside the
  subdirectory (README, QUICKSTART, …) are ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .loader import Skill, load_skill_file

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()
_CLAUDE_HOME = Path("~/.claude").expanduser()

# Tier-2 built-in skills directory: four levels up from this file
# src/nextme/skills/registry.py → src/nextme/skills → src/nextme → src → project root
_BUILTIN_SKILLS_DIR: Path = Path(__file__).parent.parent.parent.parent / "skills"


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Scan and register skill ``.md`` files from the three skill tiers.

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

    def load(
        self,
        project_path: Path | None = None,
        executors: set[str] | None = None,
    ) -> None:
        """Scan all skill tiers and register skills by trigger.

        Tiers are loaded from *lowest* to *highest* priority so that
        higher-priority skills overwrite lower-priority ones.

        Parameters
        ----------
        project_path:
            Optional project root directory.  When provided,
            ``{project_path}/.nextme/skills/`` is scanned as tier 3.
        executors:
            Set of executor names (e.g. ``{"claude"}``).  When ``None``
            or when the set contains a ``"claude"``-prefixed executor,
            the global tier (``~/.claude/skills/``) is included.
        """
        self._skills.clear()

        # Tier 1: Global — Claude's built-in / installed skills (lowest priority).
        if executors is None or any(e.startswith("claude") for e in executors):
            self._load_directory(_CLAUDE_HOME / "skills", source="global")

        # Tier 2: NextMe built-in (package) + user-installed NextMe skills.
        self._load_directory(_BUILTIN_SKILLS_DIR, source="nextme")
        self._load_directory(_NEXTME_HOME / "skills", source="nextme")

        # Tier 3: Project-local (highest priority).
        if project_path is not None:
            self._load_directory(
                project_path / ".nextme" / "skills", source="project"
            )

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
        """Load skill files from *directory*, including nested subdirectories.

        Two file patterns are recognised:

        * **Flat**: ``{directory}/<name>.md`` — trigger defaults to file stem.
        * **Nested**: ``{directory}/**/<name>/SKILL.md`` — trigger defaults to
          the immediate parent directory name.  Other ``.md`` files inside
          subdirectories are ignored (treated as documentation).

        Non-existent directories are silently skipped.  Files that fail to
        parse are logged as warnings and skipped.
        """
        if not directory.is_dir():
            logger.debug("SkillRegistry: directory not found, skipping: %s", directory)
            return

        # Collect (path, trigger_override) pairs.
        candidates: list[tuple[Path, str]] = []

        # Flat .md files directly in the directory root.
        for path in sorted(directory.glob("*.md")):
            candidates.append((path, path.stem))

        # SKILL.md files in any subdirectory at any depth.
        for path in sorted(directory.rglob("SKILL.md")):
            candidates.append((path, path.parent.name))

        if not candidates:
            logger.debug("SkillRegistry: no skill files in %s", directory)
            return

        count_before = len(self._skills)
        for path, trigger_override in candidates:
            try:
                skill = load_skill_file(
                    path, trigger_override=trigger_override, source=source
                )
            except (ValueError, OSError) as exc:
                logger.warning(
                    "SkillRegistry: failed to load skill %r: %s", str(path), exc
                )
                continue
            self._register(skill, path)

        loaded = len(self._skills) - count_before
        logger.info(
            "SkillRegistry: %d skill(s) loaded from %s [%s]",
            loaded,
            directory,
            source,
        )

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
