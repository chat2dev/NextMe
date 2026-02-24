"""NextMe core logic module.

Public surface
--------------
path_lock   — PathLockRegistry (per-path asyncio.Lock singleton)
session     — Session, UserContext, SessionRegistry
worker      — SessionWorker
commands    — Meta-command handlers + HELP_COMMANDS
dispatcher  — TaskDispatcher
"""

from .commands import (
    HELP_COMMANDS,
    handle_help,
    handle_new,
    handle_project,
    handle_status,
    handle_stop,
)
from .dispatcher import TaskDispatcher
from .path_lock import PathLockRegistry
from .session import Session, SessionRegistry, UserContext
from .worker import SessionWorker

__all__ = [
    # path_lock
    "PathLockRegistry",
    # session
    "Session",
    "UserContext",
    "SessionRegistry",
    # worker
    "SessionWorker",
    # commands
    "HELP_COMMANDS",
    "handle_new",
    "handle_stop",
    "handle_help",
    "handle_status",
    "handle_project",
    # dispatcher
    "TaskDispatcher",
]
