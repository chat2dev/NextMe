"""Comprehensive tests for nextme.context.compression."""

from __future__ import annotations

import pytest

from nextme.context.compression import (
    CompressionAlgorithm,
    CompressionResult,
    compress,
    decompress,
    choose_algorithm,
    _brotli_available,
)
from nextme.config.schema import Settings


# ---------------------------------------------------------------------------
# CompressionAlgorithm enum tests
# ---------------------------------------------------------------------------


class TestCompressionAlgorithm:
    def test_zlib_value(self):
        assert CompressionAlgorithm.ZLIB == "zlib"
        assert CompressionAlgorithm.ZLIB.value == "zlib"

    def test_lzma_value(self):
        assert CompressionAlgorithm.LZMA == "lzma"
        assert CompressionAlgorithm.LZMA.value == "lzma"

    def test_brotli_value(self):
        assert CompressionAlgorithm.BROTLI == "brotli"
        assert CompressionAlgorithm.BROTLI.value == "brotli"

    def test_all_members(self):
        members = {a.value for a in CompressionAlgorithm}
        assert members == {"zlib", "lzma", "brotli"}


# ---------------------------------------------------------------------------
# compress() tests
# ---------------------------------------------------------------------------

SAMPLE_TEXT = (
    b"The quick brown fox jumps over the lazy dog. " * 50
)  # ~2200 bytes — should compress well


class TestCompress:
    def test_zlib_returns_compression_result(self):
        result = compress(SAMPLE_TEXT, CompressionAlgorithm.ZLIB)
        assert isinstance(result, CompressionResult)
        assert result.algorithm == CompressionAlgorithm.ZLIB
        assert result.original_size == len(SAMPLE_TEXT)
        assert result.compressed_size == len(result.data)
        assert isinstance(result.data, bytes)

    def test_zlib_compresses_typical_text(self):
        result = compress(SAMPLE_TEXT, CompressionAlgorithm.ZLIB)
        assert result.compressed_size < result.original_size

    def test_lzma_returns_compression_result(self):
        result = compress(SAMPLE_TEXT, CompressionAlgorithm.LZMA)
        assert isinstance(result, CompressionResult)
        assert result.algorithm == CompressionAlgorithm.LZMA
        assert result.original_size == len(SAMPLE_TEXT)
        assert result.compressed_size == len(result.data)

    def test_lzma_compresses_typical_text(self):
        result = compress(SAMPLE_TEXT, CompressionAlgorithm.LZMA)
        assert result.compressed_size < result.original_size

    def test_brotli_raises_import_error_when_not_installed(self, monkeypatch):
        import sys

        # Remove brotli from sys.modules so the import inside compress() fails
        monkeypatch.setitem(sys.modules, "brotli", None)  # None means "not importable"

        with pytest.raises(ImportError, match="brotli"):
            compress(SAMPLE_TEXT, CompressionAlgorithm.BROTLI)

    def test_raises_value_error_for_unsupported_algorithm(self):
        # Cast a raw string to CompressionAlgorithm via value
        with pytest.raises(ValueError, match="Unsupported"):
            # We need to bypass enum validation; use a string that won't match
            compress(SAMPLE_TEXT, "unknown_algo")  # type: ignore[arg-type]

    def test_compress_empty_bytes(self):
        result = compress(b"", CompressionAlgorithm.ZLIB)
        assert result.original_size == 0
        assert isinstance(result.data, bytes)


# ---------------------------------------------------------------------------
# decompress() tests
# ---------------------------------------------------------------------------


