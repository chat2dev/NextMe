"""End-to-end persistence tests.

验证场景
---------
每个测试模拟一个「写入 → 关闭 → 重新打开 → 读取」的重启周期，
确认数据在进程边界（新实例）下能正确恢复。

覆盖组件
---------
- StateStore   — state.json（会话 ID、动态绑定、话题记录）
- MemoryManager — memory/{md5}/*.json（事实、用户上下文、个人信息）
- ContextManager — threads/{session_id}/context.*（明文 + 压缩）
- AclDb         — nextme.db（用户表、申请表）
- 跨组件         — 完整重启恢复流程
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nextme.acl.db import AclDb
from nextme.acl.schema import Role
from nextme.config.schema import Settings
from nextme.config.state_store import StateStore
from nextme.context.manager import ContextManager
from nextme.memory.manager import MemoryManager
from nextme.memory.schema import Fact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    return Settings(
        memory_debounce_seconds=1,
        context_max_bytes=100,          # 低阈值：便于触发压缩路径
        context_compression="zlib",
        memory_max_facts=50,
    )


def _make_store(tmp_path: Path, settings: Settings) -> StateStore:
    store = StateStore(settings=settings, state_path=tmp_path / "state.json")
    store._debounce_seconds = 0.05      # 加速去抖
    return store


# ===========================================================================
# StateStore — 重启持久化
# ===========================================================================


async def test_state_store_actual_id_survives_restart(tmp_path, settings):
    """save_project_actual_id → flush → 新实例 load → 仍能读回相同 actual_id。"""
    # --- 写入阶段 ---
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.save_project_actual_id("oc_chat:ou_user", "myproject", "session-uuid-abc")
    await store1.flush()

    # --- 重启：新实例从同一文件读取 ---
    store2 = _make_store(tmp_path, settings)
    await store2.load()
    actual_id = store2.get_project_actual_id("oc_chat:ou_user", "myproject")
    assert actual_id == "session-uuid-abc"


async def test_state_store_clear_actual_id_survives_restart(tmp_path, settings):
    """/new 命令清空 actual_id（空串）后重启仍为空。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.save_project_actual_id("oc_chat:ou_user", "proj", "old-id")
    store1.save_project_actual_id("oc_chat:ou_user", "proj", "")   # /new 清空
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_project_actual_id("oc_chat:ou_user", "proj") == ""


async def test_state_store_binding_survives_restart(tmp_path, settings):
    """/project bind 设置的动态绑定重启后仍然有效。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.set_binding("oc_group1", "projectA")
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_all_bindings() == {"oc_group1": "projectA"}


async def test_state_store_remove_binding_survives_restart(tmp_path, settings):
    """/project unbind 后重启绑定已消失。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.set_binding("oc_group1", "projectA")
    store1.remove_binding("oc_group1")
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_all_bindings() == {}


async def test_state_store_thread_record_survives_restart(tmp_path, settings):
    """register_thread 后 flush，重启后话题仍在活跃记录中。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.register_thread("oc_chat", "om_root_1", "proj")
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_active_thread_count("oc_chat") == 1
    threads = store2.get_threads_for_chat("oc_chat")
    assert threads[0].thread_root_id == "om_root_1"
    assert threads[0].project_name == "proj"


async def test_state_store_unregister_thread_survives_restart(tmp_path, settings):
    """unregister_thread 后重启，话题已从记录中移除。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.register_thread("oc_chat", "om_root_x", "proj")
    store1.unregister_thread("oc_chat", "om_root_x")
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_active_thread_count("oc_chat") == 0


async def test_state_store_multiple_threads_survive_restart(tmp_path, settings):
    """多个话题在同一群聊中注册，重启后全部可见。"""
    store1 = _make_store(tmp_path, settings)
    await store1.load()
    store1.register_thread("oc_chat", "om_root_1", "proj")
    store1.register_thread("oc_chat", "om_root_2", "proj")
    store1.register_thread("oc_chat2", "om_root_3", "proj")
    await store1.flush()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_active_thread_count("oc_chat") == 2
    assert store2.get_active_thread_count("oc_chat2") == 1


