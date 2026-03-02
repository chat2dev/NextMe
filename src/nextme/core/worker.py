"""SessionWorker — serial task consumer for a single Session.

Each :class:`Session` has at most one :class:`SessionWorker` running as an
asyncio Task.  The worker:

1. Pulls :class:`~nextme.protocol.types.Task` objects from the session queue.
2. Acquires the :class:`~nextme.core.path_lock.PathLockRegistry` lock for the
   project path so no two sessions write to the same directory concurrently.
3. Drives :class:`~nextme.acp.runtime.ACPRuntime` (``ensure_ready`` +
   ``execute``).
4. Streams progress updates (debounced) and final results back via the task's
   ``reply_fn``.
5. Handles task cancellation, errors, and permission requests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
from typing import Optional

from ..acp.janitor import ACPRuntimeRegistry
from ..config.schema import Settings
from ..config.state_store import StateStore
from ..memory.manager import MemoryManager
from ..memory.schema import Fact
from ..protocol.types import (
    PermissionChoice,
    PermissionRequest,
    Reply,
    ReplyType,
    Task,
    TaskStatus,
    TaskTimeoutError,
)
from ..feishu.progress_card import RunProgressCard
from .interfaces import Replier
from .path_lock import PathLockRegistry
from .prompt_loader import load_memory_template
from .session import Session

logger = logging.getLogger(__name__)

# Minimum interval between full-card updates via PUT /cards/:card_id.
# Feishu CardKit allows ~5 QPS per card for full updates; 200 ms leaves
# comfortable headroom while still feeling responsive.
_STREAMING_DEBOUNCE_SECONDS: float = 0.2


def _format_elapsed(seconds: int) -> str:
    """Return a compact human-readable elapsed time string (e.g. '5s', '1m 30s')."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


_MEMORY_TAG_RE = re.compile(r"<memory([^>]*)>(.*?)</memory>", re.DOTALL)

# Memory facts longer than this are almost certainly misused plan/content blocks.
# We still record them but keep the content visible in the card output.
_MAX_MEMORY_FACT_CHARS = 500


@dataclasses.dataclass
class _MemoryOp:
    """A parsed memory operation from an agent <memory> tag."""

    op: str        # "add" | "replace" | "forget"
    text: str      # new text (add / replace); empty for forget
    idx: int = -1  # target index in confidence-sorted facts (replace / forget)


