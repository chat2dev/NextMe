"""Per-session context file manager backed by ~/.nextme/threads/{session_id}/.

Files per session directory
---------------------------
- ``context.txt``  (uncompressed) *or*
  ``context.zlib`` / ``context.lzma`` / ``context.br`` (compressed)
- ``context.meta.json``  — metadata::

      {
          "algorithm": "zlib",
          "original_size": 12345,
          "compressed_size": 4567
      }

Compression is applied when the context exceeds
``settings.context_max_bytes``.  Compression uses the algorithm chosen by
:func:`~nextme.context.compression.choose_algorithm`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from ..config.schema import Settings
from .compression import (
    CompressionAlgorithm,
    choose_algorithm,
    compress,
    decompress,
)

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()

# Extension mapping for compressed files.
_EXT_MAP: dict[CompressionAlgorithm, str] = {
    CompressionAlgorithm.ZLIB: ".zlib",
    CompressionAlgorithm.LZMA: ".lzma",
    CompressionAlgorithm.BROTLI: ".br",
}

_META_FILENAME = "context.meta.json"
_PLAIN_FILENAME = "context.txt"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".ctx_tmp_", suffix=".bin")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_text_atomic(path: Path, text: str) -> None:
    """Write *text* (UTF-8) to *path* atomically."""
    _write_bytes_atomic(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class ContextManager:
    """Manages per-session context files in ``~/.nextme/threads/{session_id}/``.

    Parameters
    ----------
    settings:
        Application settings; ``context_max_bytes`` determines when to
        compress, and ``context_compression`` hints at the preferred
        algorithm.
    base_dir:
        Override the default ``~/.nextme/threads`` root.  Useful in tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        base_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._base_dir: Path = base_dir or (_NEXTME_HOME / "threads")

    # ------------------------------------------------------------------
    # Directory / path helpers
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        # session_id may contain ":" which is fine on POSIX but not on Windows.
        # We keep the raw value here; callers on Windows would need sanitisation.
        return self._base_dir / session_id

    def _find_context_file(self, session_dir: Path) -> tuple[Path, CompressionAlgorithm | None] | None:
        """Find the context file in *session_dir*.

        Returns a ``(path, algorithm | None)`` tuple, where *algorithm* is
        ``None`` for the plain text file.  Returns ``None`` when no context
        file exists.
        """
        # Check compressed variants first (prefer to plain).
        for algo, ext in _EXT_MAP.items():
            candidate = session_dir / f"context{ext}"
            if candidate.is_file():
                return candidate, algo
        # Plain text fallback.
        plain = session_dir / _PLAIN_FILENAME
        if plain.is_file():
            return plain, None
        return None

    def _remove_context_files(self, session_dir: Path) -> None:
        """Delete all context data files (but not the directory itself)."""
        for ext in _EXT_MAP.values():
            (session_dir / f"context{ext}").unlink(missing_ok=True)
        (session_dir / _PLAIN_FILENAME).unlink(missing_ok=True)
        (session_dir / _META_FILENAME).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def save(self, session_id: str, content: str) -> None:
        """Persist *content* for *session_id*.

        The content is compressed when its UTF-8 byte length exceeds
        ``settings.context_max_bytes``.  Any previously stored context
        files are removed before writing the new one.

        Parameters
        ----------
        session_id:
            The session identifier used as the subdirectory name.
        content:
            The context string to store.
        """
        session_dir = self._session_dir(session_id)
        raw_bytes = content.encode("utf-8")
        content_len = len(raw_bytes)

        # Remove any stale files from a previous save (algorithm may differ).
        self._remove_context_files(session_dir)

        if content_len <= self._settings.context_max_bytes:
            # Store plain text — no compression needed.
            _write_text_atomic(session_dir / _PLAIN_FILENAME, content)
            logger.debug(
                "ContextManager[%s]: saved plain context (%d bytes)",
                session_id,
                content_len,
            )
        else:
            algo = choose_algorithm(content_len, self._settings)
            result = compress(raw_bytes, algo)
            ext = _EXT_MAP[algo]
            context_file = session_dir / f"context{ext}"
            _write_bytes_atomic(context_file, result.data)

            # Write metadata alongside.
            meta = {
                "algorithm": algo.value,
                "original_size": result.original_size,
                "compressed_size": result.compressed_size,
            }
            _write_text_atomic(
                session_dir / _META_FILENAME,
                json.dumps(meta, indent=2),
            )
            logger.debug(
                "ContextManager[%s]: saved compressed context "
                "(%d → %d bytes, algorithm=%s)",
                session_id,
                result.original_size,
                result.compressed_size,
                algo.value,
            )

    async def load(self, session_id: str) -> str:
        """Load and decompress the context for *session_id*.

        Returns an empty string when no context file exists.
        """
        session_dir = self._session_dir(session_id)
        found = self._find_context_file(session_dir)
        if found is None:
            return ""

        file_path, algorithm = found

        try:
            raw_bytes = file_path.read_bytes()
        except OSError as exc:
            logger.warning(
                "ContextManager[%s]: error reading context file: %s", session_id, exc
            )
            return ""

        if algorithm is None:
            # Plain text file.
            return raw_bytes.decode("utf-8")

        try:
            decompressed = decompress(raw_bytes, algorithm)
        except Exception as exc:
            logger.warning(
                "ContextManager[%s]: decompression failed (%s): %s",
                session_id,
                algorithm.value,
                exc,
            )
            return ""

        return decompressed.decode("utf-8")

    async def append(self, session_id: str, new_content: str) -> None:
        """Append *new_content* to the existing context for *session_id*.

        Loads the current context, appends *new_content* (separated by a
        newline if the existing content is non-empty), then saves the
        merged result.

        Parameters
        ----------
        session_id:
            The session identifier.
        new_content:
            Text to append.
        """
        existing = await self.load(session_id)
        if existing:
            merged = existing + "\n" + new_content
        else:
            merged = new_content
        await self.save(session_id, merged)

    def get_size(self, session_id: str) -> int:
        """Return the on-disk size of the context file in bytes.

        For compressed files this is the *compressed* byte size.
        Returns ``0`` when no context file exists.
        """
        session_dir = self._session_dir(session_id)
        found = self._find_context_file(session_dir)
        if found is None:
            return 0
        file_path, _ = found
        try:
            return file_path.stat().st_size
        except OSError:
            return 0
