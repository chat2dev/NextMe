#!/usr/bin/env python3
"""nextme — CLI entry point.

Usage:
    nextme up   [--directory DIR] [--executor EXECUTOR] [--log-level LEVEL]
    nextme down [--timeout SECS]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_NEXTME_HOME = Path("~/.nextme").expanduser()
_LOG_FILE = _NEXTME_HOME / "logs" / "nextme.log"
_PID_FILE = _NEXTME_HOME / "nextme.pid"

# Maximum seconds to wait for in-flight tasks to drain on shutdown.
_SHUTDOWN_DRAIN_TIMEOUT = 30

# Settings fields that can be reloaded at runtime via SIGHUP (./reload.sh).
# Fields NOT listed here (app_id, app_secret, projects, bindings) require restart.
_RELOADABLE_SETTINGS: frozenset[str] = frozenset(
    {
        "log_level",
        "progress_debounce_seconds",
        "memory_debounce_seconds",
        "memory_max_facts",
        "permission_auto_approve",
        "streaming_enabled",
        "admin_users",
    }
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(log_level: str) -> None:
    """Configure root logger: rotating file handler + stderr stream handler.

    File:   ~/.nextme/logs/nextme.log  (max 10 MB × 5 backups)
    Stderr: same log level, human-readable format.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Ensure the log directory exists.
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S")

    # Rotating file handler — 10 MB per file, keep 5 backups.
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    # Stderr handler.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(numeric_level)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)


# ---------------------------------------------------------------------------
# Graceful shutdown helpers
# ---------------------------------------------------------------------------


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """Register SIGTERM and SIGINT handlers that set *shutdown_event*."""

    def _signal_handler(sig: int) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("Received signal %s — initiating graceful shutdown", sig_name)
        if not shutdown_event.is_set():
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback: use signal.signal instead.
            signal.signal(sig, lambda s, f: _signal_handler(s))


# ---------------------------------------------------------------------------
# Hot-reload helpers (SIGHUP)
# ---------------------------------------------------------------------------


