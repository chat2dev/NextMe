"""Core protocol types shared across all modules."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional


class TaskStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    WAITING_LOCK = "waiting_lock"
    EXECUTING = "executing"
    WAITING_PERMISSION = "waiting_permission"
    DONE = "done"
    CANCELED = "canceled"


class ReplyType(str, Enum):
    MARKDOWN = "markdown"
    CARD = "card"
    REACTION = "reaction"
    FILE = "file"


@dataclass
class Reply:
    type: ReplyType
    content: str
    title: str = ""
    template: str = "blue"          # card color template
    reasoning: str = ""             # collapsible reasoning panel
    is_intermediate: bool = False   # progress update (debounced)
    debug_session_id: str = ""
    file_path: str = ""


@dataclass
class PermOption:
    index: int
    label: str
    description: str = ""


@dataclass
class Task:
    id: str                             # UUID
    content: str                        # user message text
    session_id: str                     # "chatID:userID"
    reply_fn: Callable                  # async callback: (Reply) -> None
    created_at: datetime = field(default_factory=datetime.now)
    timeout: timedelta = field(default_factory=lambda: timedelta(hours=8))
    canceled: bool = False
    was_queued: bool = False            # set when task waited in queue


# Emitted by ACPRuntime callbacks
@dataclass
class ProgressEvent:
    session_id: str
    delta: str
    tool_name: str = ""


@dataclass
class PermissionRequest:
    session_id: str
    request_id: str
    description: str
    options: list[PermOption]


@dataclass
class PermissionChoice:
    request_id: str
    option_index: int       # 1-based, matching user reply
    option_label: str = ""
