"""Comprehensive tests for nextme.context.manager."""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from nextme.context.manager import ContextManager, _write_bytes_atomic, _write_text_atomic
from nextme.config.schema import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_mgr(tmp_path):
    """ContextManager with small compression threshold for testing."""
    settings = Settings(context_max_bytes=100)
    return ContextManager(settings, base_dir=tmp_path)


@pytest.fixture
def ctx_mgr_large_threshold(tmp_path):
    """ContextManager with very large threshold (never compresses)."""
    settings = Settings(context_max_bytes=10_000_000)
    return ContextManager(settings, base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Atomic write helper tests
# ---------------------------------------------------------------------------


class TestWriteBytesAtomic:
    def test_writes_bytes_to_file(self, tmp_path):
        target = tmp_path / "output.bin"
        data = b"\x00\x01\x02\x03\xff"
        _write_bytes_atomic(target, data)
        assert target.exists()
        assert target.read_bytes() == data

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "file.bin"
        _write_bytes_atomic(target, b"hello")
        assert target.is_file()

    def test_cleans_up_temp_file_on_failure(self, tmp_path):
        target = tmp_path / "target.bin"

        with patch("nextme.context.manager.os.replace", side_effect=OSError("mock failure")):
            with pytest.raises(OSError, match="mock failure"):
                _write_bytes_atomic(target, b"some data")

        # No temp files should remain
        tmp_files = list(tmp_path.glob(".ctx_tmp_*.bin"))
        assert tmp_files == []


class TestWriteTextAtomic:
    def test_writes_text_to_file(self, tmp_path):
        target = tmp_path / "output.txt"
        text = "Hello, world!"
        _write_text_atomic(target, text)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == text

    def test_writes_unicode_content(self, tmp_path):
        target = tmp_path / "unicode.txt"
        text = "你好世界！\nHello World!"
        _write_text_atomic(target, text)
        assert target.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# ContextManager.save() + load() tests
# ---------------------------------------------------------------------------


class TestContextManagerSaveAndLoad:
    async def test_small_content_stored_as_plain_text_and_loaded_back(self, ctx_mgr, tmp_path):
        # Content smaller than 100 bytes threshold
        content = "Short context"
        await ctx_mgr.save("session1", content)

        # Should be stored as plain text
        session_dir = tmp_path / "session1"
        assert (session_dir / "context.txt").is_file()

        # Should load back correctly
        loaded = await ctx_mgr.load("session1")
        assert loaded == content

    async def test_large_content_stored_compressed_and_loaded_back(self, ctx_mgr, tmp_path):
        # Content larger than 100 bytes threshold
        content = "A" * 200  # 200 bytes > 100 threshold
        await ctx_mgr.save("session1", content)

        # Should NOT be stored as plain text
        session_dir = tmp_path / "session1"
        assert not (session_dir / "context.txt").is_file()

        # Should load back correctly
        loaded = await ctx_mgr.load("session1")
        assert loaded == content

    async def test_load_returns_empty_string_when_no_file_exists(self, ctx_mgr):
        loaded = await ctx_mgr.load("nonexistent_session")
        assert loaded == ""

    async def test_load_handles_corrupt_compressed_file_gracefully(self, tmp_path):
        settings = Settings(context_max_bytes=10)  # very small threshold
        mgr = ContextManager(settings, base_dir=tmp_path)

        session_dir = tmp_path / "session1"
        session_dir.mkdir(parents=True)
        # Write corrupt zlib data
        corrupt_file = session_dir / "context.zlib"
        corrupt_file.write_bytes(b"this is not valid zlib data")

        loaded = await mgr.load("session1")
        assert loaded == ""

    async def test_save_removes_old_files_before_writing(self, ctx_mgr, tmp_path):
        # First save as plain text (small content)
        await ctx_mgr.save("session1", "short")
        session_dir = tmp_path / "session1"
        assert (session_dir / "context.txt").is_file()

        # Now save large content — should compress and remove old plain text
        content = "X" * 200
        await ctx_mgr.save("session1", content)
        assert not (session_dir / "context.txt").is_file()

        # Verify the content loads correctly
        loaded = await ctx_mgr.load("session1")
        assert loaded == content

    async def test_save_and_load_with_zlib_algorithm(self, tmp_path):
        settings = Settings(context_max_bytes=10, context_compression="zlib")
        mgr = ContextManager(settings, base_dir=tmp_path)
        content = "This is a moderately long text that should be compressed with zlib. " * 3

        await mgr.save("session1", content)

        session_dir = tmp_path / "session1"
        assert (session_dir / "context.zlib").is_file()

        loaded = await mgr.load("session1")
        assert loaded == content

    async def test_save_and_load_with_lzma_algorithm(self, tmp_path):
        settings = Settings(context_max_bytes=10, context_compression="lzma")
        mgr = ContextManager(settings, base_dir=tmp_path)
        content = "This is a moderately long text that should be compressed with lzma. " * 3

        await mgr.save("session1", content)

        session_dir = tmp_path / "session1"
        assert (session_dir / "context.lzma").is_file()

        loaded = await mgr.load("session1")
        assert loaded == content

    async def test_meta_file_written_on_compressed_save(self, ctx_mgr, tmp_path):
        content = "B" * 200
        await ctx_mgr.save("session1", content)

        session_dir = tmp_path / "session1"
        meta_file = session_dir / "context.meta.json"
        assert meta_file.is_file()

        meta = json.loads(meta_file.read_text())
        assert "algorithm" in meta
        assert "original_size" in meta
        assert "compressed_size" in meta
        assert meta["original_size"] == 200

    async def test_no_meta_file_for_plain_text_save(self, ctx_mgr_large_threshold, tmp_path):
        content = "Short content"
        await ctx_mgr_large_threshold.save("session1", content)

        session_dir = tmp_path / "session1"
        meta_file = session_dir / "context.meta.json"
        assert not meta_file.is_file()


# ---------------------------------------------------------------------------
# ContextManager.append() tests
# ---------------------------------------------------------------------------


class TestContextManagerAppend:
    async def test_appends_to_existing_content_with_newline(self, ctx_mgr_large_threshold):
        await ctx_mgr_large_threshold.save("session1", "First line")
        await ctx_mgr_large_threshold.append("session1", "Second line")

        loaded = await ctx_mgr_large_threshold.load("session1")
        assert loaded == "First line\nSecond line"

    async def test_append_works_on_empty_context(self, ctx_mgr_large_threshold):
        # No previous save
        await ctx_mgr_large_threshold.append("session1", "New content")

        loaded = await ctx_mgr_large_threshold.load("session1")
        assert loaded == "New content"

    async def test_multiple_appends(self, ctx_mgr_large_threshold):
        await ctx_mgr_large_threshold.save("session1", "Line 1")
        await ctx_mgr_large_threshold.append("session1", "Line 2")
        await ctx_mgr_large_threshold.append("session1", "Line 3")

        loaded = await ctx_mgr_large_threshold.load("session1")
        assert loaded == "Line 1\nLine 2\nLine 3"


# ---------------------------------------------------------------------------
# ContextManager.get_size() tests
# ---------------------------------------------------------------------------


class TestContextManagerGetSize:
    def test_returns_zero_when_no_file(self, ctx_mgr):
        size = ctx_mgr.get_size("nonexistent_session")
        assert size == 0

    async def test_returns_correct_size_after_plain_save(self, ctx_mgr_large_threshold, tmp_path):
        content = "Hello, world!"
        await ctx_mgr_large_threshold.save("session1", content)

        size = ctx_mgr_large_threshold.get_size("session1")
        # Size should match the byte length of the plain text file
        plain_file = tmp_path / "session1" / "context.txt"
        assert size == plain_file.stat().st_size

    async def test_returns_compressed_size_for_compressed_file(self, ctx_mgr, tmp_path):
        content = "C" * 200  # Will be compressed
        await ctx_mgr.save("session1", content)

        size = ctx_mgr.get_size("session1")
        # Size should be positive but less than original
        assert size > 0
        # For compressed data, size should be less than original 200 bytes
        # (though for single character repeated, it should compress well)
        assert size < 200


# ---------------------------------------------------------------------------
# ContextManager._find_context_file() tests
# ---------------------------------------------------------------------------


class TestFindContextFile:
    def test_compressed_file_takes_priority_over_plain_text(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "session1"
        session_dir.mkdir(parents=True)

        # Create both plain and compressed files
        plain = session_dir / "context.txt"
        plain.write_text("plain content")
        compressed = session_dir / "context.zlib"
        compressed.write_bytes(zlib.compress(b"compressed content"))

        result = ctx_mgr._find_context_file(session_dir)
        assert result is not None
        found_path, algorithm = result
        # Should find the compressed file, not the plain one
        assert found_path == compressed

    def test_returns_plain_when_no_compressed_exists(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "session1"
        session_dir.mkdir(parents=True)

        plain = session_dir / "context.txt"
        plain.write_text("plain content")

        result = ctx_mgr._find_context_file(session_dir)
        assert result is not None
        found_path, algorithm = result
        assert found_path == plain
        assert algorithm is None

    def test_returns_none_when_no_files_exist(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "empty_session"
        session_dir.mkdir(parents=True)

        result = ctx_mgr._find_context_file(session_dir)
        assert result is None


# ---------------------------------------------------------------------------
# ContextManager._remove_context_files() tests
# ---------------------------------------------------------------------------


class TestRemoveContextFiles:
    def test_removes_all_context_files(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "session1"
        session_dir.mkdir(parents=True)

        # Create all possible context files
        (session_dir / "context.txt").write_text("plain")
        (session_dir / "context.zlib").write_bytes(b"zlib data")
        (session_dir / "context.lzma").write_bytes(b"lzma data")
        (session_dir / "context.br").write_bytes(b"brotli data")
        (session_dir / "context.meta.json").write_text("{}")

        ctx_mgr._remove_context_files(session_dir)

        assert not (session_dir / "context.txt").exists()
        assert not (session_dir / "context.zlib").exists()
        assert not (session_dir / "context.lzma").exists()
        assert not (session_dir / "context.br").exists()
        assert not (session_dir / "context.meta.json").exists()

    def test_does_not_fail_when_no_files_exist(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "empty_session"
        session_dir.mkdir(parents=True)

        # Should not raise
        ctx_mgr._remove_context_files(session_dir)

    def test_does_not_remove_directory(self, ctx_mgr, tmp_path):
        session_dir = tmp_path / "session1"
        session_dir.mkdir(parents=True)
        (session_dir / "context.txt").write_text("content")

        ctx_mgr._remove_context_files(session_dir)

        # Directory should still exist
        assert session_dir.is_dir()
