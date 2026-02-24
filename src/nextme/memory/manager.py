"""Per-user memory manager backed by ~/.nextme/memory/{md5(context_id)}/.

Files per context directory
---------------------------
- user_context.json  — :class:`UserContextMemory`
- personal.json      — :class:`PersonalInfo`
- facts.json         — :class:`FactStore`

Write strategy
--------------
Updates are applied to in-memory caches immediately.  A background
debounce loop flushes dirty contexts to disk every
``settings.memory_debounce_seconds`` (default 30 s).

Atomic write
------------
Each JSON file is written to a sibling temp file first, then renamed
over the target path (POSIX ``rename(2)`` / Windows ``os.replace``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from ..config.schema import Settings
from .schema import Fact, FactStore, PersonalInfo, UserContextMemory

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _md5(text: str) -> str:
    """Return the MD5 hex digest of *text* (used for directory names)."""
    return hashlib.md5(text.encode()).hexdigest()


def _write_json_atomic(path: Path, payload: str) -> None:
    """Write *payload* to *path* atomically via a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".mem_tmp_", suffix=".json")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_safe(path: Path) -> dict:
    """Return parsed JSON dict from *path*, or ``{}`` on any error."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Per-context in-memory record
# ---------------------------------------------------------------------------


class _ContextData(NamedTuple):
    user_context: UserContextMemory
    personal: PersonalInfo
    fact_store: FactStore


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------


class MemoryManager:
    """Manages per-user memory in ``~/.nextme/memory/{md5(context_id)}/``.

    Parameters
    ----------
    settings:
        Application settings; ``memory_debounce_seconds`` controls the
        debounce interval for background disk flushes.
    base_dir:
        Override the default ``~/.nextme/memory`` root.  Useful for tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        base_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._base_dir: Path = base_dir or (_NEXTME_HOME / "memory")
        self._debounce_seconds: float = float(settings.memory_debounce_seconds)

        # In-memory cache: context_id → _ContextData
        self._cache: dict[str, _ContextData] = {}

        # Dirty set: context_ids that need flushing
        self._dirty: set[str] = set()

        # Background debounce task handle
        self._background_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Directory helpers
    # ------------------------------------------------------------------

    def _dir_for(self, context_id: str) -> Path:
        return self._base_dir / _md5(context_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load(
        self, context_id: str
    ) -> tuple[UserContextMemory, PersonalInfo, FactStore]:
        """Load all memory files for *context_id*.

        Returns in-memory cached data if already loaded.  Falls back to
        defaults when files are absent or corrupt.
        """
        if context_id in self._cache:
            d = self._cache[context_id]
            return d.user_context, d.personal, d.fact_store

        dir_path = self._dir_for(context_id)

        user_context = UserContextMemory.model_validate(
            _read_json_safe(dir_path / "user_context.json")
        )
        personal = PersonalInfo.model_validate(
            _read_json_safe(dir_path / "personal.json")
        )
        fact_store = FactStore.model_validate(
            _read_json_safe(dir_path / "facts.json")
        )

        self._cache[context_id] = _ContextData(user_context, personal, fact_store)
        logger.debug("MemoryManager: loaded memory for context %r", context_id)
        return user_context, personal, fact_store

    def get_top_facts(self, context_id: str, n: int = 15) -> list[Fact]:
        """Return the top-*n* facts by confidence for *context_id*.

        Returns an empty list if the context has not been loaded yet.
        """
        data = self._cache.get(context_id)
        if data is None:
            return []
        sorted_facts = sorted(
            data.fact_store.facts, key=lambda f: f.confidence, reverse=True
        )
        return sorted_facts[:n]

    def add_fact(self, context_id: str, fact: Fact) -> None:
        """Append *fact* to the in-memory store and mark context dirty.

        If the context has not been loaded, the fact is silently dropped
        (callers should :meth:`load` first).
        """
        data = self._cache.get(context_id)
        if data is None:
            logger.warning(
                "MemoryManager.add_fact: context %r not loaded; skipping", context_id
            )
            return
        data.fact_store.facts.append(fact)
        self._dirty.add(context_id)

    def update_user_context(self, context_id: str, ctx: UserContextMemory) -> None:
        """Replace the in-memory UserContextMemory and mark context dirty."""
        data = self._cache.get(context_id)
        if data is None:
            logger.warning(
                "MemoryManager.update_user_context: context %r not loaded; skipping",
                context_id,
            )
            return
        self._cache[context_id] = _ContextData(ctx, data.personal, data.fact_store)
        self._dirty.add(context_id)

    def update_personal_info(self, context_id: str, personal: PersonalInfo) -> None:
        """Replace the in-memory PersonalInfo and mark context dirty."""
        data = self._cache.get(context_id)
        if data is None:
            logger.warning(
                "MemoryManager.update_personal_info: context %r not loaded; skipping",
                context_id,
            )
            return
        self._cache[context_id] = _ContextData(data.user_context, personal, data.fact_store)
        self._dirty.add(context_id)

    async def flush_all(self) -> None:
        """Force write all dirty contexts to disk.

        Iterates over all dirty context ids and writes their three files
        atomically.  Errors for individual contexts are logged but do not
        abort flushing the remaining ones.
        """
        if not self._dirty:
            return

        dirty_ids = list(self._dirty)
        self._dirty.clear()

        for context_id in dirty_ids:
            data = self._cache.get(context_id)
            if data is None:
                continue
            try:
                self._flush_context(context_id, data)
            except Exception:
                logger.exception(
                    "MemoryManager: error flushing context %r", context_id
                )
                # Re-mark dirty so it is retried on the next flush cycle.
                self._dirty.add(context_id)

    # ------------------------------------------------------------------
    # Background debounce loop
    # ------------------------------------------------------------------

    async def start_debounce_loop(self) -> None:
        """Start the background task that flushes dirty contexts periodically.

        Calling this more than once is safe — subsequent calls are ignored
        when the background task is already running.
        """
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = asyncio.get_event_loop().create_task(
            self._debounce_loop(),
            name="memory-manager-debounce",
        )

    async def stop(self) -> None:
        """Cancel the background loop and flush all dirty contexts."""
        if self._background_task is not None and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            self._background_task = None

        await self.flush_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_context(self, context_id: str, data: _ContextData) -> None:
        """Write the three memory files for *context_id* to disk."""
        dir_path = self._dir_for(context_id)
        _write_json_atomic(
            dir_path / "user_context.json",
            data.user_context.model_dump_json(indent=2),
        )
        _write_json_atomic(
            dir_path / "personal.json",
            data.personal.model_dump_json(indent=2),
        )
        _write_json_atomic(
            dir_path / "facts.json",
            data.fact_store.model_dump_json(indent=2),
        )
        logger.debug("MemoryManager: flushed memory for context %r", context_id)

    async def _debounce_loop(self) -> None:
        """Background coroutine: flush dirty contexts every *debounce_seconds*."""
        try:
            while True:
                await asyncio.sleep(self._debounce_seconds)
                if self._dirty:
                    await self.flush_all()
        except asyncio.CancelledError:
            raise
