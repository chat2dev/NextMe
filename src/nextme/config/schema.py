"""Pydantic schemas for all configuration sources."""

from __future__ import annotations

from datetime import datetime
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
    task_timeout_seconds: int = Field(default=7200)
    """Maximum wall-clock execution time for a single task (seconds).
    When exceeded the task is aborted, the runtime subprocess is restarted,
    the session context is preserved, and the user receives a timeout notification.
    Set to 0 to disable the limit.  Default: 7200 (2 hours)."""

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
    memory_max_facts: int = 100  # enforced by MemoryManager.add_fact (evicts lowest-confidence facts)
    context_max_bytes: int = 1_000_000
    context_compression: Literal["zlib", "lzma", "brotli"] = "zlib"
    log_level: str = "INFO"
    progress_debounce_seconds: float = 0.5
    permission_auto_approve: bool = False
    """Auto-approve permission requests immediately without waiting for user input.

    When ``True``, the runtime responds to ``session/request_permission`` with
    the first session-wide allow option (``session_level_allow``) immediately
    and sends the user an informational card (no buttons).

    Enable this for ACP executors with short internal permission timeouts
    (e.g. ``coco acp serve``, which times out in ~2–4 s before the user can
    click a Feishu card on mobile).  The direct ``claude`` executor blocks
    indefinitely and does **not** need this setting.
    """
    streaming_enabled: bool = True
    """Enable cardkit streaming mode for real-time typewriter updates.
    When True (default) the bot uses CardKit PUT /cards/:card_id for live
    full-card updates; when False it falls back to debounced PATCH updates."""
    admin_users: list[str] = Field(default_factory=list)
    """Feishu open_ids of super-admins. Hot-reloadable via ./reload.sh (SIGHUP).
    These users bypass all ACL checks and can approve owner applications."""
    max_active_threads_per_chat: int = 8
    """每个群聊最多同时活���的话题数。超出则排队等候直到有话题关闭。"""


class ThreadRecord(BaseModel):
    """Persistent metadata for one Feishu thread session."""

    chat_id: str
    thread_root_id: str  # Feishu root message_id，话题唯一标识
    project_name: str
    created_at: datetime = Field(default_factory=datetime.now)
    last_active_at: datetime = Field(default_factory=datetime.now)


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
    thread_records: dict[str, ThreadRecord] = Field(default_factory=dict)
    # key: "chat_id:thread_root_id"
