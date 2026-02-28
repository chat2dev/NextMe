"""Comprehensive tests for nextme.memory.schema and nextme.memory.manager."""

from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from nextme.config.schema import Settings
from nextme.memory.schema import Fact, FactStore, UserContextMemory, PersonalInfo
from nextme.memory.manager import MemoryManager, _md5, _write_json_atomic, _read_json_safe


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestFact:
    def test_creates_with_defaults(self):
        fact = Fact(text="User prefers dark mode")
        assert fact.text == "User prefers dark mode"
        assert fact.confidence == 0.9
        assert fact.source == "conversation"
        assert fact.created_at is not None

    def test_creates_with_explicit_values(self):
        fact = Fact(text="User is in UTC+8", confidence=0.7, source="profile")
        assert fact.confidence == 0.7
        assert fact.source == "profile"


class TestFactStore:
    def test_creates_with_empty_facts_list(self):
        store = FactStore()
        assert store.facts == []
        assert isinstance(store.facts, list)

    def test_creates_with_facts(self):
        fact = Fact(text="Some fact")
        store = FactStore(facts=[fact])
        assert len(store.facts) == 1
        assert store.facts[0].text == "Some fact"


class TestUserContextMemory:
    def test_defaults(self):
        ctx = UserContextMemory()
        assert ctx.preferred_language == "zh"
        assert ctx.communication_style == ""
        assert ctx.notes == ""
        assert ctx.updated_at is not None

    def test_creates_with_custom_values(self):
        ctx = UserContextMemory(preferred_language="en", communication_style="formal")
        assert ctx.preferred_language == "en"
        assert ctx.communication_style == "formal"


class TestPersonalInfo:
    def test_defaults_all_empty_strings(self):
        info = PersonalInfo()
        assert info.name == ""
        assert info.timezone == ""
        assert info.role == ""
        assert info.updated_at is not None

    def test_creates_with_values(self):
        info = PersonalInfo(name="Alice", timezone="UTC+8", role="engineer")
        assert info.name == "Alice"
        assert info.timezone == "UTC+8"
        assert info.role == "engineer"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestMd5:
    def test_returns_correct_hex_digest(self):
        text = "hello"
        expected = hashlib.md5(text.encode()).hexdigest()
        assert _md5(text) == expected

    def test_returns_hex_string(self):
        result = _md5("some_context_id")
        assert isinstance(result, str)
        assert len(result) == 32
        # Should be hex characters only
        int(result, 16)

    def test_different_inputs_different_digests(self):
        assert _md5("ctx1") != _md5("ctx2")

    def test_same_input_same_output(self):
        assert _md5("abc") == _md5("abc")


class TestWriteJsonAtomic:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "output.json"
        payload = '{"key": "value"}'
        _write_json_atomic(target, payload)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == payload

    def test_file_exists_after_write(self, tmp_path):
        target = tmp_path / "subdir" / "data.json"
        _write_json_atomic(target, '{}')
        assert target.is_file()

    def test_content_matches(self, tmp_path):
        target = tmp_path / "test.json"
        payload = json.dumps({"hello": "world", "num": 42})
        _write_json_atomic(target, payload)
        loaded = json.loads(target.read_text())
        assert loaded == {"hello": "world", "num": 42}

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "file.json"
        _write_json_atomic(target, '{}')
        assert target.is_file()

    def test_cleans_up_temp_file_on_failure(self, tmp_path):
        target = tmp_path / "target.json"

        with patch("nextme.memory.manager.os.replace", side_effect=OSError("mock replace failure")):
            with pytest.raises(OSError, match="mock replace failure"):
                _write_json_atomic(target, '{"key": "val"}')

        # No temp files should remain
        tmp_files = list(tmp_path.glob(".mem_tmp_*.json"))
        assert tmp_files == []


class TestReadJsonSafe:
    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        result = _read_json_safe(missing)
        assert result == {}

    def test_returns_empty_dict_for_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("this is not json", encoding="utf-8")
        result = _read_json_safe(bad_file)
        assert result == {}

    def test_returns_correct_dict_for_valid_file(self, tmp_path):
        valid_file = tmp_path / "valid.json"
        data = {"name": "Alice", "value": 42}
        valid_file.write_text(json.dumps(data), encoding="utf-8")
        result = _read_json_safe(valid_file)
        assert result == data

    def test_returns_empty_dict_for_truncated_json(self, tmp_path):
        bad_file = tmp_path / "truncated.json"
        bad_file.write_text('{"incomplete":', encoding="utf-8")
        result = _read_json_safe(bad_file)
        assert result == {}


# ---------------------------------------------------------------------------
# MemoryManager tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_mgr(tmp_path):
    settings = Settings()
    return MemoryManager(settings, base_dir=tmp_path)


