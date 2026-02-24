"""ACPClient — thin async wrapper around a subprocess's stdin/stdout.

Handles the low-level I/O: serialising outbound dataclass messages to ndjson
lines written to ``proc.stdin``, and yielding parsed inbound dicts from
``proc.stdout`` line by line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from .protocol import parse_acp_message, serialize_msg

logger = logging.getLogger(__name__)


class ACPClient:
    """Wraps a running asyncio subprocess with ndjson I/O helpers.

    Args:
        proc: A subprocess created via ``asyncio.create_subprocess_exec`` with
              ``PIPE`` for both ``stdin`` and ``stdout``.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    # ------------------------------------------------------------------
    # Outbound (Bot → ACP)
    # ------------------------------------------------------------------

    async def send(self, msg: Any) -> None:
        """Serialize *msg* to ndjson and write it to ``proc.stdin``.

        A ``\\n`` newline is appended so the ACP process can delimit messages
        by line.  The write is followed by a ``drain()`` to ensure the bytes
        are flushed to the OS pipe buffer.

        Args:
            msg: Any dataclass instance understood by :func:`serialize_msg`.

        Raises:
            RuntimeError: If the subprocess stdin is not available (already
                closed, or process was not created with ``PIPE``).
        """
        if self._proc.stdin is None:
            raise RuntimeError("ACPClient: subprocess stdin is not available")

        line = serialize_msg(msg) + "\n"
        encoded = line.encode("utf-8")

        logger.debug("ACP → stdin: %s", line.rstrip())

        self._proc.stdin.write(encoded)
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(
                f"ACPClient: failed to write to subprocess stdin: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Inbound (ACP → Bot)
    # ------------------------------------------------------------------

    async def read_lines(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed ndjson dicts from ``proc.stdout`` line by line.

        Iterates until EOF (subprocess closed its stdout or was terminated).
        Empty lines and lines that cannot be decoded as valid ndjson are
        logged and skipped rather than raising, so the caller's loop remains
        robust against noise on stdout.

        Yields:
            A plain ``dict`` with at least a ``"type"`` key for every valid
            ndjson object line received.
        """
        if self._proc.stdout is None:
            raise RuntimeError("ACPClient: subprocess stdout is not available")

        while True:
            try:
                raw_bytes = await self._proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ACPClient: error reading from subprocess stdout: %s", exc)
                break

            # EOF — subprocess closed stdout.
            if not raw_bytes:
                logger.debug("ACPClient: subprocess stdout reached EOF")
                break

            try:
                line = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.warning(
                    "ACPClient: could not decode stdout bytes as UTF-8: %s", exc
                )
                continue

            line_stripped = line.strip()
            if not line_stripped:
                continue

            try:
                msg = parse_acp_message(line_stripped)
            except ValueError as exc:
                logger.warning("ACPClient: skipping unparseable line: %s | error: %s", line_stripped, exc)
                continue

            logger.debug("ACP ← stdout: %s", line_stripped)
            yield msg