async def test_state_store_stop_flushes_dirty_state(tmp_path, settings):
    """stop() 在取消去抖任务前强制 flush，即使去抖未触发也能落盘。"""
    store1 = _make_store(tmp_path, settings)
    store1._debounce_seconds = 9999     # 去抖超长，防止自动触发
    await store1.load()
    store1.save_project_actual_id("ctx1", "p1", "uuid-xyz")
    # 不手动 flush，依赖 stop() 兜底
    await store1.stop()

    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_project_actual_id("ctx1", "p1") == "uuid-xyz"


async def test_state_store_corrupt_file_returns_defaults(tmp_path, settings):
    """损坏的 state.json 不崩溃，返回空 GlobalState。"""
    state_path = tmp_path / "state.json"
    state_path.write_text("{ not valid json", encoding="utf-8")

    store = _make_store(tmp_path, settings)
    state = await store.load()
    assert state.contexts == {}
    assert state.bindings == {}
    assert state.thread_records == {}


async def test_state_store_idempotent_register_thread(tmp_path, settings):
    """同一话题注册两次不会重复计数（幂等）。"""
    store = _make_store(tmp_path, settings)
    await store.load()
    store.register_thread("oc_chat", "om_root_dup", "proj")
    store.register_thread("oc_chat", "om_root_dup", "proj")
    assert store.get_active_thread_count("oc_chat") == 1


async def test_state_store_get_thread_project(tmp_path, settings):
    """get_thread_project 返回正确的项目名，不存在时返回空串。"""
    store = _make_store(tmp_path, settings)
    await store.load()
    store.register_thread("oc_chat", "om_root_p", "my_project")

    assert store.get_thread_project("oc_chat", "om_root_p") == "my_project"
    assert store.get_thread_project("oc_chat", "om_nonexistent") == ""


# ===========================================================================
# MemoryManager — 重启持久化
# ===========================================================================


def _make_memory_manager(tmp_path: Path, settings: Settings) -> MemoryManager:
    return MemoryManager(settings=settings, base_dir=tmp_path / "memory")


async def test_memory_manager_facts_survive_restart(tmp_path, settings):
    """add_fact → flush_all → 新实例 load → facts 完整读回。"""
    mgr1 = _make_memory_manager(tmp_path, settings)
    await mgr1.load("ou_user1")
    mgr1.add_fact("ou_user1", Fact(text="I use Python", source="user_command"))
    mgr1.add_fact("ou_user1", Fact(text="I prefer dark mode", source="conversation"))
    await mgr1.flush_all()

    mgr2 = _make_memory_manager(tmp_path, settings)
    await mgr2.load("ou_user1")
    facts = mgr2.get_top_facts("ou_user1", n=10)
    texts = [f.text for f in facts]
    assert "I use Python" in texts
    assert "I prefer dark mode" in texts


async def test_memory_manager_dedup_merges_similar_facts(tmp_path, settings):
    """相似度 > 0.85 的事实被合并为一条，不重复存储。"""
    mgr = _make_memory_manager(tmp_path, settings)
    await mgr.load("ou_user2")
    mgr.add_fact("ou_user2", Fact(text="I like Python programming", confidence=0.8))
    mgr.add_fact("ou_user2", Fact(text="I like Python programming!", confidence=0.9))
    await mgr.flush_all()

    mgr2 = _make_memory_manager(tmp_path, settings)
    await mgr2.load("ou_user2")
    facts = mgr2.get_top_facts("ou_user2", n=10)
    # 相似文本应合并为一条
    assert len(facts) == 1
    # 高置信度版本应胜出
    assert facts[0].confidence == 0.9