class TestMemoryManagerLoad:
    async def test_returns_defaults_when_no_files_exist(self, mem_mgr):
        ctx, personal, facts = await mem_mgr.load("user123")
        assert isinstance(ctx, UserContextMemory)
        assert isinstance(personal, PersonalInfo)
        assert isinstance(facts, FactStore)
        assert ctx.preferred_language == "zh"
        assert personal.name == ""
        assert facts.facts == []

    async def test_returns_cached_data_on_second_call(self, mem_mgr):
        ctx1, personal1, facts1 = await mem_mgr.load("user123")
        # Modify the cache directly to confirm second call returns cache
        cached = mem_mgr._cache["user123"]
        new_ctx = UserContextMemory(preferred_language="en")
        from nextme.memory.manager import _ContextData
        mem_mgr._cache["user123"] = _ContextData(new_ctx, cached.personal, cached.fact_store)

        ctx2, _, _ = await mem_mgr.load("user123")
        assert ctx2.preferred_language == "en"

    async def test_reads_existing_files_correctly(self, tmp_path):
        settings = Settings()
        mgr = MemoryManager(settings, base_dir=tmp_path)

        context_id = "testuser"
        ctx_dir = tmp_path / _md5(context_id)
        ctx_dir.mkdir(parents=True)

        user_ctx_data = {
            "preferred_language": "en",
            "communication_style": "casual",
            "notes": "test notes",
            "updated_at": "2024-01-01T00:00:00",
        }
        personal_data = {
            "name": "Bob",
            "timezone": "UTC",
            "role": "developer",
            "updated_at": "2024-01-01T00:00:00",
        }
        facts_data = {
            "facts": [
                {
                    "text": "User likes Python",
                    "confidence": 0.95,
                    "created_at": "2024-01-01T00:00:00",
                    "source": "conversation",
                }
            ]
        }

        (ctx_dir / "user_context.json").write_text(json.dumps(user_ctx_data))
        (ctx_dir / "personal.json").write_text(json.dumps(personal_data))
        (ctx_dir / "facts.json").write_text(json.dumps(facts_data))

        ctx, personal, facts = await mgr.load(context_id)
        assert ctx.preferred_language == "en"
        assert ctx.communication_style == "casual"
        assert personal.name == "Bob"
        assert personal.timezone == "UTC"
        assert len(facts.facts) == 1
        assert facts.facts[0].text == "User likes Python"
        assert facts.facts[0].confidence == 0.95


class TestMemoryManagerGetTopFacts:
    def test_returns_empty_list_when_context_not_loaded(self, mem_mgr):
        result = mem_mgr.get_top_facts("not_loaded_user")
        assert result == []

    async def test_returns_facts_sorted_by_confidence(self, mem_mgr):
        await mem_mgr.load("user123")
        fact_high = Fact(text="use uv as package manager", confidence=0.95)
        fact_low = Fact(text="coverage threshold is 85 percent", confidence=0.5)
        fact_mid = Fact(text="python version required is 3.12 or newer", confidence=0.75)
        mem_mgr.add_fact("user123", fact_low)
        mem_mgr.add_fact("user123", fact_high)
        mem_mgr.add_fact("user123", fact_mid)

        result = mem_mgr.get_top_facts("user123")
        assert len(result) == 3
        assert result[0].confidence == 0.95
        assert result[1].confidence == 0.75
        assert result[2].confidence == 0.5

    async def test_returns_top_n_facts(self, mem_mgr):
        await mem_mgr.load("user123")
        for i in range(10):
            mem_mgr.add_fact("user123", Fact(text=f"Fact {i}", confidence=float(i) / 10))

        result = mem_mgr.get_top_facts("user123", n=3)
        assert len(result) == 3
        # Should be the three highest confidence ones
        assert result[0].confidence == 0.9
        assert result[1].confidence == 0.8
        assert result[2].confidence == 0.7


