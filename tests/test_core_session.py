"""Tests for nextme.core.session — Session, UserContext, SessionRegistry."""

import asyncio

import pytest

from nextme.config.schema import Project, Settings
from nextme.core.session import Session, SessionRegistry, UserContext
from nextme.protocol.types import PermissionChoice, PermOption, TaskStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    return Project(name="myproj", path=str(tmp_path), executor="claude-code-acp")


@pytest.fixture
def settings():
    return Settings(task_queue_capacity=5)


@pytest.fixture
def session(project, settings):
    return Session(context_id="chat1:user1", project=project, settings=settings)


# ---------------------------------------------------------------------------
# Session tests
# ---------------------------------------------------------------------------


class TestSessionInit:
    def test_initial_status_is_idle(self, project, settings):
        s = Session(context_id="chat:user", project=project, settings=settings)
        assert s.status == TaskStatus.IDLE

    def test_initial_actual_id_is_empty(self, project, settings):
        s = Session(context_id="chat:user", project=project, settings=settings)
        assert s.actual_id == ""

    def test_salt_is_non_empty_string(self, project, settings):
        s = Session(context_id="chat:user", project=project, settings=settings)
        assert isinstance(s.salt, str)
        assert len(s.salt) > 0

    def test_salt_is_different_per_instance(self, project, settings):
        s1 = Session(context_id="c:u", project=project, settings=settings)
        s2 = Session(context_id="c:u", project=project, settings=settings)
        assert s1.salt != s2.salt

    def test_task_queue_capacity(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.task_queue.maxsize == settings.task_queue_capacity

    def test_perm_future_starts_none(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.perm_future is None

    def test_perm_options_starts_empty(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.perm_options == []

    def test_active_task_starts_none(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.active_task is None

    def test_context_id_stored(self, project, settings):
        s = Session(context_id="chat42:user99", project=project, settings=settings)
        assert s.context_id == "chat42:user99"

    def test_project_name_stored(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.project_name == project.name

    def test_executor_stored(self, project, settings):
        s = Session(context_id="c:u", project=project, settings=settings)
        assert s.executor == project.executor


class TestSessionSetPermissionPending:
    async def test_creates_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        future = session.set_permission_pending(options)
        assert future is not None
        assert isinstance(future, asyncio.Future)

    async def test_sets_status_to_waiting_permission(self, session):
        options = [PermOption(index=1, label="Allow")]
        session.set_permission_pending(options)
        assert session.status == TaskStatus.WAITING_PERMISSION

    async def test_returns_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        returned = session.set_permission_pending(options)
        assert returned is session.perm_future

    async def test_cancels_prior_future_when_called_twice(self, session):
        options = [PermOption(index=1, label="Allow")]
        first_future = session.set_permission_pending(options)
        # Call again — first future should be cancelled
        second_future = session.set_permission_pending(options)
        assert first_future.cancelled()
        assert not second_future.done()

    async def test_stores_options(self, session):
        options = [
            PermOption(index=1, label="Allow"),
            PermOption(index=2, label="Deny"),
        ]
        session.set_permission_pending(options)
        assert session.perm_options == options


class TestSessionResolvePermission:
    async def test_sets_result_on_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        future = session.set_permission_pending(options)
        choice = PermissionChoice(request_id="req1", option_index=1)

        session.resolve_permission(choice)

        assert future.done()
        assert future.result() is choice

    async def test_clears_perm_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        session.set_permission_pending(options)
        choice = PermissionChoice(request_id="req1", option_index=1)

        session.resolve_permission(choice)

        assert session.perm_future is None

    async def test_clears_perm_options(self, session):
        options = [PermOption(index=1, label="Allow")]
        session.set_permission_pending(options)
        choice = PermissionChoice(request_id="req1", option_index=1)

        session.resolve_permission(choice)

        assert session.perm_options == []

    async def test_noop_when_no_pending_future(self, session):
        # Should not raise
        choice = PermissionChoice(request_id="req1", option_index=1)
        session.resolve_permission(choice)

    async def test_noop_when_future_already_done(self, session):
        options = [PermOption(index=1, label="Allow")]
        future = session.set_permission_pending(options)
        choice = PermissionChoice(request_id="req1", option_index=1)
        session.resolve_permission(choice)
        # Call again — should be no-op (future is already done)
        session.resolve_permission(choice)


class TestSessionCancelPermission:
    async def test_cancels_pending_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        future = session.set_permission_pending(options)

        session.cancel_permission()

        assert future.cancelled()

    async def test_clears_perm_future(self, session):
        options = [PermOption(index=1, label="Allow")]
        session.set_permission_pending(options)

        session.cancel_permission()

        assert session.perm_future is None

    async def test_clears_perm_options(self, session):
        options = [PermOption(index=1, label="Allow")]
        session.set_permission_pending(options)

        session.cancel_permission()

        assert session.perm_options == []

    def test_noop_when_no_pending_future(self, session):
        # Should not raise
        session.cancel_permission()
        assert session.perm_future is None


class TestSessionRepr:
    def test_repr_contains_context_id(self, session):
        r = repr(session)
        assert "chat1:user1" in r

    def test_repr_contains_project_name(self, session):
        r = repr(session)
        assert "myproj" in r

    def test_repr_is_string(self, session):
        assert isinstance(repr(session), str)


# ---------------------------------------------------------------------------
# UserContext tests
# ---------------------------------------------------------------------------


class TestUserContext:
    def test_get_active_session_returns_none_when_active_project_empty(self, project, settings):
        ctx = UserContext("chat:user")
        assert ctx.get_active_session() is None

    def test_get_active_session_returns_correct_session(self, project, settings):
        ctx = UserContext("chat:user")
        session = ctx.get_or_create_session(project, settings)
        result = ctx.get_active_session()
        assert result is session

    def test_get_or_create_session_creates_new_session(self, project, settings):
        ctx = UserContext("chat:user")
        session = ctx.get_or_create_session(project, settings)
        assert session is not None
        assert isinstance(session, Session)

    def test_get_or_create_session_sets_active_project(self, project, settings):
        ctx = UserContext("chat:user")
        ctx.get_or_create_session(project, settings)
        assert ctx.active_project == project.name

    def test_get_or_create_session_returns_existing_session(self, project, settings):
        ctx = UserContext("chat:user")
        s1 = ctx.get_or_create_session(project, settings)
        s2 = ctx.get_or_create_session(project, settings)
        assert s1 is s2

    def test_get_or_create_session_does_not_recreate(self, project, settings):
        ctx = UserContext("chat:user")
        s1 = ctx.get_or_create_session(project, settings)
        s1.actual_id = "acp-uuid-123"  # mutate to track identity
        s2 = ctx.get_or_create_session(project, settings)
        assert s2.actual_id == "acp-uuid-123"

    def test_multiple_projects_stored_separately(self, tmp_path, settings):
        ctx = UserContext("chat:user")
        proj_a = Project(name="proj-a", path=str(tmp_path / "a"), executor="claude-code-acp")
        proj_b = Project(name="proj-b", path=str(tmp_path / "b"), executor="claude-code-acp")
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()

        sa = ctx.get_or_create_session(proj_a, settings)
        sb = ctx.get_or_create_session(proj_b, settings)

        assert sa is not sb
        assert ctx.active_project == "proj-b"


# ---------------------------------------------------------------------------
# SessionRegistry tests
# ---------------------------------------------------------------------------


class TestSessionRegistry:
    def test_get_instance_returns_singleton(self):
        SessionRegistry._instance = None
        try:
            r1 = SessionRegistry.get_instance()
            r2 = SessionRegistry.get_instance()
            assert r1 is r2
        finally:
            SessionRegistry._instance = None

    def test_get_or_create_creates_new_user_context_for_new_context_id(self):
        registry = SessionRegistry()
        ctx = registry.get_or_create("chat1:user1")
        assert ctx is not None
        assert isinstance(ctx, UserContext)

    def test_get_or_create_returns_existing_user_context(self):
        registry = SessionRegistry()
        ctx1 = registry.get_or_create("chat1:user1")
        ctx2 = registry.get_or_create("chat1:user1")
        assert ctx1 is ctx2

    def test_get_returns_none_for_unknown_id(self):
        registry = SessionRegistry()
        result = registry.get("nonexistent:id")
        assert result is None

    def test_get_returns_context_for_known_id(self):
        registry = SessionRegistry()
        ctx = registry.get_or_create("chat2:user2")
        result = registry.get("chat2:user2")
        assert result is ctx

    def test_all_sessions_returns_all_sessions_across_contexts(self, project, settings):
        registry = SessionRegistry()

        ctx1 = registry.get_or_create("chat1:user1")
        ctx1.get_or_create_session(project, settings)

        ctx2 = registry.get_or_create("chat2:user2")
        ctx2.get_or_create_session(project, settings)

        all_sess = registry.all_sessions()
        assert len(all_sess) == 2

    def test_all_sessions_returns_empty_list_when_no_sessions(self):
        registry = SessionRegistry()
        registry.get_or_create("ctx-no-session")  # UserContext with no sessions
        assert registry.all_sessions() == []

    def test_all_sessions_returns_list_type(self):
        registry = SessionRegistry()
        result = registry.all_sessions()
        assert isinstance(result, list)

    def test_multiple_contexts_stored_independently(self):
        registry = SessionRegistry()
        ctx_a = registry.get_or_create("chatA:userA")
        ctx_b = registry.get_or_create("chatB:userB")
        assert ctx_a is not ctx_b
        assert ctx_a.context_id == "chatA:userA"
        assert ctx_b.context_id == "chatB:userB"
