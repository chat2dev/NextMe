"""Multi-source configuration loader with priority merging.

Priority order (low → high):
  1. ~/.nextme/settings.json
  2. {cwd}/nextme.json
  3. .env file  (python-dotenv)
  4. NEXTME_* environment variables
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .schema import AppConfig, Settings

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NEXTME_HOME = Path("~/.nextme").expanduser()

# Map of environment-variable suffix → AppConfig / Settings field name.
# Both AppConfig and Settings share the same env-var namespace; the loader
# knows which field belongs to which model.
_ENV_MAP: dict[str, str] = {
    "NEXTME_APP_ID": "app_id",
    "NEXTME_APP_SECRET": "app_secret",
    "NEXTME_LOG_LEVEL": "log_level",
    "NEXTME_CLAUDE_PATH": "claude_path",
    "NEXTME_ACP_IDLE_TIMEOUT_SECONDS": "acp_idle_timeout_seconds",
}

# Fields that belong to AppConfig (vs Settings)
_APP_CONFIG_FIELDS: frozenset[str] = frozenset(AppConfig.model_fields.keys())
_SETTINGS_FIELDS: frozenset[str] = frozenset(Settings.model_fields.keys())


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file and return its contents as a dict.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _collect_env_overrides() -> dict[str, Any]:
    """Return a flat dict of recognised NEXTME_* overrides from os.environ."""
    overrides: dict[str, Any] = {}
    for env_key, field_name in _ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is not None:
            overrides[field_name] = value
    return overrides


def _collect_dotenv_overrides(cwd: Path | None) -> dict[str, Any]:
    """Load .env from *cwd* (if provided) and return recognised overrides.

    python-dotenv is used so that existing ``os.environ`` values are *not*
    overwritten — we read the file values directly without side-effects.
    """
    search_dirs: list[Path] = []
    if cwd is not None:
        search_dirs.append(cwd)
    # Also look in the home directory as a fallback
    search_dirs.append(_NEXTME_HOME)

    overrides: dict[str, Any] = {}
    for directory in search_dirs:
        dotenv_path = directory / ".env"
        if dotenv_path.is_file():
            raw = dotenv_values(dotenv_path)
            for env_key, field_name in _ENV_MAP.items():
                if env_key in raw and raw[env_key] is not None:
                    # os.environ takes priority — only use dotenv value when
                    # the key is NOT already set in the real environment.
                    if env_key not in os.environ:
                        overrides[field_name] = raw[env_key]
            # Use only the first .env found (cwd wins over home)
            break

    return overrides


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ConfigLoader:
    """Load and merge configuration from multiple sources."""

    # ------------------------------------------------------------------
    # AppConfig
    # ------------------------------------------------------------------

    @staticmethod
    def load_app_config(cwd: Path | None = None) -> AppConfig:
        """Load and merge :class:`AppConfig` from multiple sources.

        Merge order (each later source overrides earlier values):

        1. ``~/.nextme/settings.json`` — user-global defaults (shared with Settings)
        2. ``{cwd}/nextme.json``        — project-local overrides
        3. ``.env`` file                — dotenv key/value pairs
        4. ``NEXTME_*`` env vars        — highest-priority overrides
        """
        # --- Layer 1: user-global settings.json --------------------------
        merged: dict[str, Any] = _read_json(_NEXTME_HOME / "settings.json")

        # --- Layer 2: cwd-local nextme.json ------------------------------
        if cwd is not None:
            local_data = _read_json(Path(cwd) / "nextme.json")
        else:
            local_data = _read_json(Path.cwd() / "nextme.json")

        # Merge rules:
        #   - projects: union by name; local entry wins on name conflict.
        #   - bindings: dict merge; local entry wins on key conflict.
        #   - all other fields: local value replaces global value.
        for key, value in local_data.items():
            if key == "projects" and "projects" in merged:
                global_by_name = {p["name"]: p for p in merged["projects"] if isinstance(p, dict)}
                for proj in value:
                    if isinstance(proj, dict):
                        global_by_name[proj["name"]] = proj
                merged["projects"] = list(global_by_name.values())
            elif key == "bindings" and "bindings" in merged:
                merged["bindings"] = {**merged["bindings"], **value}
            else:
                merged[key] = value

        # --- Layer 3: dotenv overrides (AppConfig fields only) -----------
        dotenv_overrides = _collect_dotenv_overrides(cwd)
        for field_name, value in dotenv_overrides.items():
            if field_name in _APP_CONFIG_FIELDS:
                merged[field_name] = value

        # --- Layer 4: environment variable overrides ---------------------
        env_overrides = _collect_env_overrides()
        for field_name, value in env_overrides.items():
            if field_name in _APP_CONFIG_FIELDS:
                merged[field_name] = value

        return AppConfig.model_validate(merged)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @staticmethod
    def load_settings() -> Settings:
        """Load :class:`Settings` from ``~/.nextme/settings.json`` + env vars.

        Merge order:

        1. ``~/.nextme/settings.json`` — persisted user preferences
        2. ``.env`` file               — dotenv key/value pairs
        3. ``NEXTME_*`` env vars       — highest-priority overrides
        """
        # --- Layer 1: settings.json --------------------------------------
        merged: dict[str, Any] = _read_json(_NEXTME_HOME / "settings.json")

        # --- Layer 2: dotenv overrides (Settings fields only) ------------
        dotenv_overrides = _collect_dotenv_overrides(cwd=None)
        for field_name, value in dotenv_overrides.items():
            if field_name in _SETTINGS_FIELDS:
                merged[field_name] = value

        # --- Layer 3: environment variable overrides ---------------------
        env_overrides = _collect_env_overrides()
        for field_name, value in env_overrides.items():
            if field_name in _SETTINGS_FIELDS:
                merged[field_name] = value

        return Settings.model_validate(merged)
