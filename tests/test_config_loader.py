"""Tests for nextme.config.loader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nextme.config.loader import (
    ConfigLoader,
    _collect_dotenv_overrides,
    _collect_env_overrides,
    _read_json,
)
from nextme.config.schema import AppConfig, Settings


# ---------------------------------------------------------------------------
# _read_json
# ---------------------------------------------------------------------------


class TestReadJson:
    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        result = _read_json(tmp_path / "nonexistent.json")
        assert result == {}

    def test_returns_empty_dict_for_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{{", encoding="utf-8")
        result = _read_json(bad_file)
        assert result == {}

    def test_returns_dict_for_valid_file(self, tmp_path):
        good_file = tmp_path / "good.json"
        payload = {"app_id": "test_id", "app_secret": "test_secret"}
        good_file.write_text(json.dumps(payload), encoding="utf-8")
        result = _read_json(good_file)
        assert result == payload

    def test_returns_empty_dict_for_empty_json_file(self, tmp_path):
        empty_file = tmp_path / "empty.json"
        empty_file.write_text("", encoding="utf-8")
        result = _read_json(empty_file)
        assert result == {}

    def test_returns_nested_dict(self, tmp_path):
        nested_file = tmp_path / "nested.json"
        payload = {"a": {"b": [1, 2, 3]}, "c": True}
        nested_file.write_text(json.dumps(payload), encoding="utf-8")
        result = _read_json(nested_file)
        assert result == payload


# ---------------------------------------------------------------------------
# _collect_env_overrides
# ---------------------------------------------------------------------------


class TestCollectEnvOverrides:
    def test_returns_empty_when_no_nextme_vars(self, monkeypatch):
        # Ensure no NEXTME_* vars are set
        for key in ["NEXTME_APP_ID", "NEXTME_APP_SECRET", "NEXTME_LOG_LEVEL",
                    "NEXTME_CLAUDE_PATH", "NEXTME_ACP_IDLE_TIMEOUT_SECONDS"]:
            monkeypatch.delenv(key, raising=False)
        result = _collect_env_overrides()
        assert result == {}

    def test_app_id_mapping(self, monkeypatch):
        monkeypatch.setenv("NEXTME_APP_ID", "my_app_id")
        result = _collect_env_overrides()
        assert result.get("app_id") == "my_app_id"

    def test_app_secret_mapping(self, monkeypatch):
        monkeypatch.setenv("NEXTME_APP_SECRET", "my_secret")
        result = _collect_env_overrides()
        assert result.get("app_secret") == "my_secret"

    def test_log_level_mapping(self, monkeypatch):
        monkeypatch.setenv("NEXTME_LOG_LEVEL", "DEBUG")
        result = _collect_env_overrides()
        assert result.get("log_level") == "DEBUG"

    def test_claude_path_mapping(self, monkeypatch):
        monkeypatch.setenv("NEXTME_CLAUDE_PATH", "/usr/bin/claude")
        result = _collect_env_overrides()
        assert result.get("claude_path") == "/usr/bin/claude"

    def test_acp_idle_timeout_mapping(self, monkeypatch):
        monkeypatch.setenv("NEXTME_ACP_IDLE_TIMEOUT_SECONDS", "3600")
        result = _collect_env_overrides()
        assert result.get("acp_idle_timeout_seconds") == "3600"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("NEXTME_APP_ID", "id123")
        monkeypatch.setenv("NEXTME_LOG_LEVEL", "WARNING")
        result = _collect_env_overrides()
        assert result["app_id"] == "id123"
        assert result["log_level"] == "WARNING"

    def test_unknown_env_var_not_included(self, monkeypatch):
        monkeypatch.setenv("NEXTME_UNKNOWN_FIELD", "value")
        result = _collect_env_overrides()
        assert "unknown_field" not in result
        assert "NEXTME_UNKNOWN_FIELD" not in result


# ---------------------------------------------------------------------------
# _collect_dotenv_overrides
# ---------------------------------------------------------------------------


class TestCollectDotenvOverrides:
    def test_returns_empty_when_no_dotenv_file(self, tmp_path):
        result = _collect_dotenv_overrides(tmp_path)
        assert result == {}

    def test_reads_app_id_from_dotenv(self, tmp_path, monkeypatch):
        # Ensure env var is not set so dotenv value wins
        monkeypatch.delenv("NEXTME_APP_ID", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("NEXTME_APP_ID=dotenv_id\n", encoding="utf-8")
        result = _collect_dotenv_overrides(tmp_path)
        assert result.get("app_id") == "dotenv_id"

    def test_reads_log_level_from_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEXTME_LOG_LEVEL", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("NEXTME_LOG_LEVEL=DEBUG\n", encoding="utf-8")
        result = _collect_dotenv_overrides(tmp_path)
        assert result.get("log_level") == "DEBUG"

    def test_env_var_takes_priority_over_dotenv(self, tmp_path, monkeypatch):
        # os.environ overrides dotenv value
        monkeypatch.setenv("NEXTME_APP_ID", "env_id")
        dotenv = tmp_path / ".env"
        dotenv.write_text("NEXTME_APP_ID=dotenv_id\n", encoding="utf-8")
        result = _collect_dotenv_overrides(tmp_path)
        # dotenv value should NOT override existing env var
        assert result.get("app_id") is None

    def test_cwd_dotenv_found_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEXTME_APP_SECRET", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("NEXTME_APP_SECRET=from_dotenv\n", encoding="utf-8")
        result = _collect_dotenv_overrides(tmp_path)
        assert result.get("app_secret") == "from_dotenv"

    def test_returns_empty_when_cwd_is_none_and_no_home_dotenv(self, tmp_path, monkeypatch):
        # With cwd=None, it falls back to ~/.nextme/.env
        # We cannot control home dir easily, but we can test cwd path is not searched
        result = _collect_dotenv_overrides(None)
        # Just verify it doesn't raise; result type is dict
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# ConfigLoader.load_app_config
# ---------------------------------------------------------------------------


class TestLoadAppConfig:
    def test_returns_app_config_instance(self, tmp_path):
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert isinstance(result, AppConfig)

    def test_local_nextme_json_is_loaded(self, tmp_path, monkeypatch):
        # Clear env vars to avoid interference
        for key in ["NEXTME_APP_ID", "NEXTME_APP_SECRET"]:
            monkeypatch.delenv(key, raising=False)
        local_cfg = {"app_id": "local_id", "app_secret": "local_secret"}
        (tmp_path / "nextme.json").write_text(json.dumps(local_cfg), encoding="utf-8")
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert result.app_id == "local_id"
        assert result.app_secret == "local_secret"

    def test_local_overrides_user_nextme_json(self, tmp_path, monkeypatch):
        """Verify local nextme.json takes precedence over user-level one."""
        monkeypatch.delenv("NEXTME_APP_ID", raising=False)
        # We can't easily write to ~/.nextme/nextme.json in tests, so we test
        # that local file values are used when present.
        local_cfg = {"app_id": "local_wins"}
        (tmp_path / "nextme.json").write_text(json.dumps(local_cfg), encoding="utf-8")
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert result.app_id == "local_wins"

    def test_env_var_overrides_local_nextme_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXTME_APP_ID", "env_wins")
        local_cfg = {"app_id": "local_id"}
        (tmp_path / "nextme.json").write_text(json.dumps(local_cfg), encoding="utf-8")
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert result.app_id == "env_wins"

    def test_projects_loaded_from_local_json(self, tmp_path, monkeypatch, tmp_path_factory):
        """Local project appears in the result (may be merged with global)."""
        monkeypatch.delenv("NEXTME_APP_ID", raising=False)
        # Isolate from real ~/.nextme/settings.json by redirecting _NEXTME_HOME
        fake_home = tmp_path_factory.mktemp("home")
        monkeypatch.setattr("nextme.config.loader._NEXTME_HOME", fake_home)
        projects = [{"name": "myproj", "path": str(tmp_path)}]
        local_cfg = {"app_id": "id", "projects": projects}
        (tmp_path / "nextme.json").write_text(json.dumps(local_cfg), encoding="utf-8")
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert len(result.projects) == 1
        assert result.projects[0].name == "myproj"

    def test_empty_cwd_dir_returns_defaults(self, tmp_path, monkeypatch):
        """No local nextme.json -> AppConfig with empty defaults."""
        for key in ["NEXTME_APP_ID", "NEXTME_APP_SECRET"]:
            monkeypatch.delenv(key, raising=False)
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert isinstance(result, AppConfig)
        # Projects may or may not be populated depending on ~/.nextme/nextme.json
        # but no exception should be raised

    def test_dotenv_overrides_applied(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEXTME_APP_ID", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("NEXTME_APP_ID=dotenv_app_id\n", encoding="utf-8")
        result = ConfigLoader.load_app_config(cwd=tmp_path)
        assert result.app_id == "dotenv_app_id"


# ---------------------------------------------------------------------------
# ConfigLoader.load_settings
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def test_returns_settings_instance(self):
        result = ConfigLoader.load_settings()
        assert isinstance(result, Settings)

    def test_env_var_overrides_log_level(self, monkeypatch):
        monkeypatch.setenv("NEXTME_LOG_LEVEL", "WARNING")
        result = ConfigLoader.load_settings()
        assert result.log_level == "WARNING"

    def test_env_var_overrides_claude_path(self, monkeypatch):
        monkeypatch.setenv("NEXTME_CLAUDE_PATH", "/custom/claude")
        result = ConfigLoader.load_settings()
        assert result.claude_path == "/custom/claude"

    def test_env_var_overrides_acp_idle_timeout(self, monkeypatch):
        monkeypatch.setenv("NEXTME_ACP_IDLE_TIMEOUT_SECONDS", "1800")
        result = ConfigLoader.load_settings()
        assert result.acp_idle_timeout_seconds == 1800

    def test_defaults_when_no_overrides(self, monkeypatch):
        for key in ["NEXTME_LOG_LEVEL", "NEXTME_CLAUDE_PATH",
                    "NEXTME_ACP_IDLE_TIMEOUT_SECONDS"]:
            monkeypatch.delenv(key, raising=False)
        result = ConfigLoader.load_settings()
        # Defaults should be used (unless ~/.nextme/settings.json overrides them)
        assert isinstance(result, Settings)
        assert result.log_level in ("INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL")


# ---------------------------------------------------------------------------
# Merge behaviour for projects and bindings
# ---------------------------------------------------------------------------


class TestMergeBehaviour:
    """Verify that projects and bindings are merged (not replaced) across sources."""

    def _isolated_loader(self, monkeypatch, tmp_path_factory, global_cfg, local_cfg, local_dir):
        """Helper: write global settings.json and local nextme.json with isolated _NEXTME_HOME."""
        fake_home = tmp_path_factory.mktemp("home")
        monkeypatch.setattr("nextme.config.loader._NEXTME_HOME", fake_home)
        (fake_home / "settings.json").write_text(json.dumps(global_cfg), encoding="utf-8")
        (local_dir / "nextme.json").write_text(json.dumps(local_cfg), encoding="utf-8")
        return ConfigLoader.load_app_config(cwd=local_dir)

    # -- projects --

    def test_projects_merged_local_adds_to_global(self, tmp_path, monkeypatch, tmp_path_factory):
        """Local projects are added to global projects when names differ."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"projects": [{"name": "global-proj", "path": str(tmp_path / "g")}]},
            local_cfg={"projects": [{"name": "local-proj", "path": str(tmp_path / "l")}]},
            local_dir=tmp_path,
        )
        names = {p.name for p in result.projects}
        assert "global-proj" in names
        assert "local-proj" in names
        assert len(result.projects) == 2

    def test_projects_local_wins_on_name_conflict(self, tmp_path, monkeypatch, tmp_path_factory):
        """When the same project name exists in both sources, local path wins."""
        global_path = str(tmp_path / "global_path")
        local_path = str(tmp_path / "local_path")
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"projects": [{"name": "shared", "path": global_path}]},
            local_cfg={"projects": [{"name": "shared", "path": local_path}]},
            local_dir=tmp_path,
        )
        assert len(result.projects) == 1
        assert result.projects[0].name == "shared"
        assert result.projects[0].path == local_path

    def test_projects_global_only_preserved_when_local_has_different_names(
        self, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Global projects that have no name clash with local are kept."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"projects": [
                {"name": "alpha", "path": str(tmp_path / "a")},
                {"name": "beta", "path": str(tmp_path / "b")},
            ]},
            local_cfg={"projects": [{"name": "gamma", "path": str(tmp_path / "g")}]},
            local_dir=tmp_path,
        )
        names = {p.name for p in result.projects}
        assert names == {"alpha", "beta", "gamma"}

    def test_projects_order_global_first_then_local_additions(
        self, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Merged projects list: global entries come first, then local-only additions."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"projects": [{"name": "g1", "path": str(tmp_path / "g1")}]},
            local_cfg={"projects": [{"name": "l1", "path": str(tmp_path / "l1")}]},
            local_dir=tmp_path,
        )
        # g1 was in global dict first, l1 added after
        names = [p.name for p in result.projects]
        assert names.index("g1") < names.index("l1")

    def test_projects_empty_local_preserves_global(self, tmp_path, monkeypatch, tmp_path_factory):
        """Local config with no 'projects' key leaves global projects intact."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"projects": [{"name": "g1", "path": str(tmp_path / "g")}]},
            local_cfg={"app_id": "only_scalars"},
            local_dir=tmp_path,
        )
        assert any(p.name == "g1" for p in result.projects)

    def test_projects_empty_global_uses_local_only(self, tmp_path, monkeypatch, tmp_path_factory):
        """No global projects → only local projects appear."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={},
            local_cfg={"projects": [{"name": "local-only", "path": str(tmp_path / "l")}]},
            local_dir=tmp_path,
        )
        assert len(result.projects) == 1
        assert result.projects[0].name == "local-only"

    # -- bindings --

    def test_bindings_merged_local_adds_to_global(self, tmp_path, monkeypatch, tmp_path_factory):
        """Local bindings are added to global bindings when keys differ."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"bindings": {"chat_global": "repo-G"}},
            local_cfg={"bindings": {"chat_local": "repo-L"}},
            local_dir=tmp_path,
        )
        assert result.bindings == {"chat_global": "repo-G", "chat_local": "repo-L"}

    def test_bindings_local_wins_on_key_conflict(self, tmp_path, monkeypatch, tmp_path_factory):
        """When the same chat_id exists in both, local value wins."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"bindings": {"chat_shared": "repo-global"}},
            local_cfg={"bindings": {"chat_shared": "repo-local"}},
            local_dir=tmp_path,
        )
        assert result.bindings == {"chat_shared": "repo-local"}

    def test_bindings_empty_local_preserves_global(self, tmp_path, monkeypatch, tmp_path_factory):
        """Local config with no 'bindings' key leaves global bindings intact."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={"bindings": {"chat_g": "repo-G"}},
            local_cfg={"app_id": "no_bindings"},
            local_dir=tmp_path,
        )
        assert result.bindings.get("chat_g") == "repo-G"

    def test_bindings_empty_global_uses_local_only(self, tmp_path, monkeypatch, tmp_path_factory):
        """No global bindings → only local bindings appear."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={},
            local_cfg={"bindings": {"chat_l": "repo-L"}},
            local_dir=tmp_path,
        )
        assert result.bindings == {"chat_l": "repo-L"}

    def test_bindings_and_projects_merged_simultaneously(
        self, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Both projects and bindings are merged in the same load call."""
        result = self._isolated_loader(
            monkeypatch, tmp_path_factory,
            global_cfg={
                "projects": [{"name": "g-proj", "path": str(tmp_path / "gp")}],
                "bindings": {"chat_g": "g-proj"},
            },
            local_cfg={
                "projects": [{"name": "l-proj", "path": str(tmp_path / "lp")}],
                "bindings": {"chat_l": "l-proj"},
            },
            local_dir=tmp_path,
        )
        proj_names = {p.name for p in result.projects}
        assert proj_names == {"g-proj", "l-proj"}
        assert result.bindings == {"chat_g": "g-proj", "chat_l": "l-proj"}
