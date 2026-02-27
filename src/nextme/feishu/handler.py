"""Route incoming Feishu IM events to the task dispatcher.

The lark-oapi SDK calls event handler functions synchronously from its own
internal thread.  We bridge that into the running asyncio event loop by
scheduling coroutines via ``loop.call_soon_threadsafe`` / ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Protocol

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from nextme.feishu.dedup import MessageDedup
from nextme.protocol.types import Reply, Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal dispatcher protocol so this module does not import concrete classes.
# ---------------------------------------------------------------------------

class _Dispatcher(Protocol):
    async def dispatch(self, task: Task) -> None: ...
    def handle_card_action(self, session_id: str, index: int, project_name: str = "") -> None: ...


# ---------------------------------------------------------------------------
# MessageHandler
# ---------------------------------------------------------------------------

class MessageHandler:
    """Convert lark P2ImMessageReceiveV1 events into Task objects and dispatch them."""

    def __init__(self, dedup: MessageDedup, dispatcher: _Dispatcher) -> None:
        self._dedup = dedup
        self._dispatcher = dispatcher
        # The asyncio event loop that owns the dispatcher.  Captured lazily on
        # the first call from the asyncio side (see get_event_handler).
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public: called from asyncio context to capture the running loop.
    # ------------------------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture *loop* so that handle_message can schedule coroutines onto it."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Public: create a lark EventDispatcherHandler that wraps this handler.
    # ------------------------------------------------------------------

    def build_event_dispatcher(self) -> lark.EventDispatcherHandler:
        """Return a configured lark EventDispatcherHandler."""
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )

    # ------------------------------------------------------------------
    # lark-oapi callback (called synchronously, potentially from another thread)
    # ------------------------------------------------------------------

    def _on_message_receive(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """Entry point invoked by the lark SDK for every incoming IM message."""
        try:
            self.handle_message(data)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Unhandled error in _on_message_receive")

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """Handle a card button click (card.action.trigger).

        Resolves a pending permission request when the user clicks a permission
        card button.  The response returns a toast and an updated card that
        replaces the buttons with a confirmation message so they cannot be
        clicked again.
        """
        resp = P2CardActionTriggerResponse()
        try:
            event = data.event
            if event is None or event.action is None:
                return resp

            value: dict = event.action.value or {}
            if value.get("action") != "permission_choice":
                return resp

            session_id: str = value.get("session_id", "")
            index_str: str = value.get("index", "")
            project_name: str = value.get("project_name", "")
            label: str = value.get("label", index_str)
            executor: str = value.get("executor", "")
            if not session_id or not index_str:
                return resp

            try:
                index = int(index_str)
            except (ValueError, TypeError):
                logger.warning("_on_card_action: invalid index %r", index_str)
                return resp

            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(
                    self._dispatcher.handle_card_action, session_id, index, project_name
                )
            else:
                logger.warning("_on_card_action: no running event loop, dropping action")

            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = "已收到"

            # Return an updated card that replaces the buttons with a
            # confirmation message so the user cannot click again.
            confirmed_elements: list[dict] = [
                {"tag": "markdown", "content": f"✅ 已选择: {label}"},
            ]
            footer_parts: list[str] = []
            if session_id:
                footer_parts.append(f"🆔 {session_id}")
            if executor:
                footer_parts.append(executor)
            if footer_parts:
                confirmed_elements.append({"tag": "hr"})
                confirmed_elements.append(
                    {"tag": "markdown", "content": " | ".join(footer_parts)}
                )
            confirmed_card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "需要授权"},
                    "template": "orange",
                },
                "body": {"elements": confirmed_elements},
            }
            resp.card = CallBackCard()
            resp.card.type = "raw"
            resp.card.data = confirmed_card
        except Exception:
            logger.exception("Unhandled error in _on_card_action")
        return resp

    def handle_message(self, data: Any) -> None:
        """Extract fields from *data*, dedup, create a Task, and dispatch it.

        *data* is a ``lark.im.v1.P2ImMessageReceiveV1`` instance but typed as
        ``Any`` so callers (and tests) can pass raw dicts during unit-testing.
        """
        try:
            message = data.event.message  # type: ignore[union-attr]
            sender = data.event.sender    # type: ignore[union-attr]
        except AttributeError:
            logger.warning("handle_message: unexpected data shape, skipping")
            return

        message_id: str = getattr(message, "message_id", "") or ""
        chat_id: str = getattr(message, "chat_id", "") or ""
        chat_type: str = getattr(message, "chat_type", "") or ""
        user_id: str = (
            getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
        )

        if not message_id or not chat_id:
            logger.warning(
                "handle_message: missing message_id or chat_id, skipping"
            )
            return

        if self._dedup.is_duplicate(message_id):
            return

        text = self._extract_text_from_message(message)
        if not text:
            logger.debug("handle_message: empty text, skipping message_id=%s", message_id)
            return

        session_id = f"{chat_id}:{user_id}"

        async def _reply_fn(reply: Reply) -> None:
            # Placeholder reply callback; the real one is injected by the
            # dispatcher or the session layer when the Task is processed.
            # Here we log so nothing is silently lost if a Task escapes without
            # a proper callback being set.
            logger.warning(
                "Default reply_fn called for session=%s reply_type=%s",
                session_id,
                reply.type,
            )

        task = Task(
            id=str(uuid.uuid4()),
            content=text,
            session_id=session_id,
            reply_fn=_reply_fn,
            message_id=message_id,
            chat_type=chat_type,
            created_at=datetime.now(),
        )

        logger.info(
            "New task: id=%s session=%s message_id=%s chat_type=%s text=%.80r",
            task.id,
            session_id,
            message_id,
            chat_type,
            text,
        )

        self._schedule_dispatch(task)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _schedule_dispatch(self, task: Task) -> None:
        """Schedule ``dispatcher.dispatch(task)`` on the asyncio event loop."""
        loop = self._loop
        if loop is None:
            # Fallback: try to get the running loop (works when called from
            # the same thread as the event loop).
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                logger.error("No event loop available; dropping task %s", task.id)
                return

        if loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._dispatcher.dispatch(task), loop
            )
        else:
            logger.error(
                "Event loop is not running; dropping task %s", task.id
            )

    @staticmethod
    def _extract_text_from_message(message: Any) -> str:
        """Return plain text extracted from a Feishu message object.

        Handles:
        - ``text`` messages: ``{"text": "..."}``
        - ``post`` (rich-text) messages: concatenate all ``text`` leaf nodes
        - Any other type: return empty string
        """
        msg_type: str = getattr(message, "message_type", "") or ""
        raw_content: str = getattr(message, "content", "") or ""

        if not raw_content:
            return ""

        try:
            content_obj = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError):
            logger.debug("_extract_text: could not parse content JSON")
            return ""

        if msg_type == "text":
            return str(content_obj.get("text", "")).strip()

        if msg_type == "post":
            # Rich-text (post) structure:
            # {"zh_cn": {"title": "...", "content": [[{"tag": "text", "text": "..."}, ...], ...]}}
            # We concatenate all leaf text nodes across all languages / paragraphs.
            texts: list[str] = []
            for lang_body in content_obj.values():
                paragraphs = lang_body.get("content", []) if isinstance(lang_body, dict) else []
                for paragraph in paragraphs:
                    for node in paragraph:
                        if isinstance(node, dict) and node.get("tag") == "text":
                            texts.append(node.get("text", ""))
            return " ".join(t.strip() for t in texts if t.strip())

        logger.debug("_extract_text: unsupported message type %r", msg_type)
        return ""

    # ------------------------------------------------------------------
    # Legacy / convenience: accept raw dict (used in tests)
    # ------------------------------------------------------------------

    def _extract_text(self, message: dict) -> str:  # noqa: D401
        """Extract plain text from a raw message dict (convenience / test shim)."""

        class _FakeMsg:
            def __init__(self, d: dict) -> None:
                self.message_type = d.get("message_type", d.get("msg_type", ""))
                self.content = d.get("content", "")

        return self._extract_text_from_message(_FakeMsg(message))
