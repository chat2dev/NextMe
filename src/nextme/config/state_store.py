"""Persistent, async-safe state store backed by ~/.nextme/state.json.

Features
--------
* Atomic writes via a temp file + ``os.replace`` (POSIX rename(2)).
* Debounced background flush — dirty state is written at most once every
  ``debounce_seconds`` (default: ``Settings.memory_debounce_seconds`` = 30 s).
* Explicit :meth:`flush` for force-writes (e.g. on shutdown).
* Thread-safe in-memory access through a single asyncio event-loop; no
  additional locking needed as long as all callers run on the same loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from .schema import GlobalState, ProjectState, Settings, UserState

_NEXTME_HOME = Path("~/.nextme").expanduser()
_STATE_FILE = _NEXTME_HOME / "state.json"


class StateStore:
    """Async persistent store for :class:`GlobalState`.

    Parameters
    ----------
    settings:
        Application settings; ``memory_debounce_seconds`` controls how often
        dirty state is flushed to disk by the background loop.
    state_path:
        Override the default state-file location.  Useful in tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        state_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._state_path: Path = state_path or _STATE_FILE
        self._debounce_seconds: float = float(settings.memory_debounce_seconds)

        # In-memory representation; None until :meth:`load` is called.
        self._state: GlobalState | None = None

        # Dirty flag: True when in-memory state differs from last-written disk state.
        self._dirty: bool = False

        # Handle to the background debounce task.
        self._background_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load(self) -> GlobalState:
        """Read state from disk (or return defaults if file is absent/corrupt).

        Subsequent calls return the already-loaded in-memory state without
        re-reading the file.
        """
        if self._state is not None:
            return self._state

        self._state = self._read_from_disk()
        self._dirty = False
        return self._state

    def get_user_state(self, context_id: str) -> UserState:
        """Return the :class:`UserState` for *context_id*.

        Creates and stores a blank :class:`UserState` if none exists yet.
        The store must have been loaded via :meth:`load` before calling this.
        """
        state = self._require_loaded()
        if context_id not in state.contexts:
            state.contexts[context_id] = UserState()
            self._dirty = True
        return state.contexts[context_id]

    def set_user_state(self, context_id: str, user_state: UserState) -> None:
        """Update in-memory state for *context_id* and mark the store dirty.

        The background debounce loop (or an explicit :meth:`flush`) will
        eventually persist the change to disk.
        """
        state = self._require_loaded()
        state.contexts[context_id] = user_state
        self._dirty = True

    def set_binding(self, chat_id: str, project_name: str) -> None:
        """Persist a dynamic chat→project binding (set via ``/project bind``).

        Args:
            chat_id: Feishu chat identifier.
            project_name: Name of the project to bind this chat to.
        """
        state = self._require_loaded()
        state.bindings[chat_id] = project_name
        self._dirty = True

    def remove_binding(self, chat_id: str) -> None:
        """Remove a dynamic binding for *chat_id*, if present."""
        state = self._require_loaded()
        if chat_id in state.bindings:
            state.bindings.pop(chat_id)
            self._dirty = True

    def get_all_bindings(self) -> dict[str, str]:
        """Return a copy of all dynamic chat→project bindings."""
        return dict(self._require_loaded().bindings)

    def save_project_actual_id(
        self, context_id: str, project_name: str, actual_id: str
    ) -> None:
        """Persist the Claude session id for a project (enables --resume on restart).

        Args:
            context_id: The user context id (``chatID:userID``).
            project_name: The project name.
            actual_id: The Claude session UUID to persist.  Pass ``""`` to clear.
        """
        user_state = self.get_user_state(context_id)
        if project_name not in user_state.projects:
            user_state.projects[project_name] = ProjectState()
        user_state.projects[project_name].actual_id = actual_id
        self._dirty = True

    def get_project_actual_id(self, context_id: str, project_name: str) -> str:
        """Return the persisted Claude session id for *project_name*, or ``""`` if none.

        Args:
            context_id: The user context id (``chatID:userID``).
            project_name: The project name.
        """
        state = self._require_loaded()
        user_state = state.contexts.get(context_id)
        if user_state is None:
            return ""
        project_state = user_state.projects.get(project_name)
        return project_state.actual_id if project_state else ""

    def register_thread(self, chat_id: str, thread_root_id: str, project_name: str) -> None:
        """注册一个新话题，幂等（已存在则只更新 last_active_at）。"""
        from .schema import ThreadRecord
        state = self._require_loaded()
        key = f"{chat_id}:{thread_root_id}"
        if key not in state.thread_records:
            state.thread_records[key] = ThreadRecord(
                chat_id=chat_id,
                thread_root_id=thread_root_id,
                project_name=project_name,
            )
            self._dirty = True

    def unregister_thread(self, chat_id: str, thread_root_id: str) -> None:
        """移除话题记录，幂等。"""
        state = self._require_loaded()
        key = f"{chat_id}:{thread_root_id}"
        if key in state.thread_records:
            state.thread_records.pop(key)
            self._dirty = True

    def get_active_thread_count(self, chat_id: str) -> int:
        """返回指定 chat 当前活跃话题数。"""
        state = self._require_loaded()
        return sum(1 for r in state.thread_records.values() if r.chat_id == chat_id)

    def get_threads_for_chat(self, chat_id: str):
        """Return all active ThreadRecord objects for *chat_id*, sorted by created_at."""
        from .schema import ThreadRecord
        state = self._require_loaded()
        records = [r for r in state.thread_records.values() if r.chat_id == chat_id]
        return sorted(records, key=lambda r: r.created_at)

    def get_thread_project(self, chat_id: str, thread_root_id: str) -> str:
        """返回话题关联的 project_name，不存在则返回空串。"""
        state = self._require_loaded()
        record = state.thread_records.get(f"{chat_id}:{thread_root_id}")
        return record.project_name if record else ""

    def touch_thread(self, chat_id: str, thread_root_id: str) -> None:
        """更新话题的 last_active_at 时间戳。"""
        from datetime import datetime
        state = self._require_loaded()
        key = f"{chat_id}:{thread_root_id}"
        if key in state.thread_records:
            state.thread_records[key].last_active_at = datetime.now()
            self._dirty = True

    async def flush(self) -> None:
        """Force-write the current in-memory state to disk atomically.

        Performs an atomic write using a sibling temp file and ``os.replace``,
        so a crash mid-write cannot produce a corrupt state file.

        No-op if the store has not been loaded yet.
        """
        if self._state is None:
            return
        self._write_to_disk(self._state)
        self._dirty = False

    async def start_debounce_loop(self) -> None:
        """Start the background task that flushes dirty state periodically.

        Calling this more than once is safe — subsequent calls are ignored if
        the background task is already running.
        """
        if self._background_task is not None and not self._background_task.done():
            return
        self._background_task = asyncio.get_event_loop().create_task(
            self._debounce_loop(),
            name="state-store-debounce",
        )

    async def stop(self) -> None:
        """Flush immediately and cancel the background debounce task."""
        if self._background_task is not None and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
            self._background_task = None

        # Always flush on stop, even if not dirty, to ensure consistency.
        await self.flush()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_loaded(self) -> GlobalState:
        """Return the loaded state or raise if :meth:`load` was not called."""
        if self._state is None:
            raise RuntimeError(
                "StateStore.load() must be awaited before accessing state."
            )
        return self._state

    def _read_from_disk(self) -> GlobalState:
        """Deserialise state from *self._state_path*.

        Returns a blank :class:`GlobalState` when the file does not exist or
        contains invalid JSON / schema data.
        """
        if not self._state_path.is_file():
            return GlobalState()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return GlobalState.model_validate(raw)
        except (json.JSONDecodeError, ValueError, OSError):
            # Corrupt or unreadable file — start fresh rather than crashing.
            return GlobalState()

    def _write_to_disk(self, state: GlobalState) -> None:
        """Serialise *state* to *self._state_path* via an atomic temp-file rename.

        Steps
        -----
        1. Ensure ``~/.nextme/`` exists.
        2. Write JSON to a sibling temp file in the same directory.
        3. ``os.replace`` (atomic on POSIX/Windows) the temp file over the
           target path.
        """
        target = self._state_path
        target.parent.mkdir(parents=True, exist_ok=True)

        payload = state.model_dump_json(indent=2)

        # Write to a temp file in the same directory so that os.replace is
        # guaranteed to be atomic (same filesystem).
        fd, tmp_path_str = tempfile.mkstemp(
            dir=target.parent,
            prefix=".state_tmp_",
            suffix=".json",
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, target)
        except Exception:
            # Clean up the temp file on failure to avoid leaving debris.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _debounce_loop(self) -> None:
        """Background coroutine: flush every *debounce_seconds* when dirty."""
        try:
            while True:
                await asyncio.sleep(self._debounce_seconds)
                if self._dirty:
                    await self.flush()
        except asyncio.CancelledError:
            # Let the cancellation propagate; :meth:`stop` handles the final flush.
            raise