async def test_memory_manager_eviction_respects_max_facts(tmp_path, settings):
    """超出 memory_max_facts 时，低置信度事实被淘汰。"""
    low_settings = Settings(memory_max_facts=3, memory_debounce_seconds=1)
    mgr = _make_memory_manager(tmp_path, low_settings)
    await mgr.load("ou_user3")

    for i in range(5):
        mgr.add_fact("ou_user3", Fact(text=f"fact_{i}", confidence=float(i) / 10))

    facts = mgr.get_top_facts("ou_user3", n=10)
    assert len(facts) <= 3
    # 高置信度事实保留
    kept_texts = [f.text for f in facts]
    assert "fact_4" in kept_texts   # confidence=0.4（最高）
    assert "fact_3" in kept_texts   # confidence=0.3


async def test_memory_manager_user_context_survives_restart(tmp_path, settings):
    """update_user_context → flush_all → 新实例能读回更新后的偏好。"""
    from nextme.memory.schema import UserContextMemory

    mgr1 = _make_memory_manager(tmp_path, settings)
    await mgr1.load("ou_userX")
    ucm = UserContextMemory(preferred_language="en", communication_style="concise")
    mgr1.update_user_context("ou_userX", ucm)
    await mgr1.flush_all()

    mgr2 = _make_memory_manager(tmp_path, settings)
    uc2, _, _ = await mgr2.load("ou_userX")
    assert uc2.preferred_language == "en"
    assert uc2.communication_style == "concise"


async def test_memory_manager_personal_info_survives_restart(tmp_path, settings):
    """update_personal_info → flush_all → 新实例能读回个人信息。"""
    from nextme.memory.schema import PersonalInfo

    mgr1 = _make_memory_manager(tmp_path, settings)
    await mgr1.load("ou_userY")
    pi = PersonalInfo(name="Alice", timezone="Asia/Shanghai", role="engineer")
    mgr1.update_personal_info("ou_userY", pi)
    await mgr1.flush_all()

    mgr2 = _make_memory_manager(tmp_path, settings)
    _, p2, _ = await mgr2.load("ou_userY")
    assert p2.name == "Alice"
    assert p2.timezone == "Asia/Shanghai"


async def test_memory_manager_stop_flushes_dirty(tmp_path, settings):
    """stop() 强制 flush，不依赖去抖定时器。"""
    mgr = _make_memory_manager(tmp_path, settings)
    mgr._debounce_seconds = 9999
    await mgr.load("ou_stop_test")
    mgr.add_fact("ou_stop_test", Fact(text="persist via stop", source="test"))
    await mgr.stop()

    mgr2 = _make_memory_manager(tmp_path, settings)
    await mgr2.load("ou_stop_test")
    texts = [f.text for f in mgr2.get_top_facts("ou_stop_test")]
    assert "persist via stop" in texts


async def test_memory_manager_multiple_users_isolated(tmp_path, settings):
    """不同 context_id 的记忆相互隔离，互不干扰。"""
    mgr = _make_memory_manager(tmp_path, settings)
    await mgr.load("ou_alice")
    await mgr.load("ou_bob")
    mgr.add_fact("ou_alice", Fact(text="Alice fact", source="test"))
    mgr.add_fact("ou_bob", Fact(text="Bob fact", source="test"))
    await mgr.flush_all()

    mgr2 = _make_memory_manager(tmp_path, settings)
    await mgr2.load("ou_alice")
    await mgr2.load("ou_bob")
    alice_facts = [f.text for f in mgr2.get_top_facts("ou_alice")]
    bob_facts = [f.text for f in mgr2.get_top_facts("ou_bob")]
    assert "Alice fact" in alice_facts
    assert "Bob fact" not in alice_facts
    assert "Bob fact" in bob_facts
    assert "Alice fact" not in bob_facts


# ===========================================================================
# ContextManager — 重启持久化
# ===========================================================================


def _make_ctx_manager(tmp_path: Path, settings: Settings) -> ContextManager:
    return ContextManager(settings=settings, base_dir=tmp_path / "threads")


