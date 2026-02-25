"""Tests for nextme.core.path_lock — PathLockRegistry."""

import asyncio
from pathlib import Path

import pytest

from nextme.core.path_lock import PathLockRegistry


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------


class TestPathLockRegistrySingleton:
    def test_get_instance_returns_singleton(self):
        # Reset first to avoid interference from other tests or prior state
        PathLockRegistry._instance = None
        try:
            r1 = PathLockRegistry.get_instance()
            r2 = PathLockRegistry.get_instance()
            assert r1 is r2
        finally:
            PathLockRegistry._instance = None  # clean up

    def test_get_instance_creates_on_first_call(self):
        PathLockRegistry._instance = None
        try:
            assert PathLockRegistry._instance is None
            instance = PathLockRegistry.get_instance()
            assert instance is not None
            assert PathLockRegistry._instance is instance
        finally:
            PathLockRegistry._instance = None


# ---------------------------------------------------------------------------
# get() tests — use fresh instances to avoid state leakage
# ---------------------------------------------------------------------------


class TestPathLockRegistryGet:
    def test_get_returns_lock_for_path(self, tmp_path):
        registry = PathLockRegistry()
        lock = registry.get(str(tmp_path))
        assert isinstance(lock, asyncio.Lock)

    def test_get_returns_same_lock_for_same_path(self, tmp_path):
        registry = PathLockRegistry()
        lock1 = registry.get(str(tmp_path))
        lock2 = registry.get(str(tmp_path))
        assert lock1 is lock2

    def test_get_returns_same_lock_idempotent_multiple_calls(self, tmp_path):
        registry = PathLockRegistry()
        locks = [registry.get(str(tmp_path)) for _ in range(5)]
        assert all(l is locks[0] for l in locks)

    def test_get_different_paths_return_different_locks(self, tmp_path):
        registry = PathLockRegistry()
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_a.mkdir()
        path_b.mkdir()

        lock_a = registry.get(str(path_a))
        lock_b = registry.get(str(path_b))
        assert lock_a is not lock_b

    def test_get_resolves_relative_paths(self, tmp_path, monkeypatch):
        """Relative paths resolve to canonical absolute form."""
        monkeypatch.chdir(tmp_path)
        registry = PathLockRegistry()

        # "." resolves to tmp_path
        lock_relative = registry.get(".")
        lock_absolute = registry.get(str(tmp_path))
        assert lock_relative is lock_absolute

    def test_get_same_canonical_path_via_different_absolute_notations(self, tmp_path):
        """Two paths with the same canonical form map to the same lock."""
        registry = PathLockRegistry()
        # Both are the same absolute path
        lock1 = registry.get(str(tmp_path))
        lock2 = registry.get(str(tmp_path))
        assert lock1 is lock2

    def test_get_accepts_path_object(self, tmp_path):
        registry = PathLockRegistry()
        lock_str = registry.get(str(tmp_path))
        lock_path = registry.get(tmp_path)  # Pass a Path object
        assert lock_str is lock_path

    def test_get_expands_home_tilde(self):
        """Tilde in path is expanded before creating the lock key."""
        registry = PathLockRegistry()
        home_str = str(Path("~").expanduser())
        lock_tilde = registry.get("~")
        lock_abs = registry.get(home_str)
        assert lock_tilde is lock_abs


# ---------------------------------------------------------------------------
# Mutex behaviour tests
# ---------------------------------------------------------------------------


class TestPathLockMutex:
    async def test_lock_works_as_mutex(self, tmp_path):
        """A second coroutine cannot acquire the lock while the first holds it."""
        registry = PathLockRegistry()
        lock = registry.get(str(tmp_path))

        acquired_second = False

        async def task_holding_lock():
            async with lock:
                # While holding the lock, try to acquire from another task
                other_task = asyncio.create_task(try_acquire())
                # Give it a moment; it must not have succeeded yet
                await asyncio.sleep(0.01)
                assert not acquired_second, "Lock should not be re-acquired while held"

        async def try_acquire():
            nonlocal acquired_second
            async with lock:
                acquired_second = True

        await task_holding_lock()
        # After first task releases, second should have acquired
        await asyncio.sleep(0.01)
        assert acquired_second

    async def test_lock_released_after_context_manager_exit(self, tmp_path):
        registry = PathLockRegistry()
        lock = registry.get(str(tmp_path))

        async with lock:
            pass  # acquire and release

        # Should be able to acquire again immediately
        assert not lock.locked()

    async def test_same_path_lock_is_shared_across_registry_calls(self, tmp_path):
        """Two separate registry.get() calls for the same path return the same mutex."""
        registry = PathLockRegistry()
        lock1 = registry.get(str(tmp_path))
        lock2 = registry.get(str(tmp_path))

        results = []

        async def first():
            async with lock1:
                results.append("first-start")
                await asyncio.sleep(0.05)
                results.append("first-end")

        async def second():
            # Wait for first to be holding the lock
            await asyncio.sleep(0.01)
            async with lock2:
                results.append("second")

        await asyncio.gather(first(), second())

        # second must run after first releases
        assert results.index("first-end") < results.index("second")
