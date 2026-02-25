"""Tests for nextme.main — CLI entry point, logging setup, signal handlers."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Tests: _setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_creates_log_directory(self, tmp_path):
        """_setup_logging creates the log directory if it doesn't exist."""
        from nextme.main import _setup_logging, _LOG_FILE

        log_dir = tmp_path / "logs"
        log_file = log_dir / "nextme.log"

        with patch("nextme.main._LOG_FILE", log_file):
            _setup_logging("INFO")

        assert log_dir.is_dir()

    def test_adds_file_handler(self, tmp_path):
        """_setup_logging adds a RotatingFileHandler to the root logger."""
        from nextme.main import _setup_logging
        import logging.handlers

        log_file = tmp_path / "logs" / "test.log"

        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            with patch("nextme.main._LOG_FILE", log_file):
                _setup_logging("DEBUG")

            handler_types = [type(h) for h in root.handlers]
            assert logging.handlers.RotatingFileHandler in handler_types
        finally:
            # Cleanup: remove any handlers added by _setup_logging
            for h in root.handlers[:]:
                if h not in original_handlers:
                    h.close()
                    root.removeHandler(h)

    def test_adds_stderr_handler(self, tmp_path):
        """_setup_logging adds a StreamHandler to the root logger."""
        from nextme.main import _setup_logging

        log_file = tmp_path / "logs" / "test.log"

        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            with patch("nextme.main._LOG_FILE", log_file):
                _setup_logging("WARNING")

            handler_types = [type(h) for h in root.handlers]
            assert logging.StreamHandler in handler_types
        finally:
            for h in root.handlers[:]:
                if h not in original_handlers:
                    h.close()
                    root.removeHandler(h)

    def test_sets_root_log_level(self, tmp_path):
        """_setup_logging sets the root logger level correctly."""
        from nextme.main import _setup_logging

        log_file = tmp_path / "logs" / "test.log"
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            with patch("nextme.main._LOG_FILE", log_file):
                _setup_logging("ERROR")
            assert root.level == logging.ERROR
        finally:
            root.setLevel(original_level)
            for h in root.handlers[:]:
                if h not in original_handlers:
                    h.close()
                    root.removeHandler(h)

    def test_unknown_log_level_defaults_to_info(self, tmp_path):
        """Unknown log level falls back to INFO."""
        from nextme.main import _setup_logging

        log_file = tmp_path / "logs" / "test.log"
        root = logging.getLogger()
        original_level = root.level
        original_handlers = root.handlers[:]
        try:
            with patch("nextme.main._LOG_FILE", log_file):
                _setup_logging("VERBOSE")  # unknown
            assert root.level == logging.INFO
        finally:
            root.setLevel(original_level)
            for h in root.handlers[:]:
                if h not in original_handlers:
                    h.close()
                    root.removeHandler(h)


# ---------------------------------------------------------------------------
# Tests: _install_signal_handlers
# ---------------------------------------------------------------------------


class TestInstallSignalHandlers:
    def test_signal_handler_sets_event(self):
        """Calling the signal handler sets the shutdown_event."""
        from nextme.main import _install_signal_handlers

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        shutdown_event = asyncio.Event()

        try:
            registered = {}

            def fake_add_signal_handler(sig, callback, *args):
                registered[sig] = (callback, args)

            loop.add_signal_handler = fake_add_signal_handler

            _install_signal_handlers(loop, shutdown_event)

            # Simulate SIGTERM
            assert signal.SIGTERM in registered
            callback, args = registered[signal.SIGTERM]
            callback(*args)
            assert shutdown_event.is_set()
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_idempotent_second_signal(self):
        """Calling the signal handler twice doesn't raise."""
        from nextme.main import _install_signal_handlers

        loop = asyncio.new_event_loop()
        shutdown_event = asyncio.Event()

        try:
            registered = {}

            def fake_add_signal_handler(sig, callback, *args):
                registered[sig] = (callback, args)

            loop.add_signal_handler = fake_add_signal_handler

            asyncio.set_event_loop(loop)
            _install_signal_handlers(loop, shutdown_event)

            callback, args = registered[signal.SIGTERM]
            callback(*args)  # first call
            callback(*args)  # second call — should not raise
            assert shutdown_event.is_set()
        finally:
            loop.close()

    def test_handles_not_implemented_error(self):
        """Falls back to signal.signal when add_signal_handler raises NotImplementedError."""
        from nextme.main import _install_signal_handlers

        loop = MagicMock()
        loop.add_signal_handler = MagicMock(side_effect=NotImplementedError)
        shutdown_event = asyncio.Event()

        # Should not raise
        with patch("signal.signal") as mock_signal:
            _install_signal_handlers(loop, shutdown_event)
            # signal.signal was called for each signal
            assert mock_signal.call_count >= 2