class TestDecompress:
    def test_zlib_returns_original_data(self):
        compressed = compress(SAMPLE_TEXT, CompressionAlgorithm.ZLIB)
        result = decompress(compressed.data, CompressionAlgorithm.ZLIB)
        assert result == SAMPLE_TEXT

    def test_lzma_returns_original_data(self):
        compressed = compress(SAMPLE_TEXT, CompressionAlgorithm.LZMA)
        result = decompress(compressed.data, CompressionAlgorithm.LZMA)
        assert result == SAMPLE_TEXT

    def test_brotli_raises_import_error_when_not_installed(self, monkeypatch):
        import sys

        # Remove brotli from sys.modules so the import inside decompress() fails
        monkeypatch.setitem(sys.modules, "brotli", None)  # None means "not importable"

        with pytest.raises(ImportError, match="brotli"):
            decompress(b"some data", CompressionAlgorithm.BROTLI)

    def test_raises_value_error_for_unsupported_algorithm(self):
        with pytest.raises(ValueError, match="Unsupported"):
            decompress(b"some data", "unknown_algo")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_zlib_round_trip(self):
        original = b"Hello, world! This is a test string for round-trip compression." * 20
        result = compress(original, CompressionAlgorithm.ZLIB)
        recovered = decompress(result.data, CompressionAlgorithm.ZLIB)
        assert recovered == original

    def test_lzma_round_trip(self):
        original = b"Hello, world! This is a test string for round-trip compression." * 20
        result = compress(original, CompressionAlgorithm.LZMA)
        recovered = decompress(result.data, CompressionAlgorithm.LZMA)
        assert recovered == original

    def test_zlib_round_trip_utf8_text(self):
        original_text = "你好世界！这是一段测试文本。" * 30
        original_bytes = original_text.encode("utf-8")
        result = compress(original_bytes, CompressionAlgorithm.ZLIB)
        recovered_bytes = decompress(result.data, CompressionAlgorithm.ZLIB)
        assert recovered_bytes.decode("utf-8") == original_text

    def test_lzma_round_trip_utf8_text(self):
        original_text = "Hello, this is some unicode text. " * 30
        original_bytes = original_text.encode("utf-8")
        result = compress(original_bytes, CompressionAlgorithm.LZMA)
        recovered_bytes = decompress(result.data, CompressionAlgorithm.LZMA)
        assert recovered_bytes.decode("utf-8") == original_text


# ---------------------------------------------------------------------------
# choose_algorithm() tests
# ---------------------------------------------------------------------------


class TestChooseAlgorithm:
    def test_returns_zlib_when_setting_is_zlib_and_brotli_unavailable(self, monkeypatch):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: False)

        settings = Settings(context_compression="zlib")
        result = comp_module.choose_algorithm(1000, settings)
        assert result == CompressionAlgorithm.ZLIB

    def test_returns_lzma_when_setting_is_lzma_and_brotli_unavailable(self, monkeypatch):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: False)

        settings = Settings(context_compression="lzma")
        result = comp_module.choose_algorithm(1000, settings)
        assert result == CompressionAlgorithm.LZMA

    def test_uses_size_heuristic_when_brotli_configured_but_unavailable(self, monkeypatch):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: False)

        settings = Settings(context_compression="brotli")
        # Small size → ZLIB
        result_small = comp_module.choose_algorithm(100, settings)
        assert result_small == CompressionAlgorithm.ZLIB
        # Large size → LZMA
        result_large = comp_module.choose_algorithm(600 * 1024, settings)
        assert result_large == CompressionAlgorithm.LZMA

    def test_size_below_500kb_uses_zlib_when_brotli_unavailable_and_brotli_configured(
        self, monkeypatch
    ):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: False)

        settings = Settings(context_compression="brotli")
        _500_KB = 500 * 1024
        result = comp_module.choose_algorithm(_500_KB - 1, settings)
        assert result == CompressionAlgorithm.ZLIB

    def test_size_at_500kb_uses_lzma_when_brotli_unavailable_and_brotli_configured(
        self, monkeypatch
    ):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: False)

        settings = Settings(context_compression="brotli")
        _500_KB = 500 * 1024
        result = comp_module.choose_algorithm(_500_KB, settings)
        assert result == CompressionAlgorithm.LZMA

    def test_returns_brotli_when_brotli_available(self, monkeypatch):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: True)

        settings = Settings(context_compression="zlib")
        result = comp_module.choose_algorithm(1000, settings)
        assert result == CompressionAlgorithm.BROTLI

    def test_brotli_takes_priority_over_settings_when_available(self, monkeypatch):
        import nextme.context.compression as comp_module
        monkeypatch.setattr(comp_module, "_brotli_available", lambda: True)

        # Even when settings says lzma, brotli should win
        settings = Settings(context_compression="lzma")
        result = comp_module.choose_algorithm(1000, settings)
        assert result == CompressionAlgorithm.BROTLI