async def test_context_manager_plain_text_survives_restart(tmp_path, settings):
    """短文本（< context_max_bytes）以明文保存，新实例可读回。"""
    # settings.context_max_bytes=100，短文本不触发压缩
    ctx1 = _make_ctx_manager(tmp_path, settings)
    await ctx1.save("session_1", "Hello, world!")

    ctx2 = _make_ctx_manager(tmp_path, settings)
    content = await ctx2.load("session_1")
    assert content == "Hello, world!"


async def test_context_manager_compressed_text_survives_restart(tmp_path, settings):
    """长文本（>= context_max_bytes）压缩后保存，新实例可透明解压读回。"""
    long_text = "A" * 200      # 200 bytes > context_max_bytes=100
    ctx1 = _make_ctx_manager(tmp_path, settings)
    await ctx1.save("session_long", long_text)

    # 验证确实写的是压缩文件
    session_dir = tmp_path / "threads" / "session_long"
    assert not (session_dir / "context.txt").exists(), "不应有明文文件"
    assert (session_dir / "context.meta.json").exists(), "应有 meta 文件"

    # 新实例透明读回
    ctx2 = _make_ctx_manager(tmp_path, settings)
    content = await ctx2.load("session_long")
    assert content == long_text


async def test_context_manager_meta_json_correct(tmp_path, settings):
    """压缩时 meta.json 记录了正确的算法名和大小信息。"""
    long_text = "B" * 200
    ctx = _make_ctx_manager(tmp_path, settings)
    await ctx.save("session_meta", long_text)

    meta_path = tmp_path / "threads" / "session_meta" / "context.meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["algorithm"] == "zlib"
    assert meta["original_size"] == len(long_text.encode())
    assert meta["compressed_size"] > 0
    assert meta["compressed_size"] < meta["original_size"]


async def test_context_manager_append_accumulates(tmp_path, settings):
    """append 在已有内容末尾追加，重启后累积内容完整。"""
    ctx1 = _make_ctx_manager(tmp_path, settings)
    await ctx1.save("session_app", "line1")
    await ctx1.append("session_app", "line2")

    ctx2 = _make_ctx_manager(tmp_path, settings)
    content = await ctx2.load("session_app")
    assert "line1" in content
    assert "line2" in content


async def test_context_manager_overwrite_replaces_previous(tmp_path, settings):
    """save 覆盖旧内容，不会保留旧压缩文件。"""
    long_text = "C" * 200
    ctx1 = _make_ctx_manager(tmp_path, settings)
    await ctx1.save("session_over", long_text)    # 写压缩文件

    # 覆盖为短文本
    await ctx1.save("session_over", "short")

    session_dir = tmp_path / "threads" / "session_over"
    # 旧压缩文件已被删除
    assert not (session_dir / "context.zlib").exists()
    # 新的明文文件存在
    assert (session_dir / "context.txt").exists()

    ctx2 = _make_ctx_manager(tmp_path, settings)
    assert await ctx2.load("session_over") == "short"


async def test_context_manager_missing_session_returns_empty(tmp_path, settings):
    """不存在的 session_id 返回空串，不抛出异常。"""
    ctx = _make_ctx_manager(tmp_path, settings)
    content = await ctx.load("nonexistent_session")
    assert content == ""


async def test_context_manager_get_size(tmp_path, settings):
    """get_size 返回磁盘上实际文件字节数（压缩后大小）。"""
    ctx = _make_ctx_manager(tmp_path, settings)
    assert ctx.get_size("no_session") == 0

    await ctx.save("sized_session", "Hello")
    size = ctx.get_size("sized_session")
    assert size > 0


async def test_context_manager_unicode_content_round_trip(tmp_path, settings):
    """中文等多字节字符在压缩/解压后保持完整。"""
    text = "你好，世界！" * 20   # 重复至超过 100 字节压缩阈值
    ctx1 = _make_ctx_manager(tmp_path, settings)
    await ctx1.save("session_unicode", text)

    ctx2 = _make_ctx_manager(tmp_path, settings)
    assert await ctx2.load("session_unicode") == text