def _update_log_level(log_level: str) -> None:
    """Apply *log_level* to the root logger and all its handlers immediately."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in root.handlers:
        handler.setLevel(numeric_level)


async def _reload_settings_async(settings: object, acl_manager: object | None = None) -> None:
    """Re-read settings.json and hot-update reloadable fields in *settings* in-place."""
    from .config.loader import ConfigLoader

    try:
        fresh = ConfigLoader.load_settings()
    except Exception:
        logger.exception("SIGHUP: failed to reload settings; keeping current values")
        return

    changed: list[str] = []
    for field in _RELOADABLE_SETTINGS:
        old_val = getattr(settings, field, None)
        new_val = getattr(fresh, field, None)
        if old_val != new_val:
            setattr(settings, field, new_val)
            changed.append(f"{field}: {old_val!r} → {new_val!r}")

    if changed:
        logger.info("SIGHUP: settings reloaded — %s", "; ".join(changed))
        if any(c.startswith("log_level:") for c in changed):
            _update_log_level(getattr(settings, "log_level", "INFO"))
        if acl_manager is not None and any(c.startswith("admin_users:") for c in changed):
            from .acl.manager import AclManager as _AclManager
            if isinstance(acl_manager, _AclManager):
                acl_manager.reload_admin_users(getattr(settings, "admin_users", []))
                logger.info("SIGHUP: AclManager admin_users reloaded")
    else:
        logger.info("SIGHUP: settings reloaded — no changes detected")


def _install_sighup_handler(
    loop: asyncio.AbstractEventLoop,
    settings: object,
    acl_manager: object | None = None,
) -> None:
    """Register a SIGHUP handler that hot-reloads settings.json in-place.

    Reloadable fields: log_level, progress_debounce_seconds,
    memory_debounce_seconds, memory_max_facts, permission_auto_approve,
    streaming_enabled, admin_users.

    Fields requiring restart: app_id, app_secret, projects, bindings.
    """

    def _on_sighup() -> None:
        logger.info(
            "Received SIGHUP — reloading hot settings from %s",
            _NEXTME_HOME / "settings.json",
        )
        loop.create_task(_reload_settings_async(settings, acl_manager), name="sighup-reload")

    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, AttributeError):
        # SIGHUP is not available on Windows.
        logger.debug("SIGHUP not supported on this platform; hot-reload via signal disabled")


# ---------------------------------------------------------------------------
# Main async run function
# ---------------------------------------------------------------------------


async def run(directory: str | None, executor: str, log_level: str) -> None:
    """Full startup sequence for the nextme bot.

    Startup order:
    1. Load config (AppConfig + Settings).
    2. Setup logging.
    3. Validate credentials — exit if missing.
    4. Init StateStore, load state.
    5. Init MemoryManager, SkillRegistry, ContextManager.
    6. Init SessionRegistry, PathLockRegistry.
    7. Init ACPRuntimeRegistry, ACPJanitor.
    8. Init MessageHandler, TaskDispatcher, FeishuClient.
    9. Start background tasks: janitor.run(), state_store.start_debounce_loop(),
       memory_manager.start_debounce_loop().
    10. Register SIGTERM/SIGINT handlers.
    11. await feishu_client.start()  <-- blocks until shutdown signal.

    Shutdown order:
    1. feishu_client.stop()
    2. Wait for in-flight tasks (30s timeout).
    3. acp_registry.stop_all()
    4. memory_manager.flush_all()
    5. state_store.stop()
    6. Cancel background asyncio tasks.
    """
    # ------------------------------------------------------------------
    # Step 1: Load configuration
    # ------------------------------------------------------------------
    from .config.loader import ConfigLoader

    cwd = Path(directory).resolve() if directory else None
    config = ConfigLoader.load_app_config(cwd)
    settings = ConfigLoader.load_settings()

    # ------------------------------------------------------------------
    # Step 2: Setup logging (now that we have log_level from CLI + settings)
    # ------------------------------------------------------------------
    effective_log_level = log_level or settings.log_level
    _setup_logging(effective_log_level)

    logger.info("nextme starting up (log_level=%s)", effective_log_level)
    logger.info("Config loaded: app_id=%s, projects=%d", config.app_id, len(config.projects))

    # ------------------------------------------------------------------
    # Write PID file so `nextme down` can target the exact process.
    # If a previous instance is still running, stop it first so only
    # one nextme process is ever active at a time.
    # ------------------------------------------------------------------
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)  # probe: raises if not running
                    logger.info(
                        "nextme up: previous instance (pid=%d) still running, sending SIGTERM",
                        old_pid,
                    )
                    os.kill(old_pid, signal.SIGTERM)
                    # Wait up to 5 s for graceful exit.
                    for _ in range(50):
                        time.sleep(0.1)
                        try:
                            os.kill(old_pid, 0)
                        except ProcessLookupError:
                            break
                    else:
                        logger.warning(
                            "nextme up: previous instance (pid=%d) did not exit, sending SIGKILL",
                            old_pid,
                        )
                        try:
                            os.kill(old_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                except ProcessLookupError:
                    pass  # Already gone — stale PID file.
        except (ValueError, OSError):
            pass  # Unreadable PID file — ignore.
    _PID_FILE.write_text(str(os.getpid()))
    logger.debug("PID file written: %s (pid=%d)", _PID_FILE, os.getpid())

    # ------------------------------------------------------------------
    # Step 3: Validate credentials
    # ------------------------------------------------------------------
    if not config.app_id or not config.app_secret:
        print(
            "Error: Feishu app_id and app_secret must be configured.\n"
            "Set them in ~/.nextme/nextme.json, a local nextme.json, "
            "or via NEXTME_APP_ID / NEXTME_APP_SECRET environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 4: StateStore — load persistent state
    # ------------------------------------------------------------------
    from .config.state_store import StateStore

    state_store = StateStore(settings)
    await state_store.load()
    logger.info("StateStore: state loaded")

    # ------------------------------------------------------------------
    # Step 4b: AclDb + AclManager
    # ------------------------------------------------------------------
    from .acl.db import AclDb
    from .acl.manager import AclManager

    acl_db = AclDb()
    await acl_db.open()
    acl_manager = AclManager(db=acl_db, admin_users=settings.admin_users)
    logger.info(
        "AclManager: initialized (admin_users=%d)", len(settings.admin_users)
    )

    # ------------------------------------------------------------------
    # Step 5: MemoryManager, SkillRegistry, ContextManager
    # ------------------------------------------------------------------
    from .memory.manager import MemoryManager
    from .context.manager import ContextManager
    from .skills.registry import SkillRegistry

    memory_manager = MemoryManager(settings)
    context_manager = ContextManager(settings)
    skill_registry = SkillRegistry()
    executors = {p.executor for p in config.projects}
    skill_registry.load(project_path=cwd, executors=executors)
    logger.info("SkillRegistry: %d skill(s) loaded", len(skill_registry.list_all()))

    # ------------------------------------------------------------------
    # Step 6: SessionRegistry, PathLockRegistry
    # ------------------------------------------------------------------
    from .core.session import SessionRegistry
    from .core.path_lock import PathLockRegistry

    session_registry = SessionRegistry.get_instance()
    path_lock_registry = PathLockRegistry.get_instance()

    # ------------------------------------------------------------------
    # Step 7: ACPRuntimeRegistry, ACPJanitor
    # ------------------------------------------------------------------
    from .acp.janitor import ACPRuntimeRegistry, ACPJanitor

    acp_registry = ACPRuntimeRegistry()
    acp_janitor = ACPJanitor(acp_registry, settings)

    # ------------------------------------------------------------------
    # Step 8: MessageHandler, TaskDispatcher, FeishuClient
    # ------------------------------------------------------------------
    from .feishu.dedup import MessageDedup
    from .feishu.handler import MessageHandler
    from .feishu.client import FeishuClient
    from .core.dispatcher import TaskDispatcher

    dedup = MessageDedup()

    # Build a temporary placeholder FeishuClient so we can construct the
    # dispatcher.  The dispatcher only calls feishu_client.get_replier() at
    # dispatch time (not during construction), so we patch the reference once
    # the real client is built.
    #
    # Construction order:
    #   dispatcher (needs feishu_client ref) → handler (needs dispatcher)
    #   → feishu_client (needs handler) → patch dispatcher._feishu_client
    #
    # We use a two-step approach: build dispatcher with a dummy placeholder,
    # then replace the internal reference before any message arrives.

    class _PlaceholderFeishuClient:
        """Temporary stand-in used only during object-graph construction."""

        def get_replier(self):  # noqa: D102
            raise RuntimeError("FeishuClient not yet initialised")

    dispatcher = TaskDispatcher(
        config=config,
        settings=settings,
        session_registry=session_registry,
        acp_registry=acp_registry,
        path_lock_registry=path_lock_registry,
        feishu_client=_PlaceholderFeishuClient(),  # type: ignore[arg-type]
        state_store=state_store,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        acl_manager=acl_manager,
    )

    handler = MessageHandler(
        dedup=dedup,
        dispatcher=dispatcher,
        require_at_mention=settings.require_at_mention,
    )

    # Restore active thread set from persisted state so thread replies
    # received after a bot restart are still routed correctly.
    loaded_state = await state_store.load()
    thread_keys = set(loaded_state.thread_records.keys())
    handler.restore_active_threads(thread_keys)
    logger.info("MessageHandler: restored %d active thread(s) from state", len(thread_keys))

    # Keep handler._active_threads in sync when /done closes a thread.
    dispatcher.register_thread_closed_callback(handler.deregister_thread)
    # Keep handler._active_threads in sync when a queued thread is accepted.
    dispatcher.register_thread_accept_callback(handler.register_thread)

    feishu_client = FeishuClient(config, settings, handler=handler)

    # Wire the real client into the dispatcher so dispatch() works correctly.
    dispatcher._feishu_client = feishu_client

    # Fetch the bot's own open_id so the handler can filter @mentions precisely.
    bot_open_id = await feishu_client.fetch_bot_open_id()
    if bot_open_id:
        handler._bot_open_id = bot_open_id
        logger.info("MessageHandler: bot open_id set to %r", bot_open_id)
    else:
        logger.warning(
            "MessageHandler: could not fetch bot open_id; "
            "falling back to user_id heuristic for @mention detection"
        )

    # ------------------------------------------------------------------
    # Step 9: Start background tasks
    # ------------------------------------------------------------------
    loop = asyncio.get_running_loop()

    janitor_task = loop.create_task(acp_janitor.run(), name="acp-janitor")
    await state_store.start_debounce_loop()
    await memory_manager.start_debounce_loop()

    logger.info("Background tasks started")

    # ------------------------------------------------------------------
    # Step 9.5: Acquire macOS power assertion (prevent idle sleep)
    # ------------------------------------------------------------------
    from .power import PowerAssertion  # noqa: PLC0415

    power_assertion = PowerAssertion.acquire("NextMe Feishu WebSocket keepalive")

    # ------------------------------------------------------------------
    # Step 10: Register signal handlers
    # ------------------------------------------------------------------
    shutdown_event = asyncio.Event()
    _install_signal_handlers(loop, shutdown_event)
    _install_sighup_handler(loop, settings, acl_manager)

    # ------------------------------------------------------------------
    # Step 11: Start the Feishu WebSocket — blocks until shutdown
    # ------------------------------------------------------------------
    feishu_task = loop.create_task(feishu_client.start(), name="feishu-ws")

    # Wait for either the feishu task to fail or a shutdown signal.
    done, _pending = await asyncio.wait(
        [feishu_task, loop.create_task(shutdown_event.wait(), name="shutdown-wait")],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Surface any exception from the Feishu task.
    for completed in done:
        if completed is feishu_task and not completed.cancelled():
            exc = completed.exception()
            if exc is not None:
                logger.error("FeishuClient exited with error: %s", exc)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    logger.info("Shutting down…")

    # 1. Stop Feishu WebSocket.
    try:
        await feishu_client.stop()
    except Exception:
        logger.exception("Error stopping FeishuClient")

    if not feishu_task.done():
        feishu_task.cancel()
        try:
            await asyncio.wait_for(feishu_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # 2. Wait for in-flight tasks to drain (30s timeout).
    logger.info("Waiting up to %ds for in-flight tasks to complete…", _SHUTDOWN_DRAIN_TIMEOUT)
    all_sessions = session_registry.all_sessions()
    drain_coros = []
    for session in all_sessions:
        if not session.task_queue.empty():
            drain_coros.append(session.task_queue.join())

    if drain_coros:
        try:
            await asyncio.wait_for(
                asyncio.gather(*drain_coros, return_exceptions=True),
                timeout=_SHUTDOWN_DRAIN_TIMEOUT,
            )
            logger.info("All in-flight tasks drained")
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for in-flight tasks after %ds", _SHUTDOWN_DRAIN_TIMEOUT
            )

    # 3. Stop all ACP runtimes.
    try:
        await acp_registry.stop_all()
        logger.info("ACPRuntimeRegistry: all runtimes stopped")
    except Exception:
        logger.exception("Error stopping ACP runtimes")

    # 4. Flush all memory to disk.
    try:
        await memory_manager.flush_all()
        logger.info("MemoryManager: all contexts flushed")
    except Exception:
        logger.exception("Error flushing memory")

    # 5. Close AclDb.
    try:
        await acl_db.close()
        logger.info("AclDb: closed")
    except Exception:
        logger.exception("Error closing AclDb")

    # 6. Stop StateStore (flush + cancel debounce loop).
    try:
        await state_store.stop()
        logger.info("StateStore: stopped")
    except Exception:
        logger.exception("Error stopping StateStore")

    # 7. Cancel remaining background asyncio tasks.
    for bg_task in (janitor_task,):
        if not bg_task.done():
            bg_task.cancel()
            try:
                await asyncio.wait_for(bg_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    # 8. Release macOS power assertion.
    power_assertion.release()

    # Remove PID file on clean shutdown.
    try:
        _PID_FILE.unlink(missing_ok=True)
        logger.debug("PID file removed: %s", _PID_FILE)
    except Exception:
        pass

    logger.info("nextme shutdown complete")


# ---------------------------------------------------------------------------
# nextme down helper
# ---------------------------------------------------------------------------


def _cmd_down(timeout: int) -> None:
    """Send SIGTERM to the running nextme process identified by the PID file.

    Waits up to *timeout* seconds for the process to exit, then sends SIGKILL
    if it is still alive.  Safe to call when no instance is running.
    """
    import time

    if not _PID_FILE.exists():
        print("nextme is not running (no PID file found).", file=sys.stderr)
        sys.exit(0)

    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError) as exc:
        print(f"Could not read PID file {_PID_FILE}: {exc}", file=sys.stderr)
        sys.exit(1)

    # Verify the process is still alive.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"Process {pid} is no longer running; removing stale PID file.")
        _PID_FILE.unlink(missing_ok=True)
        sys.exit(0)
    except PermissionError:
        # Process exists but belongs to another user — still try to signal.
        pass

    print(f"Sending SIGTERM to nextme (pid={pid})…")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process already exited.")
        _PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    # Wait for process to exit.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"nextme (pid={pid}) stopped.")
            sys.exit(0)
        time.sleep(0.5)

    # Still alive — escalate to SIGKILL.
    print(f"Process {pid} did not exit in {timeout}s; sending SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and launch the async run loop."""
    parser = argparse.ArgumentParser(
        prog="nextme",
        description="Feishu IM × Claude Code Agent Bot",
    )
    subparsers = parser.add_subparsers(dest="command")

    up_parser = subparsers.add_parser("up", help="Start the bot")
    up_parser.add_argument(
        "--directory",
        "-d",
        help="Project directory (overrides config)",
    )
    up_parser.add_argument(
        "--executor",
        "-e",
        default="claude",
        help="Agent executor command (default: 'claude')",
    )
    up_parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    down_parser = subparsers.add_parser("down", help="Stop a running bot instance")
    down_parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        metavar="SECS",
        help="Seconds to wait for graceful exit before SIGKILL (default: 10)",
    )

    args = parser.parse_args()

    if args.command == "up":
        asyncio.run(run(args.directory, args.executor, args.log_level))
    elif args.command == "down":
        _cmd_down(args.timeout)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
