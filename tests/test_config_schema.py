"""Tests for nextme.config.schema."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nextme.config.schema import (
    AppConfig,
    GlobalState,
    Project,
    ProjectState,
    Settings,
    UserState,
)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class TestProjectExpandPath:
    def test_tilde_is_expanded(self):
        p = Project(name="home-proj", path="~/foo/bar")
        home = Path("~/foo/bar").expanduser().resolve()
        assert p.path == str(home)

    def test_relative_path_becomes_absolute(self):
        p = Project(name="rel-proj", path="relative/path")
        expected = str(Path("relative/path").expanduser().resolve())
        assert p.path == expected
        assert os.path.isabs(p.path)

    def test_absolute_path_unchanged(self, tmp_path):
        abs_path = str(tmp_path)
        p = Project(name="abs-proj", path=abs_path)
        assert p.path == abs_path

    def test_default_executor(self):
        p = Project(name="p", path="/tmp")
        assert p.executor == "claude"

    def test_custom_executor(self):
        p = Project(name="p", path="/tmp", executor="claude-code-acp")
        assert p.executor == "claude-code-acp"


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------


class TestAppConfigGetProject:
    def setup_method(self):
        self.proj_a = Project(name="alpha", path="/tmp")
        self.proj_b = Project(name="beta", path="/tmp")
        self.config = AppConfig(
            app_id="id",
            app_secret="secret",
            projects=[self.proj_a, self.proj_b],
        )

    def test_returns_correct_project_by_name(self):
        result = self.config.get_project("alpha")
        assert result is not None
        assert result.name == "alpha"

    def test_returns_second_project(self):
        result = self.config.get_project("beta")
        assert result is not None
        assert result.name == "beta"

    def test_returns_none_for_missing_name(self):
        result = self.config.get_project("nonexistent")
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = self.config.get_project("")
        assert result is None


class TestAppConfigDefaultProject:
    def test_returns_first_project(self):
        p1 = Project(name="first", path="/tmp")
        p2 = Project(name="second", path="/tmp")
        config = AppConfig(projects=[p1, p2])
        assert config.default_project is not None
        assert config.default_project.name == "first"

    def test_returns_none_for_empty_list(self):
        config = AppConfig()
        assert config.default_project is None

    def test_returns_single_project(self):
        p = Project(name="only", path="/tmp")
        config = AppConfig(projects=[p])
        assert config.default_project.name == "only"


class TestAppConfigDefaults:
    def test_default_app_id(self):
        config = AppConfig()
        assert config.app_id == ""

    def test_default_app_secret(self):
        config = AppConfig()
        assert config.app_secret == ""

    def test_default_projects_empty(self):
        config = AppConfig()
        assert config.projects == []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_claude_path(self):
        s = Settings()
        assert s.claude_path == "claude"

    def test_default_acp_idle_timeout_seconds(self):
        s = Settings()
        assert s.acp_idle_timeout_seconds == 7200

    def test_default_task_queue_capacity(self):
        s = Settings()
        assert s.task_queue_capacity == 1024

    def test_default_memory_debounce_seconds(self):
        s = Settings()
        assert s.memory_debounce_seconds == 30

    def test_default_context_max_bytes(self):
        s = Settings()
        assert s.context_max_bytes == 1_000_000

    def test_default_context_compression(self):
        s = Settings()
        assert s.context_compression == "zlib"

    def test_default_log_level(self):
        s = Settings()
        assert s.log_level == "INFO"

    def test_default_progress_debounce_seconds(self):
        s = Settings()
        assert s.progress_debounce_seconds == 1.0

    def test_default_permission_timeout_seconds(self):
        s = Settings()
        assert s.permission_timeout_seconds == 300.0

    def test_context_compression_valid_values(self):
        for val in ("zlib", "lzma", "brotli"):
            s = Settings(context_compression=val)
            assert s.context_compression == val

    def test_override_fields(self):
        s = Settings(
            claude_path="/usr/local/bin/claude",
            acp_idle_timeout_seconds=3600,
            task_queue_capacity=512,
            memory_debounce_seconds=10,
            log_level="DEBUG",
            progress_debounce_seconds=1.5,
            permission_timeout_seconds=60.0,
        )
        assert s.claude_path == "/usr/local/bin/claude"
        assert s.acp_idle_timeout_seconds == 3600
        assert s.task_queue_capacity == 512
        assert s.memory_debounce_seconds == 10
        assert s.log_level == "DEBUG"
        assert s.progress_debounce_seconds == 1.5
        assert s.permission_timeout_seconds == 60.0


# ---------------------------------------------------------------------------
# ProjectState
# ---------------------------------------------------------------------------


class TestProjectState:
    def test_defaults(self):
        ps = ProjectState()
        assert ps.salt == ""
        assert ps.actual_id == ""
        assert ps.executor == "claude"

    def test_custom_values(self):
        ps = ProjectState(salt="abc123", actual_id="real-id", executor="custom")
        assert ps.salt == "abc123"
        assert ps.actual_id == "real-id"
        assert ps.executor == "custom"


# ---------------------------------------------------------------------------
# UserState
# ---------------------------------------------------------------------------


class TestUserState:
    def test_defaults(self):
        us = UserState()
        assert us.last_active_project == ""
        assert us.projects == {}

    def test_custom_values(self):
        ps = ProjectState(salt="s", actual_id="a")
        us = UserState(last_active_project="myproj", projects={"myproj": ps})
        assert us.last_active_project == "myproj"
        assert "myproj" in us.projects

    def test_projects_are_independent(self):
        """Each UserState instance gets its own projects dict."""
        us1 = UserState()
        us2 = UserState()
        us1.projects["x"] = ProjectState()
        assert "x" not in us2.projects


# ---------------------------------------------------------------------------
# GlobalState
# ---------------------------------------------------------------------------


class TestGlobalState:
    def test_defaults(self):
        gs = GlobalState()
        assert gs.contexts == {}

    def test_custom_contexts(self):
        us = UserState(last_active_project="proj")
        gs = GlobalState(contexts={"chat:user": us})
        assert "chat:user" in gs.contexts
        assert gs.contexts["chat:user"].last_active_project == "proj"

    def test_contexts_are_independent(self):
        """Each GlobalState instance gets its own contexts dict."""
        gs1 = GlobalState()
        gs2 = GlobalState()
        gs1.contexts["k"] = UserState()
        assert "k" not in gs2.contexts
