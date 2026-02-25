"""Tests for nextme.feishu.dedup."""
from __future__ import annotations

import time

import pytest

from nextme.feishu.dedup import MessageDedup


# ---------------------------------------------------------------------------
# Basic is_duplicate behaviour
# ---------------------------------------------------------------------------


class TestIsDuplicateBasic:
    def test_first_call_returns_false(self):
        dedup = MessageDedup()
        assert dedup.is_duplicate("msg-001") is False

    def test_second_call_with_same_id_returns_true(self):
        dedup = MessageDedup()
        dedup.is_duplicate("msg-001")
        assert dedup.is_duplicate("msg-001") is True

    def test_different_ids_return_false(self):
        dedup = MessageDedup()
        assert dedup.is_duplicate("msg-001") is False
        assert dedup.is_duplicate("msg-002") is False
        assert dedup.is_duplicate("msg-003") is False

    def test_repeated_duplicate_detection(self):
        dedup = MessageDedup()
        dedup.is_duplicate("msg-a")
        # All subsequent calls with same id are duplicates
        assert dedup.is_duplicate("msg-a") is True
        assert dedup.is_duplicate("msg-a") is True

    def test_empty_string_id(self):
        dedup = MessageDedup()
        assert dedup.is_duplicate("") is False
        assert dedup.is_duplicate("") is True

    def test_multiple_ids_independent(self):
        dedup = MessageDedup()
        dedup.is_duplicate("a")
        dedup.is_duplicate("b")
        # Both are now in cache
        assert dedup.is_duplicate("a") is True
        assert dedup.is_duplicate("b") is True
        # A new id is still fresh
        assert dedup.is_duplicate("c") is False


# ---------------------------------------------------------------------------
# Capacity eviction
# ---------------------------------------------------------------------------


class TestCapacityEviction:
    def test_oldest_entry_evicted_when_over_capacity(self):
        capacity = 3
        dedup = MessageDedup(capacity=capacity, ttl_seconds=9999.0)
        # Fill to capacity
        dedup.is_duplicate("id-1")
        dedup.is_duplicate("id-2")
        dedup.is_duplicate("id-3")
        # Adding one more should evict the oldest (id-1)
        dedup.is_duplicate("id-4")
        assert len(dedup._cache) == capacity
        assert "id-1" not in dedup._cache

    def test_eviction_allows_fresh_entry_for_evicted_id(self):
        dedup = MessageDedup(capacity=2, ttl_seconds=9999.0)
        dedup.is_duplicate("id-1")
        dedup.is_duplicate("id-2")
        # Push id-1 out
        dedup.is_duplicate("id-3")
        # id-1 was evicted, so it should be treated as new
        assert dedup.is_duplicate("id-1") is False

    def test_capacity_one_always_replaces(self):
        dedup = MessageDedup(capacity=1, ttl_seconds=9999.0)
        dedup.is_duplicate("first")
        dedup.is_duplicate("second")
        assert len(dedup._cache) == 1
        assert "first" not in dedup._cache
        assert "second" in dedup._cache

    def test_cache_never_exceeds_capacity(self):
        capacity = 5
        dedup = MessageDedup(capacity=capacity, ttl_seconds=9999.0)
        for i in range(20):
            dedup.is_duplicate(f"msg-{i}")
        assert len(dedup._cache) <= capacity

    def test_lru_order_preserved_after_revisit(self):
        """Revisiting an id moves it to end (LRU), protecting it from eviction."""
        dedup = MessageDedup(capacity=2, ttl_seconds=9999.0)
        dedup.is_duplicate("old")
        dedup.is_duplicate("new")
        # Re-access "old" to make it most-recently-used
        dedup.is_duplicate("old")  # This is a duplicate, moves to end
        # Now add a third item — "new" should be evicted (it's oldest)
        dedup.is_duplicate("third")
        assert "old" in dedup._cache
        assert "new" not in dedup._cache


# ---------------------------------------------------------------------------
# TTL eviction
# ---------------------------------------------------------------------------


class TestTTLEviction:
    def test_expired_entry_evicted(self, monkeypatch):
        """An entry added in the 'past' is evicted when TTL has elapsed."""
        current_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        dedup = MessageDedup(capacity=100, ttl_seconds=60.0)
        dedup.is_duplicate("msg-old")

        # Advance time past TTL
        current_time = 1000.0 + 61.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        # The expired entry should be evicted, so is_duplicate returns False
        assert dedup.is_duplicate("msg-old") is False

    def test_non_expired_entry_not_evicted(self, monkeypatch):
        """An entry within TTL is still considered a duplicate."""
        current_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        dedup = MessageDedup(capacity=100, ttl_seconds=60.0)
        dedup.is_duplicate("msg-fresh")

        # Advance time but stay within TTL
        current_time = 1000.0 + 30.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        assert dedup.is_duplicate("msg-fresh") is True

    def test_mixed_expired_and_fresh_entries(self, monkeypatch):
        current_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        dedup = MessageDedup(capacity=100, ttl_seconds=60.0)
        dedup.is_duplicate("old-msg")

        # Advance time: old-msg is about to expire
        current_time = 1050.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)
        dedup.is_duplicate("new-msg")

        # Advance past TTL of old-msg but not new-msg
        current_time = 1062.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        assert dedup.is_duplicate("old-msg") is False
        assert dedup.is_duplicate("new-msg") is True

    def test_evict_expired_stops_at_first_fresh_entry(self, monkeypatch):
        """_evict_expired() stops scanning when it hits a non-expired entry."""
        current_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        dedup = MessageDedup(capacity=100, ttl_seconds=60.0)
        dedup.is_duplicate("expired-1")
        dedup.is_duplicate("expired-2")

        current_time = 1010.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)
        dedup.is_duplicate("fresh-1")
        dedup.is_duplicate("fresh-2")

        # Advance past TTL of the first two
        current_time = 1065.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)
        dedup._evict_expired()

        assert "expired-1" not in dedup._cache
        assert "expired-2" not in dedup._cache
        assert "fresh-1" in dedup._cache
        assert "fresh-2" in dedup._cache

    def test_after_ttl_eviction_id_can_be_added_fresh(self, monkeypatch):
        """After TTL eviction, a previously-seen id returns False (new entry)."""
        current_time = 2000.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        dedup = MessageDedup(capacity=100, ttl_seconds=30.0)
        dedup.is_duplicate("recycled-id")
        assert dedup.is_duplicate("recycled-id") is True

        # Fast-forward past TTL
        current_time = 2031.0
        monkeypatch.setattr(time, "monotonic", lambda: current_time)

        # Now should be treated as a fresh entry
        result = dedup.is_duplicate("recycled-id")
        assert result is False


# ---------------------------------------------------------------------------
# Empty cache edge cases
# ---------------------------------------------------------------------------


class TestEmptyCache:
    def test_evict_expired_on_empty_cache_does_nothing(self):
        dedup = MessageDedup()
        # Should not raise
        dedup._evict_expired()
        assert len(dedup._cache) == 0

    def test_is_duplicate_on_empty_cache_returns_false(self):
        dedup = MessageDedup()
        assert dedup.is_duplicate("any-id") is False

    def test_cache_starts_empty(self):
        dedup = MessageDedup()
        assert len(dedup._cache) == 0
