"""Route incoming Feishu IM events to the task dispatcher.

The lark-oapi SDK calls event handler functions synchronously from its own
internal thread.  We bridge that into the running asyncio event loop by
scheduling coroutines via ``loop.call_soon_threadsafe`` / ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

# Feishu group-chat text messages encode @mentions as "@_user_1", "@_user_2", …
# at the beginning of the text field.  Strip them so commands like /help are
# recognised regardless of whether the user typed "@Bot /help" or just "/help".
_MENTION_PREFIX_RE = re.compile(r"^(?:@_user_\d+\s*)+")


def _extract_mentions(message: Any) -> list[dict[str, Any]]:
    """Return deduplicated list of {name, open_id} from a Feishu message.

    Handles two formats:
    - text messages: message.mentions SDK list (each item has .id.open_id and .name)
    - post messages: inline tag=="at" nodes in content JSON
    """
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    def _add(name: str, open_id: str) -> None:
        if open_id and open_id not in seen:
            seen.add(open_id)
            result.append({"name": name or "", "open_id": open_id})

    msg_type: str = getattr(message, "message_type", "") or ""

    # text messages: SDK provides message.mentions
    if msg_type == "text":
        for m in getattr(message, "mentions", None) or []:
            open_id = getattr(getattr(m, "id", None), "open_id", "") or ""
            name = getattr(m, "name", "") or ""
            _add(name, open_id)
        return result

    # post (rich-text) messages: parse tag=="at" nodes from content JSON
    if msg_type == "post":
        try:
            raw = getattr(message, "content", "") or ""
            content_obj = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return result
        for lang_body in content_obj.values():
            paragraphs = lang_body.get("content", []) if isinstance(lang_body, dict) else []
            for paragraph in paragraphs:
                for node in paragraph:
                    if isinstance(node, dict) and node.get("tag") == "at":
                        open_id = node.get("user_id", "") or ""
                        name = node.get("user_name", "") or ""
                        _add(name, open_id)
    return result


# ---------------------------------------------------------------------------
# Minimal dispatcher protocol so this module does not import concrete classes.
# ---------------------------------------------------------------------------

class _Dispatcher(Protocol):
    async def dispatch(self, task: Task) -> None: ...
    def handle_card_action(self, session_id: str, index: int, project_name: str = "") -> None: ...
    async def handle_acl_card_action(self, action_data: dict) -> None: ...


# ---------------------------------------------------------------------------
# MessageHandler
# ---------------------------------------------------------------------------

class MessageHandler:
    """Convert lark P2ImMessageReceiveV1 events into Task objects and dispatch them."""

    def __init__(self, dedup: MessageDedup, dispatcher: _Dispatcher,
                 require_at_mention: bool = True) -> None:
        self._dedup = dedup
        self._dispatcher = dispatcher
        self._require_at_mention = require_at_mention
        # The asyncio event loop that owns the dispatcher.  Captured lazily on
        # the first call from the asyncio side (see get_event_handler).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_threads: set[str] = set()
        # Format: "chat_id:thread_root_id" — threads the bot is actively participating in.

    # ------------------------------------------------------------------
    # Public: called from asyncio context to capture the running loop.
    # ------------------------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture *loop* so that handle_message can schedule coroutines onto it."""
        self._loop = loop

    def restore_active_threads(self, thread_keys: set[str]) -> None:
        """Restore active thread set from persisted state on startup."""
        self._active_threads = set(thread_keys)
        logger.info("MessageHandler: restored %d active thread(s)", len(thread_keys))

    def deregister_thread(self, chat_id: str, thread_root_id: str) -> None:
        """Remove a thread from the active set (called when /done closes a thread)."""
        key = f"{chat_id}:{thread_root_id}"
        self._active_threads.discard(key)
        logger.info("MessageHandler: deregistered thread %r", key)

    def register_thread(self, chat_id: str, thread_root_id: str) -> None:
        """Add a thread to the active set (called when dispatcher accepts the thread after limit check)."""
        key = f"{chat_id}:{thread_root_id}"
        self._active_threads.add(key)
        logger.info("MessageHandler: registered thread %r", key)

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

            if value.get("action") in ("acl_apply", "acl_review"):
                action_data = dict(value)
                # Inject operator open_id from card event
                operator_id = ""
                try:
                    if data.event and hasattr(data.event, "operator"):
                        operator_id = getattr(data.event.operator, "open_id", "") or ""
                except Exception:
                    pass
                action_data["operator_id"] = operator_id

                loop = self._loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._dispatcher.handle_acl_card_action(action_data), loop
                    )
                else:
                    logger.warning(
                        "_on_card_action: no running loop for acl action %r",
                        value.get("action"),
                    )
                resp.toast = CallBackToast()
                resp.toast.type = "info"
                resp.toast.content = "已收到"

                # Immediately replace the card with a "processing" state so the
                # reviewer cannot click the buttons again while the async handler runs.
                action = value.get("action")
                app_id_str = value.get("app_id", "")
                if action == "acl_review":
                    decision = value.get("decision", "")
                    decision_label = "批准" if decision == "approved" else "拒绝"
                    processing_card: dict = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": f"📋 权限申请 #{app_id_str}"},
                            "template": "grey",
                        },
                        "body": {"elements": [
                            {"tag": "markdown", "content": f"⏳ 正在{decision_label}，结果将通过私信通知你…"}
                        ]},
                    }
                else:  # acl_apply
                    processing_card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "🔒 权限申请"},
                            "template": "grey",
                        },
                        "body": {"elements": [
                            {"tag": "markdown", "content": "⏳ 申请已提交，等待审批通知…"}
                        ]},
                    }
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = processing_card
                return resp

            if value.get("action") != "permission_choice":
                return resp

            session_id: str = value.get("session_id", "")
            index_str: str = value.get("index", "")
            project_name: str = value.get("project_name", "")
            label: str = value.get("label", index_str)
            executor: str = value.get("executor", "")
            display_id: str = value.get("display_id", "")
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
            footer_id = display_id or session_id
            footer_parts: list[str] = []
            if footer_id:
                footer_parts.append(f"🆔 {footer_id}")
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

        root_id: str = getattr(message, "root_id", "") or ""
        is_group = chat_type == "group"

        if is_group:
            if root_id:
                # 话题内回复：元命令（/xxx）始终处理；普通消息受 require_at_mention 控制。
                is_meta = text.startswith("/")
                if self._require_at_mention and not is_meta and not self._has_bot_mention(message):
                    logger.debug(
                        "handle_message: ignoring thread reply without @mention root_id=%s",
                        root_id,
                    )
                    return
                thread_key = f"{chat_id}:{root_id}"
                if thread_key not in self._active_threads:
                    logger.debug(
                        "handle_message: ignoring reply in unknown thread root_id=%s", root_id
                    )
                    return
                session_id = thread_key
                thread_root_id = root_id
            else:
                # 群聊根消息：受 require_at_mention 控制
                if self._require_at_mention and not self._has_bot_mention(message):
                    logger.debug(
                        "handle_message: ignoring group root message without @mention message_id=%s",
                        message_id,
                    )
                    return
                session_id = f"{chat_id}:{message_id}"
                thread_root_id = message_id
                # 注册新话题
                self._active_threads.add(f"{chat_id}:{message_id}")
        else:
            # p2p：保持不变
            session_id = f"{chat_id}:{user_id}"
            thread_root_id = ""

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
            mentions=_extract_mentions(message),
            user_id=user_id,
            thread_root_id=thread_root_id,
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

    @staticmethod
    def _has_bot_mention(message: Any) -> bool:
        """Return True if the message has any @mention (indicating bot was @-mentioned)."""
        mentions = getattr(message, "mentions", None) or []
        return len(mentions) > 0

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
            text = str(content_obj.get("text", "")).strip()
            # Strip leading @mention placeholders inserted by Feishu for group chats.
            text = _MENTION_PREFIX_RE.sub("", text).strip()
            return text

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
