"""Skill file loader — parse ``.md`` files with YAML frontmatter.

Frontmatter format
------------------
The file must begin with a ``---`` delimiter, followed by simple
``key: value`` lines, closed by another ``---``.  Values are parsed as
plain strings; lists are supported via comma-separated inline notation
(``[item1, item2]``).  No external YAML library is required.

Example::

    ---
    name: Code Review
    trigger: review
    description: Review code from three dimensions
    tools_allowlist: []
    tools_denylist: [bash, write]
    ---

    Your skill prompt template here.

    User request: {user_input}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SkillMeta(BaseModel):
    """Frontmatter metadata for a skill file."""

    name: str
    trigger: str
    description: str = ""
    tools_allowlist: list[str] = Field(default_factory=list)
    tools_denylist: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    """A fully parsed skill: metadata + prompt template body."""

    meta: SkillMeta
    template: str  # the markdown body after frontmatter
    source: str = ""  # "builtin" | "claude" | "nextme" | "project"


# ---------------------------------------------------------------------------
# Internal: minimal frontmatter parser (no external YAML library)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)",
    re.DOTALL,
)

# Match a key: value line (optional leading whitespace, non-empty key).
_KV_LINE_RE = re.compile(r"^(\w[\w_-]*)\s*:\s*(.*)")

# Match an inline list like [] or [item1, item2, "item3"].
_INLINE_LIST_RE = re.compile(r"^\[([^\]]*)\]$")


def _parse_inline_list(value: str) -> list[str]:
    """Parse ``[item1, item2]`` into ``["item1", "item2"]``.

    Returns an empty list for ``[]``.  Each item is stripped of surrounding
    whitespace and optional surrounding quotes (single or double).
    """
    m = _INLINE_LIST_RE.match(value.strip())
    if m is None:
        return []
    inner = m.group(1).strip()
    if not inner:
        return []
    items: list[str] = []
    for part in inner.split(","):
        stripped = part.strip().strip("\"'")
        if stripped:
            items.append(stripped)
    return items


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the YAML-like frontmatter block into a plain ``dict``.

    Keys are lower-cased; values are either strings or lists of strings.
    Lines that do not match ``key: value`` are silently skipped.
    """
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_LINE_RE.match(line)
        if m is None:
            continue
        key = m.group(1).strip().lower()
        value = m.group(2).strip()

        # Detect inline list notation.
        if value.startswith("["):
            result[key] = _parse_inline_list(value)
        else:
            # Strip surrounding quotes from plain string values.
            result[key] = value.strip("\"'")

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_skill_file(
    path: Path,
    *,
    trigger_override: str = "",
    source: str = "",
) -> Skill:
    """Parse a ``.md`` skill file with YAML frontmatter.

    Parameters
    ----------
    path:
        Absolute or relative path to a ``.md`` skill file.
    trigger_override:
        When provided, used as the trigger if the frontmatter does not
        contain a ``trigger`` field (e.g. Claude global skills whose
        trigger is the directory name).
    source:
        Origin label for display purposes: ``"builtin"``, ``"claude"``,
        ``"nextme"``, or ``"project"``.

    Returns
    -------
    Skill
        The parsed skill object.

    Raises
    ------
    ValueError
        If the file has no valid frontmatter block or is missing required
        fields (``name``, ``trigger``) and no override was provided.
    OSError
        If the file cannot be read.
    """
    raw = path.read_text(encoding="utf-8")

    m = _FRONTMATTER_RE.match(raw)
    if m is None:
        raise ValueError(
            f"Skill file {path!r} does not contain a valid frontmatter block. "
            "Expected the file to start with '---'."
        )

    frontmatter_text = m.group(1)
    template = m.group(2).strip()  # strip leading/trailing blank lines

    meta_dict = _parse_frontmatter(frontmatter_text)

    # Apply overrides for fields absent in the frontmatter.
    if trigger_override:
        meta_dict.setdefault("trigger", trigger_override)
        meta_dict.setdefault("name", trigger_override)

    # Validate required fields early with a helpful message.
    for required in ("name", "trigger"):
        if required not in meta_dict or not meta_dict[required]:
            raise ValueError(
                f"Skill file {path!r} is missing required frontmatter field: {required!r}"
            )

    meta = SkillMeta.model_validate(meta_dict)
    return Skill(meta=meta, template=template, source=source)
