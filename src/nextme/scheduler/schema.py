"""Pydantic models for the scheduler subsystem."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, computed_field


class ScheduleType(str, Enum):
    ONCE = "once"          # run at a specific datetime, then mark done
    INTERVAL = "interval"  # run every N seconds
    CRON = "cron"          # run on cron expression (requires croniter)


class ScheduledTask(BaseModel):
    id: str
    chat_id: str
    creator_open_id: str
    prompt: str
    schedule_type: ScheduleType
    schedule_value: str          # ISO datetime / seconds / cron expression
    next_run_at: datetime        # UTC, when this task should next fire
    last_run_at: datetime | None = None
    status: Literal["active", "paused", "done"] = "active"
    run_count: int = 0
    max_runs: int | None = None
    created_at: datetime | None = None
    project_name: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    @computed_field  # type: ignore[misc]
    @property
    def session_id(self) -> str:
        return f"{self.chat_id}:{self.creator_open_id}"

    model_config = {"frozen": False}


class TaskRunLog(BaseModel):
    id: int | None = None
    task_id: str
    run_at: datetime
    success: bool
    error_message: str | None = None
    duration_seconds: float | None = None
