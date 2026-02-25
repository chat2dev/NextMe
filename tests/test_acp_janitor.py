"""Tests for nextme.acp.janitor — ACPRuntimeRegistry and ACPJanitor."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextme.acp.janitor import ACPJanitor, ACPRuntimeRegistry
from nextme.config.schema import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**kwargs) -> Settings:
    defaults = {"acp_idle_timeout_seconds": 7200}
    defaults.update(kwargs)
    return Settings(**defaults)


def make_mock_runtime(is_running: bool = True, last_access: datetime = None) -> MagicMock:
    """Create a mock ACPRuntime with controllable is_running and last_access."""
    mock = MagicMock()
    mock.is_running = is_running
    mock.last_access = last_access or datetime.now()
    mock.stop = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# ACPRuntimeRegistry tests
# ---------------------------------------------------------------------------


class TestACPRuntimeRegistry:
    def test_get_or_create_creates_new_runtime_for_new_session_id(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()

        runtime = registry.get_or_create(
            session_id="session-1",
            cwd="/tmp",
            settings=settings,
            executor="claude-code-acp",
        )

        assert runtime is not None
        assert "session-1" in registry

    def test_get_or_create_returns_same_runtime_for_same_session_id(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()

        runtime1 = registry.get_or_create(
            session_id="session-x",
            cwd="/tmp",
            settings=settings,
        )
        runtime2 = registry.get_or_create(
            session_id="session-x",
            cwd="/tmp",
            settings=settings,
        )

        assert runtime1 is runtime2

    def test_get_returns_none_for_unknown_session_id(self):
        registry = ACPRuntimeRegistry()
        result = registry.get("nonexistent")
        assert result is None

    def test_get_returns_runtime_for_known_session_id(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()

        created = registry.get_or_create("session-y", cwd="/tmp", settings=settings)
        fetched = registry.get("session-y")

        assert fetched is created

    async def test_remove_removes_and_stops_runtime(self):
        registry = ACPRuntimeRegistry()
        mock_runtime = make_mock_runtime()
        registry._runtimes["session-z"] = mock_runtime

        await registry.remove("session-z")

        assert "session-z" not in registry
        mock_runtime.stop.assert_awaited_once()

    async def test_remove_is_safe_for_unknown_session_id(self):
        registry = ACPRuntimeRegistry()
        # Should not raise
        await registry.remove("does-not-exist")

    async def test_stop_all_stops_all_runtimes_concurrently(self):
        registry = ACPRuntimeRegistry()
        mock1 = make_mock_runtime()
        mock2 = make_mock_runtime()
        registry._runtimes["s1"] = mock1
        registry._runtimes["s2"] = mock2

        await registry.stop_all()

        mock1.stop.assert_awaited_once()
        mock2.stop.assert_awaited_once()
        assert len(registry) == 0

    async def test_stop_all_noop_when_registry_is_empty(self):
        registry = ACPRuntimeRegistry()
        # Should not raise
        await registry.stop_all()
        assert len(registry) == 0

    async def test_stop_all_clears_registry(self):
        registry = ACPRuntimeRegistry()
        registry._runtimes["s1"] = make_mock_runtime()
        registry._runtimes["s2"] = make_mock_runtime()

        await registry.stop_all()

        assert len(registry) == 0

    def test_len_correct_count(self):
        registry = ACPRuntimeRegistry()
        assert len(registry) == 0

        settings = make_settings()
        registry.get_or_create("s1", cwd="/tmp", settings=settings)
        assert len(registry) == 1

        registry.get_or_create("s2", cwd="/tmp", settings=settings)
        assert len(registry) == 2

    def test_contains_correct_membership(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()

        assert "s1" not in registry
        registry.get_or_create("s1", cwd="/tmp", settings=settings)
        assert "s1" in registry
        assert "s2" not in registry

    async def test_stop_all_handles_stop_exception(self):
        """stop_all should not raise even if a runtime's stop() raises."""
        registry = ACPRuntimeRegistry()
        mock_runtime = make_mock_runtime()
        mock_runtime.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        registry._runtimes["s1"] = mock_runtime

        # Should complete without raising
        await registry.stop_all()
        assert len(registry) == 0


