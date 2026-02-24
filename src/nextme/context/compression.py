"""Compression utilities for context storage.

Supported algorithms
--------------------
- ``zlib``   — fastest; good ratio for text; stdlib only
- ``lzma``   — best ratio; slower; stdlib only
- ``brotli`` — fast + excellent ratio for text; requires the ``brotli``
               extra (``pip install nextme[brotli]``)

Algorithm selection
-------------------
:func:`choose_algorithm` follows the rule:

* If the optional ``brotli`` package is installed, always prefer Brotli.
* Otherwise use ``zlib`` for data < 500 KB and ``lzma`` for larger data.

The *settings.context_compression* value is used as a hint / override
when it matches a supported algorithm; if Brotli is requested but not
installed, the function falls back automatically.
"""

from __future__ import annotations

import zlib
import lzma
from enum import Enum
from typing import NamedTuple

from ..config.schema import Settings


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class CompressionAlgorithm(str, Enum):
    ZLIB = "zlib"
    LZMA = "lzma"
    BROTLI = "brotli"


class CompressionResult(NamedTuple):
    data: bytes
    algorithm: CompressionAlgorithm
    original_size: int
    compressed_size: int


# ---------------------------------------------------------------------------
# Internal: optional brotli import
# ---------------------------------------------------------------------------


def _brotli_available() -> bool:
    """Return True when the ``brotli`` package is importable."""
    try:
        import brotli  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compress(
    data: bytes,
    algorithm: CompressionAlgorithm = CompressionAlgorithm.ZLIB,
) -> CompressionResult:
    """Compress *data* with the given *algorithm*.

    Parameters
    ----------
    data:
        Raw bytes to compress.
    algorithm:
        One of :class:`CompressionAlgorithm`.

    Returns
    -------
    CompressionResult
        Named tuple containing the compressed bytes, the algorithm used,
        the original size, and the compressed size.

    Raises
    ------
    ImportError
        If ``algorithm`` is BROTLI but the ``brotli`` package is not installed.
    ValueError
        If an unsupported algorithm is specified.
    """
    original_size = len(data)

    if algorithm == CompressionAlgorithm.ZLIB:
        compressed = zlib.compress(data, level=6)

    elif algorithm == CompressionAlgorithm.LZMA:
        compressed = lzma.compress(data, preset=6)

    elif algorithm == CompressionAlgorithm.BROTLI:
        try:
            import brotli
        except ImportError as exc:
            raise ImportError(
                "The 'brotli' package is required for Brotli compression. "
                "Install it with: pip install nextme[brotli]"
            ) from exc
        compressed = brotli.compress(data, quality=6)

    else:
        raise ValueError(f"Unsupported compression algorithm: {algorithm!r}")

    return CompressionResult(
        data=compressed,
        algorithm=algorithm,
        original_size=original_size,
        compressed_size=len(compressed),
    )


def decompress(data: bytes, algorithm: CompressionAlgorithm) -> bytes:
    """Decompress *data* that was compressed with *algorithm*.

    Parameters
    ----------
    data:
        Compressed bytes.
    algorithm:
        The algorithm that was used to compress *data*.

    Returns
    -------
    bytes
        Decompressed raw bytes.

    Raises
    ------
    ImportError
        If ``algorithm`` is BROTLI but the ``brotli`` package is not installed.
    ValueError
        If an unsupported algorithm is specified.
    """
    if algorithm == CompressionAlgorithm.ZLIB:
        return zlib.decompress(data)

    elif algorithm == CompressionAlgorithm.LZMA:
        return lzma.decompress(data)

    elif algorithm == CompressionAlgorithm.BROTLI:
        try:
            import brotli
        except ImportError as exc:
            raise ImportError(
                "The 'brotli' package is required for Brotli decompression. "
                "Install it with: pip install nextme[brotli]"
            ) from exc
        return brotli.decompress(data)

    else:
        raise ValueError(f"Unsupported compression algorithm: {algorithm!r}")


def choose_algorithm(size: int, settings: Settings) -> CompressionAlgorithm:
    """Select the best compression algorithm for *size* bytes of data.

    Decision logic
    --------------
    1. If Brotli is installed, always return BROTLI (best ratio for text).
    2. Otherwise, use the ``settings.context_compression`` preference if it
       is not BROTLI (i.e. a valid stdlib algorithm was configured).
    3. Final fallback: ZLIB for < 500 KB, LZMA for larger data.

    Parameters
    ----------
    size:
        Number of bytes to be compressed.
    settings:
        Application settings; ``context_compression`` is used as a hint.

    Returns
    -------
    CompressionAlgorithm
        The selected algorithm.
    """
    _500_KB = 500 * 1024

    if _brotli_available():
        return CompressionAlgorithm.BROTLI

    # Honour explicit non-brotli setting preference.
    configured = settings.context_compression
    if configured == "lzma":
        return CompressionAlgorithm.LZMA
    if configured == "zlib":
        return CompressionAlgorithm.ZLIB

    # Brotli was configured but not installed — fall back to size heuristic.
    return CompressionAlgorithm.ZLIB if size < _500_KB else CompressionAlgorithm.LZMA
