"""Session, UserContext, and SessionRegistry.

Session
-------
Represents one user's active project session.  Holds the task queue, the
active task reference, and the optional pending-permission future.

UserContext
-----------
Groups all sessions belonging to one ``context_id`` (``chatID:userID``).

SessionRegistry
---------------
Global singleton mapping ``context_id`` → :class:`UserContext`.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import ClassVar, Optional

from ..config.schema import Project, Settings
from ..protocol.types import PermissionChoice, PermOption, Task, TaskStatus

logger = logging.getLogger(__name__)


class Session:
    """Represents one user's active project session.

    Attributes:
        context_id: ``"chatID:userID"`` composite key.
        project_name: Name of the project this session is attached to.
        project_path: Absolute filesystem path to the project directory.
        executor: ACP subprocess executable (e.g. ``"claude-code-acp"``).
        salt: Random string used for deterministic session ID generation.
        actual_id: ACP-assigned session UUID (set after the first execute).
        status: Current :class:`~nextme.protocol.types.TaskStatus`.
        task_queue: Bounded async queue of pending :class:`~nextme.protocol.types.Task` objects.
        pending_tasks: Tasks that have been enqueued but not yet started.
        active_task: The :class:`~nextme.protocol.types.Task` currently executing, if any.
        perm_future: Pending permission future (set while waiting for user input).
        perm_options: Options associated with the pending permission request.
    """

    def __init__(self, context_id: str, project: Project, settings: Settings) -> None:
        self.context_id: str = context_id
        self.project_name: str = project.name
        self.project_path: Path = Path(project.path)
        self.executor: str = project.executor
        self.executor_args: list[str] = list(project.executor_args)
        self.salt: str = secrets.token_hex(8)
        self.actual_id: str = ""
        self.status: TaskStatus = TaskStatus.IDLE
        self.task_queue: asyncio.Queue[Task] = asyncio.Queue(
            maxsize=settings.task_queue_capacity
        )
        self.pending_tasks: list[Task] = []
        self.active_task: Optional[Task] = None
        self.perm_future: Optional[asyncio.Future[PermissionChoice]] = None
        self.perm_options: list[PermOption] = []

        logger.debug(
            "Session created: context_id=%r project=%r path=%s executor=%r",
            context_id,
            project.name,
            project.path,
            project.executor,
        )

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def set_permission_pending(
        self, options: list[PermOption]
    ) -> asyncio.Future[PermissionChoice]:
        """Create the permission future, store options, and return the future.

        Replaces any previously pending permission future (the old one is
        cancelled first).

        Args:
            options: The list of permission options to present to the user.

        Returns:
            An :class:`asyncio.Future` that will be resolved when the user
            replies via :meth:`resolve_permission`.
        """
        self.cancel_permission()

        loop = asyncio.get_event_loop()
        self.perm_future = loop.create_future()
        self.perm_options = list(options)
        self.status = TaskStatus.WAITING_PERMISSION
        logger.debug(
            "Session[%s]: permission pending (%d option(s))",
            self.context_id,
            len(options),
        )
        return self.perm_future

    def resolve_permission(self, choice: PermissionChoice) -> None:
        """Set the result on the pending permission future.

        No-op if there is no pending permission.

        Args:
            choice: The user's selected :class:`~nextme.protocol.types.PermissionChoice`.
        """
        if self.perm_future is None or self.perm_future.done():
            logger.debug(
                "Session[%s]: resolve_permission called with no pending future",
                self.context_id,
            )
            return
        logger.debug(
            "Session[%s]: resolving permission with index=%d",
            self.context_id,
            choice.option_index,
        )
        self.perm_future.set_result(choice)
        self.perm_future = None
        self.perm_options = []

    def cancel_permission(self) -> None:
        """Cancel the pending permission future, if any."""
        if self.perm_future is not None and not self.perm_future.done():
            logger.debug(
                "Session[%s]: cancelling pending permission future", self.context_id
            )
            self.perm_future.cancel()
        self.perm_future = None
        self.perm_options = []

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Session(context_id={self.context_id!r}, "
            f"project={self.project_name!r}, "
            f"status={self.status.value!r})"
        )


class UserContext:
    """All sessions belonging to one ``context_id`` (``chatID:userID``).

    Attributes:
        context_id: The composite ``"chatID:userID"`` key.
        active_project: Name of the currently active project.
        sessions: Mapping of ``project_name`` → :class:`Session`.
    """

    def __init__(self, context_id: str) -> None:
        self.context_id: str = context_id
        self.active_project: str = ""
        self.sessions: dict[str, Session] = {}

    # ------------------------------------------------------------------
    # Session access
    # ------------------------------------------------------------------

    def get_active_session(self) -> Optional[Session]:
        """Return the session for the active project, or ``None``."""
        if not self.active_project:
            return None
        return self.sessions.get(self.active_project)

    def get_or_create_session(
        self, project: Project, settings: Settings
    ) -> Session:
        """Return the existing session for *project*, or create a new one.

        Also sets *project* as the active project.

        Args:
            project: Project configuration.
            settings: Application settings.

        Returns:
            A :class:`Session` for the given project.
        """
        if project.name not in self.sessions:
            logger.info(
                "UserContext[%s]: creating session for project %r",
                self.context_id,
                project.name,
            )
            self.sessions[project.name] = Session(
                context_id=self.context_id,
                project=project,
                settings=settings,
            )
        self.active_project = project.name
        return self.sessions[project.name]

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"UserContext(context_id={self.context_id!r}, "
            f"active_project={self.active_project!r}, "
            f"sessions={list(self.sessions.keys())!r})"
        )


class SessionRegistry:
    """Global singleton mapping ``context_id`` → :class:`UserContext`.

    Usage::

        registry = SessionRegistry.get_instance()
        user_ctx = registry.get_or_create("oc_abc123:ou_xyz789")
    """

    _instance: ClassVar[Optional[SessionRegistry]] = None

    def __init__(self) -> None:
        self._contexts: dict[str, UserContext] = {}

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> SessionRegistry:
        """Return the global singleton, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
            logger.debug("SessionRegistry: singleton created")
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self, context_id: str) -> UserContext:
        """Return the :class:`UserContext` for *context_id*, creating it if absent.

        Args:
            context_id: Composite ``"chatID:userID"`` identifier.

        Returns:
            A :class:`UserContext` for the given *context_id*.
        """
        if context_id not in self._contexts:
            logger.debug(
                "SessionRegistry: creating UserContext for %r", context_id
            )
            self._contexts[context_id] = UserContext(context_id)
        return self._contexts[context_id]

    def get(self, context_id: str) -> Optional[UserContext]:
        """Return the :class:`UserContext` for *context_id*, or ``None``.

        Args:
            context_id: Composite ``"chatID:userID"`` identifier.

        Returns:
            The :class:`UserContext`, or ``None`` if not found.
        """
        return self._contexts.get(context_id)

    def all_sessions(self) -> list[Session]:
        """Return all :class:`Session` objects across every user context."""
        sessions: list[Session] = []
        for user_ctx in self._contexts.values():
            sessions.extend(user_ctx.sessions.values())
        return sessions