# ===========================================================================
# AclDb — 重启持久化
# ===========================================================================


@pytest.fixture
async def acl_db(tmp_path):
    db = AclDb(db_path=tmp_path / "nextme.db")
    await db.open()
    yield db
    await db.close()


async def test_acl_db_user_survives_reconnect(tmp_path):
    """add_user 后关闭再重新打开，用户仍然存在。"""
    db1 = AclDb(db_path=tmp_path / "nextme.db")
    await db1.open()
    await db1.add_user("ou_alice", Role.OWNER, "Alice", added_by="ou_admin")
    await db1.close()

    db2 = AclDb(db_path=tmp_path / "nextme.db")
    await db2.open()
    user = await db2.get_user("ou_alice")
    await db2.close()

    assert user is not None
    assert user.open_id == "ou_alice"
    assert user.role == Role.OWNER
    assert user.display_name == "Alice"


async def test_acl_db_remove_user_survives_reconnect(tmp_path):
    """remove_user 后关闭再重新打开，用户已消失。"""
    db1 = AclDb(db_path=tmp_path / "nextme.db")
    await db1.open()
    await db1.add_user("ou_bob", Role.COLLABORATOR, "Bob", added_by="ou_admin")
    await db1.remove_user("ou_bob")
    await db1.close()

    db2 = AclDb(db_path=tmp_path / "nextme.db")
    await db2.open()
    user = await db2.get_user("ou_bob")
    await db2.close()
    assert user is None


async def test_acl_db_list_users_by_role(acl_db):
    """list_users(role) 只返回指定角色的用户。"""
    await acl_db.add_user("ou_o1", Role.OWNER, "Owner1", added_by="ou_admin")
    await acl_db.add_user("ou_c1", Role.COLLABORATOR, "Collab1", added_by="ou_admin")
    await acl_db.add_user("ou_c2", Role.COLLABORATOR, "Collab2", added_by="ou_admin")

    owners = await acl_db.list_users(Role.OWNER)
    collabs = await acl_db.list_users(Role.COLLABORATOR)
    all_users = await acl_db.list_users()

    assert len(owners) == 1
    assert owners[0].open_id == "ou_o1"
    assert len(collabs) == 2
    assert len(all_users) == 3


async def test_acl_db_application_survives_reconnect(tmp_path):
    """create_application 后关闭再重开，申请仍为 pending。"""
    db1 = AclDb(db_path=tmp_path / "nextme.db")
    await db1.open()
    app_id = await db1.create_application("ou_newuser", "NewUser", Role.COLLABORATOR)
    await db1.close()

    db2 = AclDb(db_path=tmp_path / "nextme.db")
    await db2.open()
    app = await db2.get_application(app_id)
    await db2.close()

    assert app is not None
    assert app.applicant_id == "ou_newuser"
    assert app.status == "pending"
    assert app.requested_role == Role.COLLABORATOR


async def test_acl_db_approve_survives_reconnect(tmp_path):
    """approve 申请后关闭再重开，状态仍为 approved。"""
    db1 = AclDb(db_path=tmp_path / "nextme.db")
    await db1.open()
    app_id = await db1.create_application("ou_applicant", "Applicant", Role.OWNER)
    await db1.update_application_status(app_id, "approved", "ou_admin")
    await db1.close()

    db2 = AclDb(db_path=tmp_path / "nextme.db")
    await db2.open()
    app = await db2.get_application(app_id)
    pending = await db2.list_pending_applications()
    await db2.close()

    assert app.status == "approved"
    assert app.processed_by == "ou_admin"
    assert len(pending) == 0


async def test_acl_db_duplicate_pending_rejected(acl_db):
    """同一 applicant 不能有两条 pending 申请（UNIQUE 约束）。"""
    id1 = await acl_db.create_application("ou_dup", "Dup", Role.COLLABORATOR)
    id2 = await acl_db.create_application("ou_dup", "Dup", Role.COLLABORATOR)
    assert id1 is not None
    assert id2 is None   # 已有 pending，返回 None