class TestMemoryManagerAddFact:
    async def test_appends_fact_and_marks_dirty(self, mem_mgr):
        await mem_mgr.load("user123")
        fact = Fact(text="User prefers dark mode")
        mem_mgr.add_fact("user123", fact)

        cached = mem_mgr._cache["user123"]
        assert len(cached.fact_store.facts) == 1
        assert cached.fact_store.facts[0].text == "User prefers dark mode"
        assert "user123" in mem_mgr._dirty

    def test_no_op_when_context_not_loaded(self, mem_mgr, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            fact = Fact(text="Should be dropped")
            mem_mgr.add_fact("not_loaded", fact)

        assert "not_loaded" not in mem_mgr._cache
        assert "not_loaded" not in mem_mgr._dirty
        # Warning should have been logged
        assert any("not loaded" in record.message.lower() or "skipping" in record.message.lower()
                   for record in caplog.records)


class TestMemoryManagerUpdateUserContext:
    async def test_updates_cache_and_marks_dirty(self, mem_mgr):
        await mem_mgr.load("user123")
        new_ctx = UserContextMemory(preferred_language="en", communication_style="formal")
        mem_mgr.update_user_context("user123", new_ctx)

        cached = mem_mgr._cache["user123"]
        assert cached.user_context.preferred_language == "en"
        assert cached.user_context.communication_style == "formal"
        assert "user123" in mem_mgr._dirty

    def test_no_op_when_context_not_loaded(self, mem_mgr, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            new_ctx = UserContextMemory(preferred_language="en")
            mem_mgr.update_user_context("unknown_user", new_ctx)

        assert "unknown_user" not in mem_mgr._cache


class TestMemoryManagerUpdatePersonalInfo:
    async def test_updates_cache_and_marks_dirty(self, mem_mgr):
        await mem_mgr.load("user123")
        new_personal = PersonalInfo(name="Alice", timezone="UTC+8", role="engineer")
        mem_mgr.update_personal_info("user123", new_personal)

        cached = mem_mgr._cache["user123"]
        assert cached.personal.name == "Alice"
        assert cached.personal.timezone == "UTC+8"
        assert "user123" in mem_mgr._dirty

    def test_no_op_when_context_not_loaded(self, mem_mgr, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            new_personal = PersonalInfo(name="Bob")
            mem_mgr.update_personal_info("unknown_user", new_personal)

        assert "unknown_user" not in mem_mgr._cache


class TestMemoryManagerFlushAll:
    async def test_writes_files_to_disk_and_clears_dirty(self, mem_mgr, tmp_path):
        await mem_mgr.load("user123")
        fact = Fact(text="Test fact")
        mem_mgr.add_fact("user123", fact)
        assert "user123" in mem_mgr._dirty

        await mem_mgr.flush_all()

        # Dirty set should be cleared
        assert "user123" not in mem_mgr._dirty

        # Files should exist on disk
        ctx_dir = tmp_path / _md5("user123")
        assert (ctx_dir / "user_context.json").is_file()
        assert (ctx_dir / "personal.json").is_file()
        assert (ctx_dir / "facts.json").is_file()

        # Content should be correct
        facts_data = json.loads((ctx_dir / "facts.json").read_text())
        assert len(facts_data["facts"]) == 1
        assert facts_data["facts"][0]["text"] == "Test fact"

    async def test_no_op_when_nothing_dirty(self, mem_mgr, tmp_path):
        # Nothing loaded or dirty
        await mem_mgr.flush_all()
        # No files should have been created
        assert list(tmp_path.iterdir()) == []

    async def test_flushes_multiple_contexts(self, mem_mgr, tmp_path):
        await mem_mgr.load("user1")
        await mem_mgr.load("user2")
        mem_mgr.add_fact("user1", Fact(text="Fact for user1"))
        mem_mgr.add_fact("user2", Fact(text="Fact for user2"))

        await mem_mgr.flush_all()

        assert mem_mgr._dirty == set()
        assert (tmp_path / _md5("user1") / "facts.json").is_file()
        assert (tmp_path / _md5("user2") / "facts.json").is_file()


class TestMemoryManagerDebounceLoop:
    async def test_start_debounce_loop_creates_background_task(self, mem_mgr):
        await mem_mgr.start_debounce_loop()
        assert mem_mgr._background_task is not None
        assert not mem_mgr._background_task.done()
        # Cleanup
        await mem_mgr.stop()

    async def test_start_debounce_loop_idempotent(self, mem_mgr):
        await mem_mgr.start_debounce_loop()
        task1 = mem_mgr._background_task

        # Second call should be ignored
        await mem_mgr.start_debounce_loop()
        task2 = mem_mgr._background_task

        assert task1 is task2
        # Cleanup
        await mem_mgr.stop()

    async def test_stop_cancels_background_task_and_flushes(self, mem_mgr, tmp_path):
        await mem_mgr.start_debounce_loop()
        assert mem_mgr._background_task is not None

        # Load and dirty a context
        await mem_mgr.load("user123")
        mem_mgr.add_fact("user123", Fact(text="Pending fact"))
        assert "user123" in mem_mgr._dirty

        await mem_mgr.stop()

        # Task should be done/cancelled
        assert mem_mgr._background_task is None or mem_mgr._background_task.done()

        # Dirty context should have been flushed
        assert "user123" not in mem_mgr._dirty
        ctx_dir = tmp_path / _md5("user123")
        assert (ctx_dir / "facts.json").is_file()
