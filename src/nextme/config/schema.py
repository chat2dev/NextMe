"""Pydantic schemas for all configuration sources."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Project(BaseModel):
    name: str
    path: str
    executor: str = "claude"
    """Agent executor command.  Built-in values:
    ``"claude"`` (DirectClaudeRuntime), ``"cc-acp"`` / ``"coco"`` (ACPRuntime)."""
    executor_args: list[str] = Field(default_factory=list)
    """Extra arguments appended to *executor* when spawning the subprocess.
    Example: ``["acp", "serve"]`` for ``coco acp serve``."""

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class AppConfig(BaseModel):
    """nextme.json — app credentials + project list."""

    app_id: str = ""
    app_secret: str = ""
    projects: list[Project] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)
    """Static chat→project bindings from nextme.json.  Key: chat_id, value: project name."""

    def get_project(self, name: str) -> Optional[Project]:
        for p in self.projects:
            if p.name == name:
                return p
        return None

    def get_binding(self, chat_id: str) -> Optional[str]:
        """Return the project name bound to *chat_id*, or ``None``."""
        return self.bindings.get(chat_id)

    @property
    def default_project(self) -> Optional[Project]:
        return self.projects[0] if self.projects else None


class Settings(BaseModel):
    """~/.nextme/settings.json — behaviour tuning."""

    claude_path: str = "claude"
    acp_idle_timeout_seconds: int = 7200
    task_queue_capacity: int = 1024
    memory_debounce_seconds: int = 30
    context_max_bytes: int = 1_000_000
    context_compression: Literal["zlib", "lzma", "brotli"] = "zlib"
    log_level: str = "INFO"
    progress_debounce_seconds: float = 0.5
    permission_timeout_seconds: float = 300.0


class ProjectState(BaseModel):
    """Persistent state for a single project session."""

    salt: str = ""
    actual_id: str = ""
    executor: str = "claude"


class UserState(BaseModel):
    """Persistent state for a user context (chatID:userID)."""

    last_active_project: str = ""
    projects: dict[str, ProjectState] = Field(default_factory=dict)


class GlobalState(BaseModel):
    """~/.nextme/state.json top-level structure."""

    contexts: dict[str, UserState] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)
    """Dynamic chat→project bindings set via ``/project bind``.  Key: chat_id, value: project name."""