# ---------------------------------------------------------------------------
# ACPJanitor._reap_idle tests
# ---------------------------------------------------------------------------


class TestACPJanitorReapIdle:
    async def test_reap_idle_removes_runtimes_not_running(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings(acp_idle_timeout_seconds=3600)
        mock_runtime = make_mock_runtime(is_running=False)
        registry._runtimes["s1"] = mock_runtime
        registry.remove = AsyncMock()

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        registry.remove.assert_awaited_once_with("s1")

    async def test_reap_idle_removes_runtimes_idle_beyond_threshold(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings(acp_idle_timeout_seconds=3600)

        # Runtime has been idle for 3 hours — beyond the 1-hour threshold
        old_access = datetime.now() - timedelta(hours=3)
        mock_runtime = make_mock_runtime(is_running=True, last_access=old_access)
        registry._runtimes["s-old"] = mock_runtime
        registry.remove = AsyncMock()

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        registry.remove.assert_awaited_once_with("s-old")

    async def test_reap_idle_keeps_recently_active_runtimes(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings(acp_idle_timeout_seconds=3600)

        # Runtime accessed 30 seconds ago — well within the 1-hour threshold
        recent_access = datetime.now() - timedelta(seconds=30)
        mock_runtime = make_mock_runtime(is_running=True, last_access=recent_access)
        registry._runtimes["s-fresh"] = mock_runtime
        registry.remove = AsyncMock()

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        registry.remove.assert_not_awaited()

    async def test_reap_idle_noop_on_empty_registry(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()
        registry.remove = AsyncMock()

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        registry.remove.assert_not_awaited()

    async def test_reap_idle_removes_only_stale_runtimes(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings(acp_idle_timeout_seconds=3600)

        old_access = datetime.now() - timedelta(hours=5)
        recent_access = datetime.now() - timedelta(minutes=5)

        stale_runtime = make_mock_runtime(is_running=True, last_access=old_access)
        fresh_runtime = make_mock_runtime(is_running=True, last_access=recent_access)

        registry._runtimes["stale"] = stale_runtime
        registry._runtimes["fresh"] = fresh_runtime

        removed_sessions = []

        async def mock_remove(sid):
            removed_sessions.append(sid)

        registry.remove = mock_remove

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        assert "stale" in removed_sessions
        assert "fresh" not in removed_sessions

    async def test_reap_idle_removes_not_running_regardless_of_last_access(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings(acp_idle_timeout_seconds=3600)

        # Even if last_access is very recent, a stopped runtime should be reaped
        recent_access = datetime.now() - timedelta(seconds=1)
        stopped_runtime = make_mock_runtime(is_running=False, last_access=recent_access)
        registry._runtimes["stopped"] = stopped_runtime
        registry.remove = AsyncMock()

        janitor = ACPJanitor(registry, settings)
        await janitor._reap_idle()

        registry.remove.assert_awaited_once_with("stopped")


# ---------------------------------------------------------------------------
# ACPJanitor.run() tests
# ---------------------------------------------------------------------------


class TestACPJanitorRun:
    async def test_run_can_be_cancelled(self):
        registry = ACPRuntimeRegistry()
        settings = make_settings()
        janitor = ACPJanitor(registry, settings)

        task = asyncio.create_task(janitor.run())
        # Give the task a moment to start the sleep
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_run_calls_reap_idle_after_sleep(self):
        """run() should invoke _reap_idle after each sleep interval."""
        registry = ACPRuntimeRegistry()
        settings = make_settings()
        janitor = ACPJanitor(registry, settings)

        reap_calls = []

        async def mock_reap_idle():
            reap_calls.append(1)
            # After first reap, cancel via exception to escape the loop
            raise asyncio.CancelledError

        janitor._reap_idle = mock_reap_idle

        # Patch sleep so test doesn't actually wait 60 seconds
        async def instant_sleep(_seconds):
            pass

        with patch("nextme.acp.janitor.asyncio.sleep", new=instant_sleep):
            task = asyncio.create_task(janitor.run())
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(reap_calls) >= 1
