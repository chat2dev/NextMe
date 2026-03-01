"""Build and send replies to Feishu (Lark).

Supports plain markdown text messages, interactive cards (v2 schema), emoji
reactions, and in-place card updates for streaming progress.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid_mod

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    IdConvertCardRequest,
    IdConvertCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# Element ID for the main content element in streaming progress cards.
_CONTENT_ELEMENT_ID = "content_el"

from nextme.protocol.types import PermOption

logger = logging.getLogger(__name__)


class FeishuReplier:
    """High-level helper for sending messages and interactive cards to Feishu."""

    def __init__(self, client: lark.Client) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Sending primitives
    # ------------------------------------------------------------------

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send a markdown text message to *chat_id*.  Returns the message_id."""
        content = json.dumps({"text": text})
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            logger.error(
                "send_text failed: code=%s msg=%s",
                response.code,
                response.msg,
            )
            return ""
        message_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("send_text -> message_id=%s", message_id)
        return message_id

    async def send_card(self, chat_id: str, card_json: str) -> str:
        """Send an interactive card to *chat_id*.  Returns the message_id."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            logger.error(
                "send_card failed: code=%s msg=%s",
                response.code,
                response.msg,
            )
            return ""
        message_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("send_card -> message_id=%s", message_id)
        return message_id

    async def update_card(self, message_id: str, card_json: str) -> None:
        """Replace the content of an existing interactive card (for progress updates)."""
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(card_json)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.apatch(request)
        if not response.success():
            logger.error(
                "update_card failed: message_id=%s code=%s msg=%s",
                message_id,
                response.code,
                response.msg,
            )
        else:
            logger.debug("update_card ok: message_id=%s", message_id)

    async def create_card(self, card_json: str) -> str:
        """Create a card via the cardkit API and return its *card_id*.

        The card JSON should be built with :meth:`build_streaming_progress_card`
        (includes element ``id`` fields).  Once created, call
        :meth:`enable_streaming_mode` to lift Feishu QPS limits, then send the
        card via :meth:`send_card_by_id` or :meth:`reply_card_by_id` and update
        element-by-element via :meth:`stream_set_content`.

        Returns ``""`` on failure (caller falls back to regular card flow).
        """
        request = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(card_json)
                .build()
            )
            .build()
        )
        response = await self._client.cardkit.v1.card.acreate(request)
        if not response.success():
            logger.warning(
                "create_card failed: code=%s msg=%s",
                response.code,
                response.msg,
            )
            return ""
        card_id: str = response.data.card_id or ""  # type: ignore[union-attr]
        logger.debug("create_card -> card_id=%s", card_id)
        return card_id

    async def enable_streaming_mode(self, card_id: str) -> bool:
        """Enable streaming mode on a cardkit card entity via PATCH /settings.

        ``streaming_mode`` must NOT be placed inside the card content JSON,
        because the IM renderer doesn't recognise it and rejects the card with
        error 200621 "parse card json err".  Setting it via the settings API
        instead lifts the Feishu QPS limit for PUT /content calls on this card.

        Returns ``True`` on success, ``False`` on failure (streaming continues
        but QPS limits may apply).
        """
        settings_json = json.dumps({"config": {"streaming_mode": True}})
        request = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings_json)
                .sequence(1)
                .build()
            )
            .build()
        )
        response = await self._client.cardkit.v1.card.asettings(request)
        if not response.success():
            logger.warning(
                "enable_streaming_mode failed: card_id=%s code=%s msg=%s",
                card_id,
                response.code,
                response.msg,
            )
            return False
        logger.debug("enable_streaming_mode ok: card_id=%s", card_id)
        return True

    async def send_card_by_id(self, chat_id: str, card_id: str) -> str:
        """Send a cardkit card (referenced by *card_id*) to *chat_id*.

        Uses ``im/v1`` with ``msg_type="interactive"`` and content
        ``{"type":"card","data":{"card_id":"..."}}`` so Feishu resolves the
        live card entity from cardkit.
        Returns the new ``message_id``, or ``""`` on failure.
        """
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            logger.error(
                "send_card_by_id failed: chat_id=%s code=%s msg=%s",
                chat_id,
                response.code,
                response.msg,
            )
            return ""
        message_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("send_card_by_id -> message_id=%s", message_id)
        return message_id

    async def reply_card_by_id(
        self, message_id: str, card_id: str, in_thread: bool = True
    ) -> str:
        """Reply to *message_id* with a cardkit card referenced by *card_id*.

        Uses ``im/v1`` reply with ``msg_type="interactive"`` and content
        ``{"type":"card","data":{"card_id":"..."}}`` so Feishu resolves the
        live card entity from cardkit.
        Returns the new ``message_id``, or ``""`` on failure.
        """
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(content)
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.areply(request)
        if not response.success():
            logger.error(
                "reply_card_by_id failed: message_id=%s code=%s msg=%s",
                message_id,
                response.code,
                response.msg,
            )
            return ""
        new_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("reply_card_by_id -> new message_id=%s", new_id)
        return new_id

    async def get_card_id(self, message_id: str) -> str:
        """Convert an im *message_id* to a cardkit *card_id* for streaming updates.

        Returns ``""`` on failure (caller falls back to full-card PATCH).
        """
        request = (
            IdConvertCardRequest.builder()
            .request_body(
                IdConvertCardRequestBody.builder()
                .message_id(message_id)
                .build()
            )
            .build()
        )
        response = await self._client.cardkit.v1.card.aid_convert(request)
        if not response.success():
            logger.warning(
                "get_card_id failed: message_id=%s code=%s msg=%s",
                message_id,
                response.code,
                response.msg,
            )
            return ""
        card_id: str = response.data.card_id or ""  # type: ignore[union-attr]
        logger.debug("get_card_id: message_id=%s -> card_id=%s", message_id, card_id)
        return card_id

    async def stream_set_content(self, card_id: str, full_text: str, sequence: int) -> None:
        """Set the full text of the content element for typewriter streaming.

        Uses the cardkit ``PUT /elements/:element_id/content`` endpoint, which
        is the official Feishu typewriter API: callers pass the ever-growing
        **full accumulated text** (not just the delta), and Feishu animates the
        difference as a typewriter effect.

        The *sequence* number must be strictly increasing across all calls for
        the same card so Feishu can discard out-of-order deliveries.
        """
        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(_CONTENT_ELEMENT_ID)
            .request_body(
                ContentCardElementRequestBody.builder()
                .uuid(str(_uuid_mod.uuid4()))
                .content(full_text)
                .sequence(sequence)
                .build()
            )
            .build()
        )
        response = await self._client.cardkit.v1.card_element.acontent(request)
        if not response.success():
            logger.warning(
                "stream_set_content failed: card_id=%s seq=%d code=%s msg=%s",
                card_id,
                sequence,
                response.code,
                response.msg,
            )
        else:
            logger.debug("stream_set_content ok: card_id=%s seq=%d", card_id, sequence)

    async def update_card_entity(self, card_id: str, card_json: str, sequence: int) -> None:
        """Replace the full content of a cardkit card entity via PUT /cards/:card_id.

        Used to finalize a streaming card — updates the header title/template
        (e.g. "思考中..." → "完成") and body in one atomic operation.

        The *sequence* number must be strictly increasing across all calls for
        the same card so Feishu can discard out-of-order deliveries.
        """
        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(
                    Card.builder()
                    .type("card_json")
                    .data(card_json)
                    .build()
                )
                .uuid(str(_uuid_mod.uuid4()))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        response = await self._client.cardkit.v1.card.aupdate(request)
        if not response.success():
            logger.warning(
                "update_card_entity failed: card_id=%s seq=%d code=%s msg=%s",
                card_id,
                sequence,
                response.code,
                response.msg,
            )
        else:
            logger.debug("update_card_entity ok: card_id=%s seq=%d", card_id, sequence)

    async def send_reaction(self, message_id: str, emoji: str = "SMILE") -> None:
        """Add an emoji reaction to the message identified by *message_id*."""
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build())
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message_reaction.acreate(request)
        if not response.success():
            logger.error(
                "send_reaction failed: message_id=%s emoji=%s code=%s msg=%s",
                message_id,
                emoji,
                response.code,
                response.msg,
            )
        else:
            logger.debug("send_reaction ok: message_id=%s emoji=%s", message_id, emoji)

    async def reply_text(
        self, message_id: str, text: str, in_thread: bool = True
    ) -> str:
        """Reply to *message_id* with a plain-text message.

        Args:
            message_id: The Feishu message_id to reply to.
            text: The text content to send.
            in_thread: When ``True`` (default), reply appears inside a thread.

        Returns:
            The new message_id of the sent reply, or ``""`` on failure.
        """
        content = json.dumps({"text": text})
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(content)
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.areply(request)
        if not response.success():
            logger.error(
                "reply_text failed: message_id=%s code=%s msg=%s",
                message_id,
                response.code,
                response.msg,
            )
            return ""
        new_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("reply_text -> new message_id=%s", new_id)
        return new_id

    async def reply_card(
        self, message_id: str, card_json: str, in_thread: bool = True
    ) -> str:
        """Reply to *message_id* with an interactive card.

        Args:
            message_id: The Feishu message_id to reply to.
            card_json: The card JSON string.
            in_thread: When ``True`` (default), reply appears inside a thread.

        Returns:
            The new message_id of the sent card, or ``""`` on failure.
        """
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.areply(request)
        if not response.success():
            logger.error(
                "reply_card failed: message_id=%s code=%s msg=%s",
                message_id,
                response.code,
                response.msg,
            )
            return ""
        new_id: str = response.data.message_id  # type: ignore[union-attr]
        logger.debug("reply_card -> new message_id=%s", new_id)
        return new_id

    # ------------------------------------------------------------------
    # Card builders
    # ------------------------------------------------------------------

    def build_progress_card(
        self,
        status: str,
        content: str,
        title: str = "⏳ 思考中...",
    ) -> str:
        """Return a card JSON string for in-progress status updates (fallback path).

        Used when cardkit streaming is unavailable.  Sent via ``im/v1`` which
        does **not** support element ``id`` fields or ``streaming_mode``.
        """
        elements: list[dict] = [
            {"tag": "markdown", "content": content},
        ]
        if status:
            elements.append({"tag": "markdown", "content": f"_{status}_"})
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "yellow",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_streaming_progress_card(
        self,
        content: str = "思考中...",
        title: str = "⏳ 思考中...",
    ) -> str:
        """Return a card JSON for cardkit creation with element IDs for streaming.

        ``streaming_mode: true`` is included in the card config as required by
        the cardkit API (the IM renderer never sees this JSON — it only receives
        a ``{"type":"card","data":{"card_id":"..."}}`` reference).  Element IDs
        allow :meth:`stream_set_content` to target the content element via the
        PUT /content typewriter API.
        """
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True, "streaming_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "yellow",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content, "element_id": _CONTENT_ELEMENT_ID},
                ]
            },
        }
        return json.dumps(card, ensure_ascii=False)

    def build_result_card(
        self,
        content: str,
        title: str = "✅ 完成",
        template: str = "blue",
        reasoning: str = "",
        session_id: str = "",
        elapsed: str = "",
        executor: str = "",
        tool_count: int = 0,
    ) -> str:
        """Return a card JSON string for the final result."""
        elements: list[dict] = [
            {"tag": "markdown", "content": content},
        ]
        if reasoning:
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "plain_text", "content": "思考过程"},
                        "vertical_align": "center",
                    },
                    "elements": [
                        {"tag": "markdown", "content": reasoning},
                    ],
                }
            )
        if tool_count > 0:
            elements.append({"tag": "markdown", "content": f"🔧 工具调用 **{tool_count}** 次"})
        footer_parts: list[str] = []
        if session_id:
            footer_parts.append(f"🆔 {session_id}")
        if executor:
            footer_parts.append(executor)
        if elapsed:
            footer_parts.append(f"耗时: {elapsed}")
        if footer_parts:
            elements.append({"tag": "hr"})
            elements.append(
                {"tag": "markdown", "content": " | ".join(footer_parts)}
            )
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_permission_card(
        self,
        description: str,
        options: list[PermOption],
        session_id: str = "",
        project_name: str = "",
        executor: str = "",
        display_id: str = "",
    ) -> str:
        """Return a card JSON string for a permission request with numbered buttons.

        Args:
            session_id: The context_id (``oc_xxx:ou_xxx``) stored in the button
                value so the card action callback can look up the correct
                UserContext in the session registry.
            display_id: The human-readable session ID shown in the card footer
                (usually ``actual_id``, the Claude/ACP session UUID).  Falls
                back to *session_id* when empty.
        """
        elements: list[dict] = [
            {"tag": "markdown", "content": description},
            {"tag": "hr"},
        ]

        # One button per option so each appears on its own line on mobile.
        # schema 2.0 does not support the legacy "action" wrapper tag;
        # buttons are placed directly as body elements instead.
        for opt in options:
            label = f"{opt.index}. {opt.label}"
            if opt.description:
                label += f" — {opt.description}"
            elements.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if opt.index == 1 else "default",
                    "value": {
                        "action": "permission_choice",
                        "index": str(opt.index),
                        "session_id": session_id,
                        "project_name": project_name,
                        "label": label,
                        "executor": executor,
                        "display_id": display_id,
                    },
                }
            )

        footer_id = display_id or session_id
        footer_parts: list[str] = []
        if footer_id:
            footer_parts.append(f"🆔 {footer_id}")
        if executor:
            footer_parts.append(executor)
        if footer_parts:
            elements.append({"tag": "hr"})
            elements.append(
                {"tag": "markdown", "content": " | ".join(footer_parts)}
            )

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": "需要授权"},
                "template": "orange",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_error_card(self, error: str, title: str = "❌ 出错了") -> str:
        """Return a card JSON string for an error message."""
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": error},
                ]
            },
        }
        return json.dumps(card, ensure_ascii=False)

    def build_help_card(self, commands: list[tuple[str, str]]) -> str:
        """Return a card JSON string listing available commands.

        *commands* is a list of (command, description) pairs.
        """
        lines = ["| 命令 | 说明 |", "| --- | --- |"]
        for cmd, desc in commands:
            lines.append(f"| `{cmd}` | {desc} |")
        table_md = "\n".join(lines)

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📖 帮助"},
                "template": "green",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": table_md},
                ]
            },
        }
        return json.dumps(card, ensure_ascii=False)

    def build_info_card(self, title: str, content: str, template: str = "blue") -> str:
        """Return a card JSON string for informational messages.

        Used by /whoami, /status, /acl commands.
        """
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                ]
            },
        }
        return json.dumps(card, ensure_ascii=False)
