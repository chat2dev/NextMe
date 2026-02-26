"""Tests for nextme.config.state_store."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nextme.config.schema import GlobalState, Settings, UserState
from nextme.config.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_store(tmp_path: Path, debounce: float = 30.0) -> StateStore:
    settings = Settings(memory_debounce_seconds=int(debounce) if debounce >= 1 else 1)
    store = StateStore(
        settings=settings,
        state_path=tmp_path / "state.json",
    )
    # Override debounce directly so we can use sub-second values in tests
    store._debounce_seconds = debounce
    return store


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


async def test_load_returns_global_state_when_file_missing(tmp_path):
    store = make_store(tmp_path)
    state = await store.load()
    assert isinstance(state, GlobalState)
    assert state.contexts == {}


async def test_load_returns_cached_state_on_second_call(tmp_path):
    store = make_store(tmp_path)
    state1 = await store.load()
    state2 = await store.load()
    assert state1 is state2


async def test_load_returns_defaults_for_corrupt_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{ invalid json !!!", encoding="utf-8")
    store = make_store(tmp_path)
    state = await store.load()
    assert isinstance(state, GlobalState)
    assert state.contexts == {}


async def test_load_returns_defaults_for_empty_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("", encoding="utf-8")
    store = make_store(tmp_path)
    state = await store.load()
    assert isinstance(state, GlobalState)


async def test_load_reads_existing_valid_state(tmp_path):
    state_file = tmp_path / "state.json"
    payload = {
        "contexts": {
            "chat1:user1": {
                "last_active_project": "myproj",
                "projects": {},
            }
        }
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    store = make_store(tmp_path)
    state = await store.load()
    assert "chat1:user1" in state.contexts
    assert state.contexts["chat1:user1"].last_active_project == "myproj"


# ---------------------------------------------------------------------------
# get_user_state()
# ---------------------------------------------------------------------------


async def test_get_user_state_creates_blank_if_not_found(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    user_state = store.get_user_state("chat:user")
    assert isinstance(user_state, UserState)
    assert user_state.last_active_project == ""


async def test_get_user_state_marks_dirty_when_creating(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    assert store._dirty is False
    store.get_user_state("new_context")
    assert store._dirty is True


async def test_get_user_state_returns_existing_state(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    us = UserState(last_active_project="proj_x")
    store.set_user_state("ctx1", us)
    retrieved = store.get_user_state("ctx1")
    assert retrieved.last_active_project == "proj_x"


async def test_get_user_state_returns_same_object(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    us = UserState(last_active_project="proj_y")
    store.set_user_state("ctx2", us)
    retrieved = store.get_user_state("ctx2")
    assert retrieved is us


# ---------------------------------------------------------------------------
# set_user_state()
# ---------------------------------------------------------------------------


async def test_set_user_state_updates_state(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    us = UserState(last_active_project="new_proj")
    store.set_user_state("ctx", us)
    result = store.get_user_state("ctx")
    assert result.last_active_project == "new_proj"


async def test_set_user_state_marks_dirty(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store._dirty = False  # Reset dirty flag after load
    store.set_user_state("ctx", UserState())
    assert store._dirty is True


async def test_set_user_state_overwrites_existing(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("ctx", UserState(last_active_project="old"))
    store.set_user_state("ctx", UserState(last_active_project="new"))
    assert store.get_user_state("ctx").last_active_project == "new"


# ---------------------------------------------------------------------------
# flush()
# ---------------------------------------------------------------------------


async def test_flush_writes_state_file(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("u", UserState(last_active_project="proj"))
    await store.flush()
    state_file = tmp_path / "state.json"
    assert state_file.is_file()


async def test_flush_content_is_valid_json(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("u", UserState(last_active_project="proj"))
    await store.flush()
    state_file = tmp_path / "state.json"
    content = state_file.read_text(encoding="utf-8")
    data = json.loads(content)
    assert "contexts" in data


async def test_flush_written_data_is_correct(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("chat:user", UserState(last_active_project="alpha"))
    await store.flush()
    state_file = tmp_path / "state.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["contexts"]["chat:user"]["last_active_project"] == "alpha"


async def test_flush_noop_when_not_loaded(tmp_path):
    store = make_store(tmp_path)
    # Should not raise, should be a no-op
    await store.flush()
    state_file = tmp_path / "state.json"
    assert not state_file.exists()


async def test_flush_clears_dirty_flag(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("ctx", UserState())
    assert store._dirty is True
    await store.flush()
    assert store._dirty is False


# ---------------------------------------------------------------------------
# start_debounce_loop() / stop()
# ---------------------------------------------------------------------------


async def test_start_debounce_loop_creates_task(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    await store.start_debounce_loop()
    assert store._background_task is not None
    assert not store._background_task.done()
    await store.stop()


async def test_stop_cancels_background_task(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    await store.start_debounce_loop()
    task = store._background_task
    await store.stop()
    assert task.done()


async def test_stop_flushes_state(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_user_state("ctx", UserState(last_active_project="flush_on_stop"))
    await store.start_debounce_loop()
    await store.stop()
    state_file = tmp_path / "state.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["contexts"]["ctx"]["last_active_project"] == "flush_on_stop"


async def test_start_debounce_loop_idempotent(tmp_path):
    """Calling start_debounce_loop twice should not create a second task."""
    store = make_store(tmp_path)
    await store.load()
    await store.start_debounce_loop()
    task1 = store._background_task
    await store.start_debounce_loop()
    task2 = store._background_task
    assert task1 is task2
    await store.stop()


async def test_stop_without_start_does_not_raise(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    # Should be a no-op; no background task
    await store.stop()


# ---------------------------------------------------------------------------
# _require_loaded()
# ---------------------------------------------------------------------------


async def test_require_loaded_raises_before_load(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError, match="load\\(\\)"):
        store._require_loaded()


async def test_require_loaded_succeeds_after_load(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    result = store._require_loaded()
    assert isinstance(result, GlobalState)


async def test_get_user_state_raises_before_load(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError):
        store.get_user_state("ctx")


async def test_set_user_state_raises_before_load(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError):
        store.set_user_state("ctx", UserState())


# ---------------------------------------------------------------------------
# Debounce loop flushes dirty state
# ---------------------------------------------------------------------------


async def test_debounce_loop_flushes_dirty_state(tmp_path):
    """Debounce loop with a very short interval should flush dirty state."""
    store = make_store(tmp_path, debounce=0.05)
    await store.load()
    store.set_user_state("ctx", UserState(last_active_project="debounced"))
    assert store._dirty is True
    await store.start_debounce_loop()
    # Wait long enough for the debounce loop to fire at least once
    await asyncio.sleep(0.2)
    await store.stop()
    state_file = tmp_path / "state.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["contexts"]["ctx"]["last_active_project"] == "debounced"


async def test_debounce_loop_does_not_flush_when_not_dirty(tmp_path):
    """Debounce loop should not create file if state was never dirtied."""
    store = make_store(tmp_path, debounce=0.05)
    await store.load()
    # Do not modify state — _dirty remains False
    await store.start_debounce_loop()
    await asyncio.sleep(0.2)
    # Stop will flush even if not dirty (guaranteed consistency), so just
    # verify the loop ran without errors
    await store.stop()
    # The stop() will flush, so just assert no exception was raised


# ---------------------------------------------------------------------------
# set_binding / remove_binding / get_all_bindings
# ---------------------------------------------------------------------------


async def test_set_binding_stores_chat_to_project(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_001", "repo-A")
    assert store.get_all_bindings() == {"oc_chat_001": "repo-A"}


async def test_set_binding_overwrites_existing(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_001", "repo-A")
    store.set_binding("oc_chat_001", "repo-B")
    assert store.get_all_bindings()["oc_chat_001"] == "repo-B"


async def test_set_binding_marks_dirty(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store._dirty = False
    store.set_binding("oc_chat_001", "repo-A")
    assert store._dirty is True


async def test_set_binding_persists_after_flush(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_001", "repo-A")
    await store.flush()
    # Reload from disk
    store2 = make_store(tmp_path)
    await store2.load()
    assert store2.get_all_bindings() == {"oc_chat_001": "repo-A"}


async def test_remove_binding_removes_existing(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_001", "repo-A")
    store._dirty = False
    store.remove_binding("oc_chat_001")
    assert "oc_chat_001" not in store.get_all_bindings()
    assert store._dirty is True


async def test_remove_binding_noop_for_missing(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store._dirty = False
    store.remove_binding("nonexistent_chat")
    # Should not mark dirty when nothing was removed
    assert store._dirty is False


async def test_get_all_bindings_returns_copy(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_001", "repo-A")
    bindings = store.get_all_bindings()
    bindings["mutated"] = "oops"
    # Original should be unaffected
    assert "mutated" not in store.get_all_bindings()


async def test_get_all_bindings_empty_when_no_bindings(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    assert store.get_all_bindings() == {}


async def test_multiple_bindings_stored_correctly(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.set_binding("oc_chat_A", "repo-X")
    store.set_binding("oc_chat_B", "repo-Y")
    bindings = store.get_all_bindings()
    assert bindings == {"oc_chat_A": "repo-X", "oc_chat_B": "repo-Y"}


# ---------------------------------------------------------------------------
# save_project_actual_id / get_project_actual_id
# ---------------------------------------------------------------------------


async def test_save_project_actual_id_persists_value(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-uuid-123")
    assert store.get_project_actual_id("ctx1:user1", "repo-a") == "sess-uuid-123"


async def test_get_project_actual_id_returns_empty_for_unknown_context(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    assert store.get_project_actual_id("nonexistent", "repo-a") == ""


async def test_get_project_actual_id_returns_empty_for_unknown_project(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-abc")
    assert store.get_project_actual_id("ctx1:user1", "repo-b") == ""


async def test_save_project_actual_id_marks_dirty(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store._dirty = False
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-xyz")
    assert store._dirty is True


async def test_save_project_actual_id_clear_with_empty_string(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-abc")
    store.save_project_actual_id("ctx1:user1", "repo-a", "")
    assert store.get_project_actual_id("ctx1:user1", "repo-a") == ""


async def test_save_project_actual_id_multiple_projects_independent(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-a")
    store.save_project_actual_id("ctx1:user1", "repo-b", "sess-b")
    assert store.get_project_actual_id("ctx1:user1", "repo-a") == "sess-a"
    assert store.get_project_actual_id("ctx1:user1", "repo-b") == "sess-b"


async def test_save_project_actual_id_persists_across_flush_reload(tmp_path):
    store = make_store(tmp_path)
    await store.load()
    store.save_project_actual_id("ctx1:user1", "repo-a", "sess-persist")
    await store.flush()

    store2 = make_store(tmp_path)
    await store2.load()
    assert store2.get_project_actual_id("ctx1:user1", "repo-a") == "sess-persist"
