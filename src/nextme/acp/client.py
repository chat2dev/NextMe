"""ACPClient — bidirectional JSON-RPC 2.0 wrapper around a subprocess's stdin/stdout.

Responsibilities
----------------
* Serialize outbound requests (client → cc-acp, via stdin).
* Yield all inbound messages from cc-acp's stdout, classified as:
    - ``"response"``       matched response to one of our requests
    - ``"notification"``   push notification (no id, ``session/update``)
    - ``"server_request"`` cc-acp calling the client (e.g. permission)
* Send response messages back to cc-acp for ``server_request`` messages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from .protocol import make_error_response, make_request, make_response, parse_message

logger = logging.getLogger(__name__)


class ACPClient:
    """Wraps a running asyncio subprocess with bidirectional JSON-RPC I/O.

    Args:
        proc: A subprocess created via ``asyncio.create_subprocess_exec`` with
              ``PIPE`` for both ``stdin`` and ``stdout``.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._req_id: int = 0

    # ------------------------------------------------------------------
    # Outbound — client → cc-acp (stdin)
    # ------------------------------------------------------------------

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        """Serialize a JSON-RPC request to stdin and return the request id.

        Args:
            method: JSON-RPC method name (e.g. ``"session/new"``).
            params: Method parameters dict.

        Returns:
            The integer request id that was assigned to this request.

        Raises:
            RuntimeError: If stdin is unavailable or the write fails.
        """
        if self._proc.stdin is None:
            raise RuntimeError("ACPClient: subprocess stdin is not available")

        self._req_id += 1
        req_id = self._req_id
        line = make_request(method, params, req_id) + "\n"
        logger.debug("ACP → [%d] %s", req_id, method)

        self._proc.stdin.write(line.encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(f"ACPClient: stdin write failed: {exc}") from exc

        return req_id

    async def send_response(self, req_id: int, result: Any) -> None:
        """Send a JSON-RPC success response to cc-acp (for inbound server requests).

        Args:
            req_id: The ``id`` from the server request being answered.
            result: The result payload dict.
        """
        await self._send_raw(make_response(req_id, result))

    async def send_error_response(self, req_id: int, code: int, message: str) -> None:
        """Send a JSON-RPC error response to cc-acp."""
        await self._send_raw(make_error_response(req_id, code, message))

    async def _send_raw(self, line: str) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("ACPClient: subprocess stdin is not available")
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(f"ACPClient: stdin write failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Inbound — cc-acp → client (stdout)
    # ------------------------------------------------------------------

    async def read_lines(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed JSON-RPC dicts from ``proc.stdout`` until EOF.

        Each yielded dict is a raw parsed message — callers should use
        :func:`~nextme.acp.protocol.classify` to determine the message kind.
        Invalid/empty lines are logged and skipped.

        Yields:
            Parsed JSON-RPC message dicts.
        """
        if self._proc.stdout is None:
            raise RuntimeError("ACPClient: subprocess stdout is not available")

        while True:
            try:
                raw = await self._proc.stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ACPClient: stdout read error: %s", exc)
                break

            if not raw:
                logger.debug("ACPClient: stdout EOF")
                break

            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.warning("ACPClient: UTF-8 decode error: %s", exc)
                continue

            stripped = line.strip()
            if not stripped:
                continue

            # Ignore non-JSON diagnostic lines emitted by cc-acp
            if not stripped.startswith("{"):
                logger.debug("ACP ← (non-json): %s", stripped)
                continue

            try:
                msg = parse_message(stripped)
            except ValueError as exc:
                logger.warning("ACPClient: skipping unparseable line: %s | %s", stripped[:200], exc)
                continue

            logger.debug("ACP ← %s", stripped[:300])
            yield msg
