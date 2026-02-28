"""Jinja2 prompt template loader for NextMe.

Load order for each template:
1. ``user_prompts_dir / memory.md``    — user override (default: ~/.nextme/prompts/)
2. ``src/nextme/prompts/memory.md``    — bundled default (via importlib.resources)
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
        user_prompts_dir: Override the user prompts directory (defaults to
            ``~/.nextme/prompts/``).  Primarily used in tests.
    """
    prompts_dir = (
        user_prompts_dir if user_prompts_dir is not None else _NEXTME_HOME / "prompts"
    )
    user_path = prompts_dir / "memory.md"
    if user_path.is_file():
        source = user_path.read_text(encoding="utf-8")
    else:
        source = (
            files("nextme.prompts").joinpath("memory.md").read_text(encoding="utf-8")
        )
    return jinja2.Template(source)
