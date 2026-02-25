"""Feishu WebSocket long-connection client with auto-reconnect.

Wraps ``lark_oapi.ws.Client`` (which handles reconnection internally) and
provides lifecycle management (start / stop) plus a factory for
``FeishuReplier`` instances.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import lark_oapi as lark

from nextme.config.schema import AppConfig, Settings
from nextme.feishu.handler import MessageHandler
from nextme.feishu.reply import FeishuReplier

logger = logging.getLogger(__name__)


class FeishuClient:
    """Manage a Feishu WebSocket connection and expose a ``FeishuReplier``."""

    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        handler: MessageHandler,
    ) -> None:
        self._config = config
        self._settings = settings
        self._handler = handler

        # Determine SDK log level from settings.
        _log_level_map = {
            "DEBUG": lark.LogLevel.DEBUG,
            "INFO": lark.LogLevel.INFO,
            "WARNING": lark.LogLevel.WARNING,
            "ERROR": lark.LogLevel.ERROR,
        }
        sdk_log_level = _log_level_map.get(
            settings.log_level.upper(), lark.LogLevel.INFO
        )

        # REST client (used for sending messages, reactions, etc.)
        self._lark_client: lark.Client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .build()
        )

        # WebSocket client (handles long-connection + reconnect automatically).
        event_dispatcher = handler.build_event_dispatcher()
        self._ws_client: lark.ws.Client = lark.ws.Client(
            config.app_id,
            config.app_secret,
            event_handler=event_dispatcher,
            log_level=sdk_log_level,
        )

        self._stop_event: asyncio.Event = asyncio.Event()
        # The fresh event loop used by the ws thread; set during start(), used by stop().
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket connection.

        Registers the current event loop with the handler so that message
        callbacks can schedule coroutines onto it, then runs the lark WS
        client in a thread executor (``lark.ws.Client.start()`` is a blocking
        call).  Returns only after ``stop()`` is called.
        """
        loop = asyncio.get_running_loop()
        self._handler.attach_loop(loop)

        logger.info(
            "Starting Feishu WebSocket client (app_id=%s)", self._config.app_id
        )

        self._stop_event.clear()

        # lark_oapi.ws.client stores a module-level ``loop`` variable captured
        # via asyncio.get_event_loop() at first import.  When the module is
        # first imported inside asyncio.run() (as is the case here due to lazy
        # imports), that variable holds the *running* main event loop.  Calling
        # loop.run_until_complete() on an already-running loop raises
        # "RuntimeError: This event loop is already running".
        #
        # Fix: inside the thread executor, temporarily replace the module-level
        # ``loop`` reference with a fresh event loop so lark's blocking
        # run_until_complete() calls succeed.
        import lark_oapi.ws.client as _lark_ws_mod  # noqa: PLC0415

        def _run_ws() -> None:
            fresh = asyncio.new_event_loop()
            prev = _lark_ws_mod.loop
            _lark_ws_mod.loop = fresh
            self._ws_loop = fresh  # expose for stop()
            try:
                self._ws_client.start()
            finally:
                self._ws_loop = None
                _lark_ws_mod.loop = prev
                fresh.close()

        try:
            await loop.run_in_executor(None, _run_ws)
        except asyncio.CancelledError:
            logger.info("FeishuClient.start() cancelled")
            raise
        except Exception:
            logger.exception("FeishuClient WebSocket error")
            raise
        finally:
            self._stop_event.set()
            logger.info("FeishuClient WebSocket connection closed")

    async def stop(self) -> None:
        """Gracefully disconnect the WebSocket connection.

        The lark SDK's ``ws.Client.start()`` blocks on an infinite
        ``asyncio.sleep`` loop (``_select()``) inside a thread-executor.
        There is no public ``stop()`` method.  We stop it by:

        1. Disabling auto-reconnect so the client won't reopen after close.
        2. Stopping the thread's private event loop via
           ``call_soon_threadsafe(loop.stop)`` — this unblocks
           ``loop.run_until_complete(_select())``.
        """
        logger.info("Stopping Feishu WebSocket client")

        # Disable reconnection so the client doesn't fight the shutdown.
        try:
            self._ws_client._auto_reconnect = False
        except Exception:
            pass

        # Stop the thread's event loop to unblock _select().
        ws_loop = self._ws_loop
        if ws_loop is not None and not ws_loop.is_closed():
            try:
                ws_loop.call_soon_threadsafe(ws_loop.stop)
                logger.debug("Sent stop() to ws thread event loop")
            except Exception:
                logger.exception("Error stopping ws thread event loop")

        self._stop_event.set()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def get_replier(self) -> FeishuReplier:
        """Return a ``FeishuReplier`` backed by the underlying lark REST client."""
        return FeishuReplier(self._lark_client)
