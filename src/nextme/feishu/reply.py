"""Build and send replies to Feishu (Lark).

Supports plain markdown text messages, interactive cards (v2 schema), emoji
reactions, and in-place card updates for streaming progress.
"""

from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

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

    # ------------------------------------------------------------------
    # Card builders
    # ------------------------------------------------------------------

    def build_progress_card(
        self,
        status: str,
        content: str,
        title: str = "思考中...",
    ) -> str:
        """Return a card JSON string for in-progress status updates."""
        elements: list[dict] = [
            {"tag": "markdown", "content": content},
        ]
        if status:
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": status},
                    ],
                }
            )
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "yellow",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_result_card(
        self,
        content: str,
        title: str = "完成",
        template: str = "blue",
        reasoning: str = "",
        session_id: str = "",
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
        footer_parts: list[str] = []
        if session_id:
            footer_parts.append(f"session: {session_id}")
        if footer_parts:
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": " | ".join(footer_parts)},
                    ],
                }
            )
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
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
    ) -> str:
        """Return a card JSON string for a permission request with numbered buttons."""
        elements: list[dict] = [
            {"tag": "markdown", "content": description},
            {"tag": "hr"},
        ]

        # One action row per option so each button gets its own line on mobile.
        for opt in options:
            label = f"{opt.index}. {opt.label}"
            if opt.description:
                label += f" — {opt.description}"
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label},
                            "type": "primary" if opt.index == 1 else "default",
                            "value": {
                                "action": "permission_choice",
                                "index": str(opt.index),
                                "session_id": session_id,
                            },
                        }
                    ],
                }
            )

        if session_id:
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"session: {session_id}"},
                    ],
                }
            )

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "需要授权"},
                "template": "orange",
            },
            "body": {"elements": elements},
        }
        return json.dumps(card, ensure_ascii=False)

    def build_error_card(self, error: str) -> str:
        """Return a card JSON string for an error message."""
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "出错了"},
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
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "帮助"},
                "template": "green",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": table_md},
                ]
            },
        }
        return json.dumps(card, ensure_ascii=False)