async def test_acl_db_insert_or_replace_updates_role(acl_db):
    """INSERT OR REPLACE 可以更新用户角色。"""
    await acl_db.add_user("ou_update", Role.COLLABORATOR, "U", added_by="admin")
    await acl_db.add_user("ou_update", Role.OWNER, "U-promoted", added_by="admin")

    user = await acl_db.get_user("ou_update")
    assert user.role == Role.OWNER
    assert user.display_name == "U-promoted"


# ===========================================================================
# 跨组件集成 — 完整重启恢复流程
# ===========================================================================


async def test_full_restart_recovery(tmp_path, settings):
    """模拟完整的写入 → stop() → 新实例 → 读取，验证三个组件的数据都恢复正确。"""
    # ---- Phase 1: 写入阶段（模拟进程运行中） ----
    store = _make_store(tmp_path, settings)
    store._debounce_seconds = 9999
    await store.load()

    # StateStore: 绑定 + 会话 ID + 话题
    store.set_binding("oc_group_X", "project_alpha")
    store.save_project_actual_id("oc_group_X:ou_dev", "project_alpha", "uuid-restart-test")
    store.register_thread("oc_group_X", "om_thread_001", "project_alpha")

    # MemoryManager: 两条事实
    mem = _make_memory_manager(tmp_path, settings)
    await mem.load("ou_dev")
    mem.add_fact("ou_dev", Fact(text="prefers vim", source="conversation", confidence=0.9))
    mem.add_fact("ou_dev", Fact(text="timezone UTC+8", source="user_command", confidence=0.95))

    # ContextManager: 超阈值内容 → 压缩存储
    ctx = _make_ctx_manager(tmp_path, settings)
    long_ctx = "conversation turn " * 10   # > 100 bytes
    await ctx.save("oc_group_X:ou_dev", long_ctx)

    # 模拟 stop(): 强制 flush
    await store.stop()
    await mem.stop()

    # ---- Phase 2: 重启后读取（新实例） ----
    store2 = _make_store(tmp_path, settings)
    await store2.load()

    # 验证 StateStore
    assert store2.get_all_bindings() == {"oc_group_X": "project_alpha"}
    assert store2.get_project_actual_id("oc_group_X:ou_dev", "project_alpha") == "uuid-restart-test"
    assert store2.get_active_thread_count("oc_group_X") == 1
    threads = store2.get_threads_for_chat("oc_group_X")
    assert threads[0].thread_root_id == "om_thread_001"

    # 验证 MemoryManager
    mem2 = _make_memory_manager(tmp_path, settings)
    await mem2.load("ou_dev")
    facts = mem2.get_top_facts("ou_dev", n=10)
    fact_texts = [f.text for f in facts]
    assert "prefers vim" in fact_texts
    assert "timezone UTC+8" in fact_texts

    # 验证 ContextManager
    ctx2 = _make_ctx_manager(tmp_path, settings)
    restored = await ctx2.load("oc_group_X:ou_dev")
    assert restored == long_ctx


async def test_state_store_and_acl_db_independent(tmp_path, settings):
    """StateStore（JSON）与 AclDb（SQLite）可以同时写入，互不干扰。"""
    store = _make_store(tmp_path, settings)
    await store.load()
    store.set_binding("oc_chat_y", "proj_y")

    db = AclDb(db_path=tmp_path / "nextme.db")
    await db.open()
    await db.add_user("ou_coexist", Role.OWNER, "CoUser", added_by="ou_admin")

    await store.flush()
    await db.close()

    # 重新读取
    store2 = _make_store(tmp_path, settings)
    await store2.load()
    assert store2.get_all_bindings()["oc_chat_y"] == "proj_y"

    db2 = AclDb(db_path=tmp_path / "nextme.db")
    await db2.open()
    user = await db2.get_user("ou_coexist")
    await db2.close()
    assert user is not None
    assert user.role == Role.OWNER