class SessionWorker:
    """Serially consume tasks from a :class:`~nextme.core.session.Session`'s queue.

    Args:
        session: The session this worker is bound to.
        acp_registry: Global registry of :class:`~nextme.acp.runtime.ACPRuntime` instances.
        replier: Helper for sending Feishu messages / cards.
        settings: Application settings.
        path_lock_registry: Global path-based lock registry.
    """

    def __init__(
        self,
        session: Session,
        acp_registry: ACPRuntimeRegistry,
        replier: Replier,
        settings: Settings,
        path_lock_registry: PathLockRegistry,
        state_store: Optional[StateStore] = None,
        memory_manager: Optional[MemoryManager] = None,
    ) -> None:
        self._session = session
        self._acp_registry = acp_registry
        self._replier = replier
        self._settings = settings
        self._path_lock_registry = path_lock_registry
        self._state_store = state_store
        self._memory_manager = memory_manager
        self._memory_template = load_memory_template()

        # State maintained across _on_progress calls for a single task.
        self._progress_message_id: Optional[str] = None
        self._progress_buffer: list[str] = []
        self._last_progress_update: float = 0.0
        self._task_start: float = 0.0
        self._active_message_id: str = ""   # original Feishu message_id
        self._active_in_thread: bool = False  # thread vs quote-reply mode

        # Streaming card state (reset per-task).
        self._card_id: Optional[str] = None          # cardkit card_id (None = use fallback)
        self._sequence: int = 0                      # strictly-increasing sequence counter
        self._run_card: Optional[RunProgressCard] = None  # event tracker for streaming
        self._last_streaming_update: float = 0.0     # debounce timestamp

    @property
    def _proj(self) -> str:
        """Return a bracketed project name tag for card titles, e.g. '【myproject】'."""
        return f"【{self._session.project_name}】"

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_and_strip_memory(content: str) -> tuple[list[_MemoryOp], str]:
        """Parse ``<memory ...>...</memory>`` tags from agent output.

        Returns ``(ops, stripped_content)`` where *ops* is a list of
        :class:`_MemoryOp` objects and *stripped_content* has all memory
        tags removed (oversized ADD blocks are kept visible).

        Supported ops:
        - ``<memory>text</memory>``                       → ADD
        - ``<memory op="replace" idx="N">text</memory>``  → REPLACE fact N
        - ``<memory op="forget" idx="N"></memory>``        → FORGET fact N
        """
        ops: list[_MemoryOp] = []

        def _collect(m: re.Match) -> str:
            attr_str: str = m.group(1)
            text: str = m.group(2).strip()
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', attr_str))
            op = attrs.get("op", "add")
            raw_idx = attrs.get("idx", "")
            idx = int(raw_idx) if raw_idx.lstrip("-").isdigit() else -1

            if op == "add":
                if len(text) > _MAX_MEMORY_FACT_CHARS:
                    logger.warning(
                        "worker: oversized <memory> block (%d chars) kept in display; "
                        "use <memory> only for short, discrete facts",
                        len(text),
                    )
                    ops.append(_MemoryOp(op="add", text=text))
                    return text
                ops.append(_MemoryOp(op="add", text=text))
                return ""
            elif op == "replace" and idx >= 0 and text:
                ops.append(_MemoryOp(op="replace", text=text, idx=idx))
                return ""
            elif op == "forget" and idx >= 0:
                ops.append(_MemoryOp(op="forget", text="", idx=idx))
                return ""
            # Malformed tag — strip it silently.
            return ""

        stripped = _MEMORY_TAG_RE.sub(_collect, content)
        # Collapse 3+ consecutive newlines down to 2.
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
        return ops, stripped

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Consume tasks from the session queue until cancelled.

        This coroutine is designed to run as an ``asyncio.Task``.
        Cancellation is handled gracefully: the active task (if any) is
        cancelled before the loop exits.
        """
        logger.info(
            "SessionWorker[%s]: started for project %r",
            self._session.context_id,
            self._session.project_name,
        )
        try:
            while True:
                task = await self._session.task_queue.get()

                # Remove from pending list now that it has been dequeued.
                try:
                    self._session.pending_tasks.remove(task)
                except ValueError:
                    pass

                if task.canceled:
                    logger.debug(
                        "SessionWorker[%s]: skipping already-cancelled task %s",
                        self._session.context_id,
                        task.id,
                    )
                    self._session.task_queue.task_done()
                    continue

                self._session.active_task = task
                try:
                    await self._execute_task(task)
                except asyncio.CancelledError:
                    logger.info(
                        "SessionWorker[%s]: worker cancelled during task %s",
                        self._session.context_id,
                        task.id,
                    )
                    # Mark task as cancelled and send feedback before re-raising.
                    task.canceled = True
                    self._session.status = TaskStatus.CANCELED
                    await self._send_cancelled(task)
                    raise
                finally:
                    self._session.active_task = None
                    self._session.status = TaskStatus.IDLE
                    self._session.task_queue.task_done()

        except asyncio.CancelledError:
            logger.info(
                "SessionWorker[%s]: shutting down", self._session.context_id
            )
            raise
        except Exception:
            logger.exception(
                "SessionWorker[%s]: unhandled error in run loop",
                self._session.context_id,
            )

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    async def _execute_task(self, task: Task) -> None:
        """Execute *task* end-to-end with progress and permission handling.

        Steps:
        1. Check path lock contention; send appropriate initial card.
        2. Acquire path lock for the project directory.
        3. Ensure the ACP runtime is ready.
        4. Execute via ACP, streaming progress and handling permissions.
        5. Send the final result card.
        """
        self._session.status = TaskStatus.EXECUTING

        # Reset per-task progress state.
        self._progress_message_id = None
        self._progress_buffer = []
        self._last_progress_update = 0.0
        self._task_start = time.monotonic()
        self._active_message_id = task.message_id
        self._active_in_thread = task.chat_type == "group"
        self._card_id = None
        self._sequence = 0
        self._run_card = None
        self._last_streaming_update = 0.0

        # ── DEBUG ① 用户原始输入 ──────────────────────────────────────────
        logger.debug(
            "SessionWorker[%s]: ━━━ [USER-INPUT] ━━━\n%s",
            self._session.context_id,
            task.content,
        )

        # group → thread reply; p2p → quote reply; no message_id → top-level.
        in_thread = self._active_in_thread
        chat_id = self._session.context_id.split(":")[0]

        # Step 1 — Check path lock contention BEFORE sending any progress card.
        #
        # When two users share the same project path, the second user's worker
        # reaches this point while the first still holds the lock.  Without this
        # check the second user would receive an initial "思考中..." card that
        # never updates — because the worker immediately blocks on the lock and
        # cannot send further progress.  Instead we detect contention eagerly and
        # show a "候补中" (queued) card so the user knows their task is waiting.
        path_lock = self._path_lock_registry.get(self._session.project_path)
        lock_contended = path_lock.locked()

        if lock_contended:
            # Another session holds the path lock.  Show a waiting card and set
            # the correct status before blocking on the lock.
            self._session.status = TaskStatus.WAITING_LOCK
            waiting_card = self._replier.build_progress_card(
                status="",
                content="候补中，等待当前任务完成后自动执行...",
                title=f"⏳ 候补中... {self._proj}",
            )
            try:
                if task.message_id:
                    self._progress_message_id = await self._replier.reply_card(
                        task.message_id, waiting_card, in_thread=in_thread
                    )
                else:
                    self._progress_message_id = await self._replier.send_card(
                        chat_id, waiting_card
                    )
                logger.debug(
                    "SessionWorker[%s]: sent waiting card (lock contended) message_id=%s",
                    self._session.context_id,
                    self._progress_message_id,
                )
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to send waiting card",
                    self._session.context_id,
                )
        else:
            # No contention: send the initial "思考中..." card now so the user
            # gets immediate feedback.  Try cardkit streaming first, fall back to
            # a regular im/v1 card.
            card_id = ""
            if self._settings.streaming_enabled:
                try:
                    self._run_card = RunProgressCard()
                    initial_card_json = json.dumps(
                        self._run_card.build_card("running", self._proj), ensure_ascii=False
                    )
                    card_id = await self._replier.create_card(initial_card_json)
                except Exception:
                    card_id = ""

            streaming_ok = False
            if card_id:
                # Cardkit-first: reference the cardkit card_id from im/v1.
                self._card_id = card_id
                try:
                    if task.message_id:
                        sent_id = await self._replier.reply_card_by_id(
                            task.message_id, card_id, in_thread=in_thread
                        )
                    else:
                        sent_id = await self._replier.send_card_by_id(chat_id, card_id)
                except Exception:
                    logger.exception(
                        "SessionWorker[%s]: failed to send streaming progress card",
                        self._session.context_id,
                    )
                    sent_id = ""

                if sent_id:
                    self._progress_message_id = sent_id
                    streaming_ok = True
                    logger.debug(
                        "SessionWorker[%s]: streaming card card_id=%s message_id=%s",
                        self._session.context_id,
                        card_id,
                        sent_id,
                    )
                else:
                    # Feishu rejected the card_id reference (e.g. 230099) —
                    # clear streaming mode and fall through to the regular card path.
                    logger.warning(
                        "SessionWorker[%s]: streaming card send returned empty "
                        "(card_id=%s), falling back to regular card",
                        self._session.context_id,
                        card_id,
                    )
                    self._card_id = None

            if not streaming_ok:
                # Fallback: regular im/v1 card with debounced full-card PATCH.
                self._card_id = None
                initial_card = self._replier.build_progress_card(
                    status="",
                    content="思考中...",
                    title=f"思考中... {self._proj}",
                )
                try:
                    if task.message_id:
                        self._progress_message_id = await self._replier.reply_card(
                            task.message_id, initial_card, in_thread=in_thread
                        )
                    else:
                        self._progress_message_id = await self._replier.send_card(
                            chat_id, initial_card
                        )
                    logger.debug(
                        "SessionWorker[%s]: sent initial progress card message_id=%s",
                        self._session.context_id,
                        self._progress_message_id,
                    )
                except Exception:
                    logger.exception(
                        "SessionWorker[%s]: failed to send initial progress card",
                        self._session.context_id,
                    )

        # Step 2 — Acquire path lock (blocks if another session is executing).
        logger.debug(
            "SessionWorker[%s]: acquiring path lock for %s",
            self._session.context_id,
            self._session.project_path,
        )
        async with path_lock:
            self._session.status = TaskStatus.EXECUTING

            # If we showed a "候补中" card while waiting, update it to "思考中..."
            # now that we hold the lock and are about to start real work.
            if lock_contended and self._progress_message_id:
                thinking_card = self._replier.build_progress_card(
                    status="",
                    content="思考中...",
                    title=f"思考中... {self._proj}",
                )
                try:
                    await self._replier.update_card(self._progress_message_id, thinking_card)
                    logger.debug(
                        "SessionWorker[%s]: updated waiting card to thinking message_id=%s",
                        self._session.context_id,
                        self._progress_message_id,
                    )
                except Exception:
                    logger.exception(
                        "SessionWorker[%s]: failed to update waiting card to thinking",
                        self._session.context_id,
                    )

            # Step 3 — Obtain and ready the ACP runtime.
            runtime = self._acp_registry.get_or_create(
                session_id=f"{self._session.context_id}:{self._session.project_name}",
                cwd=str(self._session.project_path),
                settings=self._settings,
                executor=self._session.executor,
                executor_args=self._session.executor_args,
            )

            try:
                await runtime.ensure_ready()
            except Exception as exc:
                logger.error(
                    "SessionWorker[%s]: ACPRuntime.ensure_ready failed: %s",
                    self._session.context_id,
                    exc,
                )
                await self._send_error(task, str(exc))
                self._session.status = TaskStatus.DONE
                return

            # Sync actual_id from runtime to session (may be set from prior run).
            if runtime.actual_id and not self._session.actual_id:
                self._session.actual_id = runtime.actual_id

            # Restore persisted session id to runtime (enables --resume on restart).
            if not runtime.actual_id and self._state_store is not None:
                persisted_id = self._state_store.get_project_actual_id(
                    self._session.context_id, self._session.project_name
                )
                if persisted_id:
                    await runtime.restore_session(persisted_id)
                    self._session.actual_id = persisted_id
                    logger.info(
                        "SessionWorker[%s]: restored session id %r for project %r",
                        self._session.context_id,
                        persisted_id,
                        self._session.project_name,
                    )

            # Inject user memory facts into the task prompt for new sessions only.
            # Facts are keyed by user_id (global across all chats) not context_id.
            if not runtime.actual_id and self._memory_manager is not None:
                user_id = self._session.context_id.rsplit(":", 1)[-1]
                try:
                    await self._memory_manager.load(user_id)
                except Exception:
                    logger.exception(
                        "SessionWorker[%s]: failed to load memory", self._session.context_id
                    )
                facts = self._memory_manager.get_top_facts(user_id, n=10)
                if facts:
                    rendered = self._memory_template.render(
                        count=len(facts),
                        facts=facts,
                    )
                    # ── DEBUG ④ 注入的长期 memory ──────────────────────────
                    logger.debug(
                        "SessionWorker[%s]: ━━━ [MEMORY-INJECT] %d facts ━━━\n%s",
                        self._session.context_id,
                        len(facts),
                        rendered,
                    )
                    task = dataclasses.replace(
                        task,
                        content=f"{rendered}\n\n[用户消息]\n{task.content}",
                    )

            # Step 4 — Execute.
            # ── DEBUG ② 发给 Executor(Agent) 的原始输入 ─────────────────
            logger.debug(
                "SessionWorker[%s]: ━━━ [AGENT-INPUT] ━━━\n%s",
                self._session.context_id,
                task.content,
            )
            try:
                final_content = await runtime.execute(
                    task=task,
                    on_progress=self._on_progress,
                    on_permission=self._on_permission,
                )
            except asyncio.CancelledError:
                self._session.status = TaskStatus.CANCELED
                await self._send_cancelled(task)
                raise
            except TaskTimeoutError:
                logger.warning(
                    "SessionWorker[%s]: task %s timed out after %.0fs",
                    self._session.context_id,
                    task.id,
                    time.monotonic() - self._task_start,
                )
                self._session.status = TaskStatus.DONE
                await self._send_timeout(task)
                return
            except Exception as exc:
                logger.error(
                    "SessionWorker[%s]: ACPRuntime.execute failed: %s",
                    self._session.context_id,
                    exc,
                )
                self._session.status = TaskStatus.DONE
                await self._send_error(task, str(exc))
                return

            # ── DEBUG ③ Executor(Agent) 返回的原始消息 ──────────────────
            logger.debug(
                "SessionWorker[%s]: ━━━ [AGENT-OUTPUT] ━━━\n%s",
                self._session.context_id,
                final_content,
            )

            # Sync actual_id back to session after execute.
            if runtime.actual_id:
                self._session.actual_id = runtime.actual_id

            # Persist session id for restart resumption.
            if self._state_store is not None and runtime.actual_id:
                self._state_store.save_project_actual_id(
                    self._session.context_id,
                    self._session.project_name,
                    runtime.actual_id,
                )

        # Extract <memory> facts written by the agent and strip them from the
        # displayed output.  Writeback happens regardless of session age so
        # that the agent can update memory at any point in a conversation.
        user_id = self._session.context_id.rsplit(":", 1)[-1]
        memory_ops, final_content = self._extract_and_strip_memory(final_content)
        # ── DEBUG ⑤ Agent 返回的 memory 操作 ────────────────────────────
        if memory_ops:
            logger.debug(
                "SessionWorker[%s]: ━━━ [MEMORY-OPS] %d op(s) ━━━\n%s",
                self._session.context_id,
                len(memory_ops),
                "\n".join(
                    f"  [{i}] op={op.op} idx={op.idx} text={op.text!r}"
                    for i, op in enumerate(memory_ops)
                ),
            )
        if memory_ops and self._memory_manager is not None:
            for op in memory_ops:
                if op.op == "add":
                    self._memory_manager.add_fact(
                        user_id, Fact(text=op.text, source="agent_output")
                    )
                elif op.op == "replace":
                    if not self._memory_manager.replace_fact(user_id, op.idx, op.text):
                        logger.warning(
                            "SessionWorker[%s]: replace_fact idx=%d out of range",
                            self._session.context_id,
                            op.idx,
                        )
                elif op.op == "forget":
                    if not self._memory_manager.forget_fact(user_id, op.idx):
                        logger.warning(
                            "SessionWorker[%s]: forget_fact idx=%d out of range",
                            self._session.context_id,
                            op.idx,
                        )
            logger.debug(
                "SessionWorker[%s]: processed %d memory ops from agent output",
                self._session.context_id,
                len(memory_ops),
            )

        # Step 5 — Send final result card.
        self._session.status = TaskStatus.DONE

        if task.canceled:
            await self._send_cancelled(task)
            return

        await self._send_result(task, final_content)

    # ------------------------------------------------------------------
    # Progress callback
    # ------------------------------------------------------------------

    async def _on_progress(self, delta: str, tool_name: str) -> None:
        """Receive a progress delta from ACPRuntime and forward to the card.

        Two modes:
        - **Streaming** (``_card_id`` set): directly patches card elements via
          the cardkit API — no debounce needed, no full card re-render.
        - **Fallback** (``_card_id`` is None): accumulates text and rebuilds
          the full card on a debounce timer (``progress_debounce_seconds``).

        Args:
            delta: Text delta emitted by the ACP subprocess.
            tool_name: Name (and optional args) of the tool being invoked.
        """
        if delta:
            self._progress_buffer.append(delta)

        # ------------------------------------------------------------------
        # Streaming path — PUT /content with full accumulated text.
        #
        # The Feishu cardkit typewriter API (PUT /elements/:id/content) expects
        # the ever-growing FULL text on every call, not just a delta.  Feishu
        # animates the difference as a typewriter effect.
        #
        # Debounce: batch multiple LLM token chunks into one API call so we
        # send ≤ 1/DEBOUNCE_S updates per second instead of one per token.
        # Tool-use events are always flushed immediately.
        # ------------------------------------------------------------------
        if self._card_id:
            if self._run_card is None:
                self._run_card = RunProgressCard()

            elapsed_s = int(time.monotonic() - self._task_start)
            if delta:
                self._run_card.add_text_chunk(delta)
            if tool_name:
                self._run_card.add_tool(tool_name, elapsed_s)

            now = time.monotonic()
            if (
                not tool_name
                and now - self._last_streaming_update < _STREAMING_DEBOUNCE_SECONDS
            ):
                # Not enough time and no tool event — skip this update.
                return

            if delta or tool_name:
                self._sequence += 1
                self._last_streaming_update = now
                try:
                    card_json = json.dumps(
                        self._run_card.build_card("running", self._proj),
                        ensure_ascii=False,
                    )
                    await self._replier.update_card_entity(
                        self._card_id, card_json, self._sequence
                    )
                except Exception as exc:
                    logger.debug(
                        "SessionWorker[%s]: update_card_entity failed (seq=%d): %s",
                        self._session.context_id,
                        self._sequence,
                        exc,
                    )
            return

        # ------------------------------------------------------------------
        # Fallback path — debounced full-card PATCH.
        # When streaming is disabled, skip all intermediate updates so the
        # user sees only the initial "思考中..." card and the final result.
        # ------------------------------------------------------------------
        if not self._settings.streaming_enabled:
            return

        now = time.monotonic()
        elapsed = now - self._last_progress_update

        if elapsed < self._settings.progress_debounce_seconds and not tool_name:
            # Not enough time has passed and no tool-use event; skip update.
            return

        accumulated = "".join(self._progress_buffer)

        if not accumulated and not tool_name:
            return

        elapsed_s = int(now - self._task_start)
        elapsed_str = _format_elapsed(elapsed_s)
        if tool_name:
            status_text = f"工具调用: {tool_name} · {elapsed_str}"
        else:
            status_text = elapsed_str

        self._last_progress_update = now

        card = self._replier.build_progress_card(
            status=status_text,
            content=accumulated or "思考中...",
            title=f"思考中... {self._proj}",
        )

        if self._progress_message_id:
            try:
                await self._replier.update_card(self._progress_message_id, card)
            except Exception as exc:
                # Progress updates are best-effort; log briefly and continue.
                logger.warning(
                    "SessionWorker[%s]: failed to update progress card: %s",
                    self._session.context_id,
                    exc,
                )
        else:
            # Fallback: initial card send failed; send a new card keeping the
            # same reply mode (thread / quote / top-level) as the original task.
            chat_id = self._session.context_id.split(":")[0]
            try:
                if self._active_message_id:
                    self._progress_message_id = await self._replier.reply_card(
                        self._active_message_id, card, in_thread=self._active_in_thread
                    )
                else:
                    self._progress_message_id = await self._replier.send_card(
                        chat_id, card
                    )
            except Exception as exc:
                logger.warning(
                    "SessionWorker[%s]: failed to send fallback progress card: %s",
                    self._session.context_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Permission callback
    # ------------------------------------------------------------------

    async def _on_permission(self, req: PermissionRequest) -> PermissionChoice:
        """Block until the user replies to the permission request.

        Sends a permission card to the chat, then waits on the session's
        permission future (resolved when the user clicks a button).

        When ``req.options`` is empty this is an auto-approve notification
        (fired by :func:`~nextme.acp.runtime._notify_auto_approved`): a simple
        informational text is sent and the method returns immediately without
        waiting for user input.

        If the user chooses a deny/reject option (label contains "deny" or
        "reject"), the active task is marked as cancelled so the agent stops
        processing after the current tool call completes.

        There is no timeout: the method blocks indefinitely until the user
        responds or the worker is cancelled (e.g. via ``/stop``).

        Args:
            req: The permission request from ACPRuntime.

        Returns:
            The user's :class:`~nextme.protocol.types.PermissionChoice`.
        """
        chat_id = self._session.context_id.split(":")[0]
        logger.info(
            "SessionWorker[%s]: permission request received id=%r description=%r",
            self._session.context_id,
            req.request_id,
            req.description,
        )

        # Auto-approve notification path: empty options means the runtime has
        # already responded on our behalf — just inform the user and return.
        if not req.options:
            desc = req.description or "工具调用"
            try:
                await self._replier.send_text(chat_id, f"已自动授权: {desc}")
            except Exception:
                logger.exception(
                    "SessionWorker[%s]: failed to send auto-approve notification",
                    self._session.context_id,
                )
            return PermissionChoice(request_id=req.request_id, option_index=1)

        # Build and send the permission card.
        # session_id = context_id so handle_card_action can look up the
        # UserContext in the registry; display_id = actual_id for the footer.
        card = self._replier.build_permission_card(
            description=req.description,
            options=req.options,
            session_id=self._session.context_id,
            project_name=self._session.project_name,
            executor=self._session.executor,
            display_id=self._session.actual_id or "",
        )
        try:
            await self._replier.send_card(chat_id, card)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send permission card",
                self._session.context_id,
            )

        # Register the pending future on the session.
        future = self._session.set_permission_pending(req.options)

        try:
            # Block indefinitely — no timeout, no fallback.
            # asyncio.shield keeps the future alive if the coroutine is
            # cancelled (e.g. /stop); cancel_permission() cleans it up below.
            choice = await asyncio.shield(future)
            logger.info(
                "SessionWorker[%s]: permission choice index=%d label=%r",
                self._session.context_id,
                choice.option_index,
                choice.option_label,
            )
            # If user chose a deny/reject option, cancel the active task so the
            # agent stops after the current tool call completes.
            if 0 < choice.option_index <= len(req.options):
                chosen_label = req.options[choice.option_index - 1].label
                if "deny" in chosen_label.lower() or "reject" in chosen_label.lower():
                    active_task = self._session.active_task
                    if active_task is not None and not active_task.canceled:
                        active_task.canceled = True
                        logger.info(
                            "SessionWorker[%s]: user denied permission — task marked canceled",
                            self._session.context_id,
                        )
            return choice
        except asyncio.CancelledError:
            self._session.cancel_permission()
            raise

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    async def _update_or_reply(self, task: Task, card_json: str) -> None:
        """Update the existing progress card in-place, or send a new reply.

        For **regular** (non-streaming) progress cards — ``_card_id`` is ``None``:
        PATCH in place so the user sees one card transition from "思考中..." to the
        final state.  If PATCH fails for any reason, fall through and send a new
        reply so the result is never silently dropped.

        For **streaming** (cardkit) progress cards — ``_card_id`` is set:
        The IM message holds a ``{"card_id":"xxx"}`` reference.  Feishu rejects
        PATCH requests on such messages with error 230099.  Skip the PATCH and
        send a new reply directly (the streaming card remains as a progress log).
        """
        if self._progress_message_id and self._card_id is None:
            # Non-streaming: try PATCH in place.
            try:
                await self._replier.update_card(self._progress_message_id, card_json)
                return
            except Exception as exc:
                logger.warning(
                    "SessionWorker[%s]: failed to update card in-place (%s); "
                    "falling back to new reply",
                    self._session.context_id,
                    exc,
                )
                # Fall through to send a new reply.

        # Streaming mode or PATCH failed: send a new reply.
        try:
            if task.message_id:
                await self._replier.reply_card(
                    task.message_id, card_json, in_thread=self._active_in_thread
                )
            else:
                chat_id = self._session.context_id.split(":")[0]
                await self._replier.send_card(chat_id, card_json)
        except Exception as exc:
            logger.error(
                "SessionWorker[%s]: failed to send fallback reply card: %s",
                self._session.context_id,
                exc,
            )

    async def _send_result(self, task: Task, content: str) -> None:
        """Update the progress card to show the final result.

        In streaming mode the streaming card already contains all agent output,
        so we append a compact completion footer instead of sending a second
        card (which would duplicate the answer).  Non-streaming mode PATCHes
        the progress card in-place as before.
        """
        elapsed_s = int(time.monotonic() - self._task_start)
        elapsed_str = _format_elapsed(elapsed_s)

        tool_count = self._run_card.tool_count if self._run_card is not None else 0

        if self._card_id:
            # Streaming mode — replace the full card (header + body) via
            # PUT /cards/:card_id so the execution-log card transitions to the
            # final answer card with a green "✅ 完成" header.
            final_card = self._replier.build_result_card(
                content=content or "(无输出)",
                title=f"✅ 完成 {self._proj}",
                template="green",
                session_id=self._session.actual_id or "",
                elapsed=elapsed_str,
                executor=self._session.executor,
                tool_count=tool_count,
            )
            self._sequence += 1
            try:
                await self._replier.update_card_entity(
                    self._card_id,
                    final_card,
                    self._sequence,
                )
                return
            except Exception as exc:
                logger.warning(
                    "SessionWorker[%s]: failed to finalize streaming card (%s); "
                    "falling back to new reply",
                    self._session.context_id,
                    exc,
                )
                # Fall through to send a new reply card.

        result_card = self._replier.build_result_card(
            content=content or "(无输出)",
            title=f"完成 {self._proj}",
            template="blue",
            session_id=self._session.actual_id or "",
            elapsed=elapsed_str,
            executor=self._session.executor,
            tool_count=tool_count,
        )
        await self._update_or_reply(task, result_card)

    async def _send_timeout(self, task: Task) -> None:
        """Update the progress card to show a task-timeout notice.

        The session and ACP runtime are intentionally *not* reset so the user
        can continue the conversation in the same context after the timeout.
        """
        elapsed_s = int(time.monotonic() - self._task_start)
        elapsed_str = _format_elapsed(elapsed_s)
        timeout_card = self._replier.build_error_card(
            f"任务执行超时（已运行 {elapsed_str}），会话上下文已保留，可继续发消息。",
            title=f"⏰ 超时 {self._proj}",
        )
        try:
            await self._update_or_reply(task, timeout_card)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send timeout reply for task %s",
                self._session.context_id,
                task.id,
            )

    async def _send_error(self, task: Task, error: str) -> None:
        """Update the progress card to show an error."""
        error_card = self._replier.build_error_card(error, title=f"出错了 {self._proj}")
        await self._update_or_reply(task, error_card)

    async def _send_cancelled(self, task: Task) -> None:
        """Update the progress card to show a cancellation notice."""
        cancel_card = self._replier.build_result_card(
            content="操作已取消",
            title=f"已取消 {self._proj}",
            template="grey",
        )
        try:
            await self._update_or_reply(task, cancel_card)
        except Exception:
            logger.exception(
                "SessionWorker[%s]: failed to send cancel reply for task %s",
                self._session.context_id,
                task.id,
            )
