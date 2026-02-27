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
from .direct_runtime import DirectClaudeRuntime
from .runtime import ACPRuntime

logger = logging.getLogger(__name__)

_JANITOR_INTERVAL_SECONDS = 60

# Executor names that use the ACP JSON-RPC 2.0 protocol (→ ACPRuntime).
# Any other name is treated as a direct ``claude`` CLI executor (→ DirectClaudeRuntime).
_ACP_EXECUTOR_NAMES: frozenset[str] = frozenset({"claude-code-acp", "cc-acp", "coco"})

_AnyRuntime = ACPRuntime | DirectClaudeRuntime


class ACPRuntimeRegistry:
    """Thread-safe (asyncio-safe) registry of active runtime instances.

    Holds either :class:`ACPRuntime` (cc-acp protocol) or
    :class:`DirectClaudeRuntime` (direct claude CLI) depending on the
    executor name passed to :meth:`get_or_create`.

    Intended to be used as a module-level singleton, though nothing prevents
    creating multiple instances for testing.
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, _AnyRuntime] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get_or_create(
        self,
        session_id: str,
        cwd: str,
        settings: Settings,
        executor: str = "claude",
        executor_args: list[str] | None = None,
    ) -> _AnyRuntime:
        """Return the existing runtime for *session_id*, or create one.

        Selects the runtime implementation based on *executor*:

        * ``"claude-code-acp"`` / ``"cc-acp"`` / ``"coco"`` → :class:`ACPRuntime`
          (JSON-RPC 2.0 over ACP-compatible subprocess).
        * Any other value → :class:`DirectClaudeRuntime`
          (direct ``claude --print --output-format stream-json`` invocation).

        This method is **synchronous** and does *not* start any subprocess.
        Call :meth:`ensure_ready` on the returned runtime before sending prompts.

        Args:
            session_id: Unique bot-level session identifier.
            cwd: Working directory passed to the subprocess.
            settings: Application settings used by the runtime.
            executor: Executor command.  Defaults to ``"claude"``.
            executor_args: Extra arguments appended to *executor* when spawning
                the subprocess (e.g. ``["acp", "serve"]`` for ``coco acp serve``).

        Returns:
            A runtime instance (possibly freshly created).
        """
        if session_id not in self._runtimes:
            args = executor_args or []
            logger.info(
                "ACPRuntimeRegistry: creating %s runtime for session %r (cmd=%r)",
                "ACPRuntime" if executor in _ACP_EXECUTOR_NAMES else "DirectClaudeRuntime",
                session_id,
                [executor, *args] if args else executor,
            )
            runtime: _AnyRuntime
            if executor in _ACP_EXECUTOR_NAMES:
                runtime = ACPRuntime(
                    session_id=session_id,
                    cwd=cwd,
                    settings=settings,
                    executor=executor,
                    executor_args=args,
                )
            else:
                runtime = DirectClaudeRuntime(
                    session_id=session_id,
                    cwd=cwd,
                    settings=settings,
                    executor=executor,
                )
            self._runtimes[session_id] = runtime
        return self._runtimes[session_id]

    def get(self, session_id: str) -> Optional[_AnyRuntime]:
        """Return the runtime for *session_id*, or ``None`` if not registered.

        Args:
            session_id: Unique bot-level session identifier.

        Returns:
            The runtime instance, or ``None``.
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
            if not runtime.is_running and isinstance(runtime, ACPRuntime):
                # Persistent ACP process (cc-acp / coco) died unexpectedly — remove stale entry.
                idle_session_ids.append(session_id)
                continue
            # For DirectClaudeRuntime, is_running=False between tasks is normal
            # (process exits after each response). Only evict on idle timeout.
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
