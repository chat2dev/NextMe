"""ACPJanitor and ACPRuntimeRegistry.

ACPRuntimeRegistry
------------------
Global singleton that maps ``session_id`` → :class:`~nextme.acp.runtime.ACPRuntime`.
Use :meth:`ACPRuntimeRegistry.get_or_create` to obtain a runtime for a session.

ACPJanitor
----------
Background coroutine that periodically checks all runtimes and stops any that
have been idle longer than ``settings.acp_idle_timeout_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config.schema import Settings
from .runtime import ACPRuntime

logger = logging.getLogger(__name__)

_JANITOR_INTERVAL_SECONDS = 60


class ACPRuntimeRegistry:
    """Thread-safe (asyncio-safe) registry of active :class:`ACPRuntime` instances.

    Intended to be used as a module-level singleton, though nothing prevents
    creating multiple instances for testing.
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, ACPRuntime] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get_or_create(
        self,
        session_id: str,
        cwd: str,
        settings: Settings,
        executor: str = "claude-code-acp",
    ) -> ACPRuntime:
        """Return the existing :class:`ACPRuntime` for *session_id*, or create one.

        This method is **synchronous** and does *not* start the subprocess.
        Call :meth:`~nextme.acp.runtime.ACPRuntime.ensure_ready` on the
        returned runtime before sending prompts.

        Args:
            session_id: Unique bot-level session identifier.
            cwd: Working directory passed to the subprocess.
            settings: Application settings used by the runtime.
            executor: ACP subprocess command (default ``"claude-code-acp"``).

        Returns:
            An :class:`ACPRuntime` instance (possibly freshly created).
        """
        if session_id not in self._runtimes:
            logger.info(
                "ACPRuntimeRegistry: creating runtime for session %r", session_id
            )
            self._runtimes[session_id] = ACPRuntime(
                session_id=session_id,
                cwd=cwd,
                settings=settings,
                executor=executor,
            )
        return self._runtimes[session_id]

    def get(self, session_id: str) -> Optional[ACPRuntime]:
        """Return the runtime for *session_id*, or ``None`` if not registered.

        Args:
            session_id: Unique bot-level session identifier.

        Returns:
            The :class:`ACPRuntime` instance, or ``None``.
        """
        return self._runtimes.get(session_id)

    async def remove(self, session_id: str) -> None:
        """Stop and remove the runtime for *session_id*.

        Safe to call even if *session_id* is not registered.

        Args:
            session_id: Unique bot-level session identifier.
        """
        runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            logger.info(
                "ACPRuntimeRegistry: stopping and removing runtime for session %r",
                session_id,
            )
            await runtime.stop()

    async def stop_all(self) -> None:
        """Stop all registered runtimes and clear the registry.

        Runtimes are stopped concurrently via :func:`asyncio.gather`.
        """
        if not self._runtimes:
            return

        session_ids = list(self._runtimes.keys())
        runtimes = [self._runtimes.pop(sid) for sid in session_ids]

        logger.info(
            "ACPRuntimeRegistry: stopping all %d runtime(s)", len(runtimes)
        )

        results = await asyncio.gather(
            *[r.stop() for r in runtimes], return_exceptions=True
        )

        for sid, result in zip(session_ids, results):
            if isinstance(result, Exception):
                logger.warning(
                    "ACPRuntimeRegistry: error stopping runtime %r: %s", sid, result
                )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._runtimes)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._runtimes


class ACPJanitor:
    """Background coroutine that reaps idle :class:`ACPRuntime` subprocesses.

    The janitor wakes up every :data:`_JANITOR_INTERVAL_SECONDS` seconds and
    stops any runtime whose ``last_access`` is older than
    ``settings.acp_idle_timeout_seconds``.

    Usage::

        janitor = ACPJanitor(registry, settings)
        task = asyncio.create_task(janitor.run())
        # … on shutdown:
        task.cancel()

    Args:
        registry: The :class:`ACPRuntimeRegistry` to inspect.
        settings: Application settings; ``acp_idle_timeout_seconds`` controls
                  the idle threshold.
    """

    def __init__(self, registry: ACPRuntimeRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    async def run(self) -> None:
        """Periodically reap idle runtimes.

        Runs until cancelled.  On each iteration:

        1. Collect all runtimes whose ``last_access`` exceeds the idle timeout.
        2. Stop them concurrently.
        3. Remove them from the registry.
        4. Sleep for :data:`_JANITOR_INTERVAL_SECONDS`.
        """
        logger.info(
            "ACPJanitor: started (interval=%ds, idle_timeout=%ds)",
            _JANITOR_INTERVAL_SECONDS,
            self._settings.acp_idle_timeout_seconds,
        )
        try:
            while True:
                await asyncio.sleep(_JANITOR_INTERVAL_SECONDS)
                await self._reap_idle()
        except asyncio.CancelledError:
            logger.info("ACPJanitor: cancelled, shutting down")
            raise

    async def _reap_idle(self) -> None:
        """Identify and stop runtimes that have exceeded the idle timeout."""
        from datetime import datetime, timedelta

        idle_threshold = timedelta(seconds=self._settings.acp_idle_timeout_seconds)
        now = datetime.now()

        # Collect session ids whose runtimes are idle.
        idle_session_ids: list[str] = []
        for session_id, runtime in list(self._registry._runtimes.items()):
            if not runtime.is_running:
                # Already stopped; remove stale entry.
                idle_session_ids.append(session_id)
                continue
            if now - runtime.last_access >= idle_threshold:
                idle_session_ids.append(session_id)

        if not idle_session_ids:
            return

        logger.info(
            "ACPJanitor: reaping %d idle runtime(s): %s",
            len(idle_session_ids),
            idle_session_ids,
        )

        for session_id in idle_session_ids:
            await self._registry.remove(session_id)
