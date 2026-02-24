"""PathLockRegistry — one asyncio.Lock per physical filesystem path.

Prevents two sessions from writing to the same project directory simultaneously.
The registry is a global singleton; obtain it via ``PathLockRegistry.get_instance()``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar, Optional

logger = logging.getLogger(__name__)


class PathLockRegistry:
    """Global singleton: one :class:`asyncio.Lock` per physical filesystem path.

    Prevents two sessions from writing to the same project simultaneously.

    Usage::

        registry = PathLockRegistry.get_instance()
        async with registry.get("/home/user/myproject"):
            # exclusive access to that directory
            ...
    """

    _instance: ClassVar[Optional[PathLockRegistry]] = None

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> PathLockRegistry:
        """Return the global singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
            logger.debug("PathLockRegistry: singleton created")
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, path: str | Path) -> asyncio.Lock:
        """Return the lock for *path*, creating it on first use.

        The path is resolved to its absolute canonical form so that
        ``"./foo"`` and ``"/abs/foo"`` map to the same lock.

        Args:
            path: A filesystem path (relative or absolute).

        Returns:
            An :class:`asyncio.Lock` exclusive to that path.
        """
        canonical = str(Path(path).expanduser().resolve())
        if canonical not in self._locks:
            logger.debug("PathLockRegistry: creating lock for %r", canonical)
            self._locks[canonical] = asyncio.Lock()
        return self._locks[canonical]
