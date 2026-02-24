"""LRU message deduplication cache.

Prevents processing duplicate messages that can arrive on WebSocket reconnect.
Uses an OrderedDict as an LRU container: most-recently-seen IDs move to the end;
when capacity is exceeded the oldest entry (front) is evicted.  A separate TTL
sweep removes entries that are older than *ttl_seconds* regardless of capacity.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class MessageDedup:
    """LRU cache: message_id -> timestamp.  Capacity 1000, TTL 5 min.

    Thread-safety note: this class is intentionally NOT thread-safe.  All
    callers in this project run on a single asyncio event loop, so no locking
    is needed.
    """

    def __init__(self, capacity: int = 1000, ttl_seconds: float = 300.0) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        # OrderedDict preserves insertion order; oldest entry is at the front.
        self._cache: OrderedDict[str, float] = OrderedDict()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_duplicate(self, message_id: str) -> bool:
        """Return True if *message_id* was seen recently; mark as seen if not.

        A message is considered "seen" when it was recorded within the last
        *ttl_seconds*.  Expired entries are evicted lazily before the check.
        """
        self._evict_expired()

        now = time.monotonic()

        if message_id in self._cache:
            # Move to end to keep LRU order fresh, update timestamp.
            self._cache.move_to_end(message_id)
            self._cache[message_id] = now
            logger.debug("Duplicate message detected: %s", message_id)
            return True

        # New message — record it.
        self._cache[message_id] = now
        self._cache.move_to_end(message_id)

        # Enforce capacity limit by evicting the oldest entry.
        while len(self._cache) > self._capacity:
            evicted_id, _ = self._cache.popitem(last=False)
            logger.debug("Evicted message from dedup cache (capacity): %s", evicted_id)

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove all entries whose timestamp is older than *ttl_seconds*."""
        if not self._cache:
            return

        cutoff = time.monotonic() - self._ttl
        # Entries are stored in insertion order; the oldest are at the front.
        # We can stop as soon as we hit a non-expired entry.
        expired_ids: list[str] = []
        for message_id, ts in self._cache.items():
            if ts < cutoff:
                expired_ids.append(message_id)
            else:
                break  # Remaining entries are newer.

        for message_id in expired_ids:
            del self._cache[message_id]
            logger.debug("Evicted message from dedup cache (TTL): %s", message_id)