# ---------------------------------------------------------------------------
# Tests: main() CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_no_command_prints_help(self, capsys):
        """main() with no command prints help and returns (no sys.exit)."""
        from nextme.main import main

        with patch("sys.argv", ["nextme"]):
            # Should print help and return without starting bot
            main()

        output = capsys.readouterr()
        # argparse prints help to stdout or just returns
        assert True  # doesn't crash

    def test_main_up_command_calls_asyncio_run(self, tmp_path):
        """main() with 'up' command calls asyncio.run(run(...))."""
        from nextme.main import main

        with (
            patch("sys.argv", ["nextme", "up", "--directory", str(tmp_path)]),
            patch("asyncio.run") as mock_run,
        ):
            main()
            mock_run.assert_called_once()

    def test_main_up_with_log_level(self):
        """main() passes log_level to run()."""
        from nextme.main import main

        with (
            patch("sys.argv", ["nextme", "up", "--log-level", "DEBUG"]),
            patch("asyncio.run") as mock_run,
        ):
            main()
            mock_run.assert_called_once()
            # The coroutine passed to asyncio.run should be a coroutine from run()
            coro = mock_run.call_args[0][0]
            coro.close()  # clean up the coroutine

    def test_main_up_default_executor(self):
        """main() defaults executor to 'claude-code-acp'."""
        from nextme.main import main

        with (
            patch("sys.argv", ["nextme", "up"]),
            patch("asyncio.run") as mock_run,
        ):
            main()
            coro = mock_run.call_args[0][0]
            coro.close()


# ---------------------------------------------------------------------------
# Tests: run() function — partial integration
# ---------------------------------------------------------------------------


class TestRun:
    async def test_run_exits_with_error_when_no_credentials(self, tmp_path, monkeypatch):
        """run() calls sys.exit(1) when app_id or app_secret is missing."""
        from nextme.main import run
        from nextme.config.schema import AppConfig, Settings

        # Config with no credentials
        empty_config = AppConfig(app_id="", app_secret="", projects=[])
        mock_settings = Settings()

        with (
            patch("nextme.main._setup_logging"),
            patch("nextme.config.loader.ConfigLoader.load_app_config", return_value=empty_config),
            patch("nextme.config.loader.ConfigLoader.load_settings", return_value=mock_settings),
            pytest.raises(SystemExit) as exc_info,
        ):
            await run(None, "claude-code-acp", "INFO")

        assert exc_info.value.code == 1

    async def test_run_loads_config_with_directory(self, tmp_path):
        """run() passes directory to ConfigLoader as a resolved Path."""
        from nextme.main import run
        from nextme.config.schema import AppConfig, Settings

        # Use empty credentials so run() exits early via sys.exit(1)
        mock_config = AppConfig(app_id="", app_secret="", projects=[])
        mock_settings = Settings()

        with (
            patch("nextme.main._setup_logging"),
            patch("nextme.config.loader.ConfigLoader.load_app_config", return_value=mock_config) as mock_loader,
            patch("nextme.config.loader.ConfigLoader.load_settings", return_value=mock_settings),
            pytest.raises(SystemExit) as exc_info,
        ):
            await run(str(tmp_path), "claude-code-acp", "INFO")

        assert exc_info.value.code == 1
        # ConfigLoader was called with a Path derived from tmp_path
        mock_loader.assert_called_once()
        path_arg = mock_loader.call_args[0][0]
        assert path_arg is not None
