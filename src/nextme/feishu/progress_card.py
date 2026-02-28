"""RunProgressCard — streaming execution progress card for Feishu.

Maintains a sliding window of events (tool calls + text snippets) and renders
them as a structured Feishu interactive card body.  The card is updated via the
CardKit full-card-update API (PUT /cards/:card_id) on every significant event.

Life-cycle
----------
1. Create:  ``card = RunProgressCard()``
2. Events:  ``card.add_tool(name, elapsed_s)`` / ``card.add_text_chunk(delta)``
3. Render:  ``card.build_card("running", "【project】")`` → JSON-dump and send
4. Finalise: caller replaces with ``build_result_card`` at completion
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Default sliding-window size (mirrors lark-code-bot's max_events=8).
_DEFAULT_MAX_EVENTS = 8

# Max chars of LLM text tail shown in the sliding window.
_MAX_TEXT_PREVIEW = 120


def _fmt_elapsed(seconds: int) -> str:
    """Return a compact elapsed string, e.g. ``'5s'``, ``'1m 30s'``."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if s else f"{m}m"


@dataclass
class _Event:
    kind: str     # "tool" | "text"
    key: str      # dedup key for in-place update
    content: str  # markdown line shown in the card


class RunProgressCard:
    """Tracks execution events and renders a structured Feishu progress card.

    The card shows a bounded sliding window of recent events:

    * **Tool calls** — ``🔧 `ToolName(args)` · 3s``
    * **Text chunks** — ``💬 <tail of accumulated LLM output>``

    When the window overflows, tool events are counted as omitted and their
    count is displayed so the user can see how many were hidden.

    Args:
        max_events: Maximum visible entries in the sliding window.
            Older entries are silently dropped (tool count still tracked).
    """

    def __init__(self, max_events: int = _DEFAULT_MAX_EVENTS) -> None:
        self._events: list[_Event] = []
        self._omitted: int = 0
        self._tool_count: int = 0
        self._max_events = max(1, max_events)
        self._start: float = time.monotonic()
        self._text: str = ""  # full accumulated LLM text (for tail truncation)

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def tool_count(self) -> int:
        """Total number of distinct tool-call events seen so far."""
        return self._tool_count

    @property
    def elapsed_s(self) -> int:
        """Seconds elapsed since the card was created."""
        return int(time.monotonic() - self._start)

    # ------------------------------------------------------------------
    # Event mutators
    # ------------------------------------------------------------------

    def add_tool(self, tool_name: str, elapsed_s: int) -> None:
        """Add (or update) a tool-call entry in the sliding window.

        If the immediately-preceding event is a tool with the same base name
        (the identifier before the first ``(``) it is updated in-place.
        This handles the :class:`~nextme.acp.direct_runtime.DirectClaudeRuntime`
        pattern of emitting the bare name first then the formatted
        ``Name(args)`` version on ``content_block_stop``.

        Args:
            tool_name: Tool identifier, e.g. ``"Bash(ls -la)"`` or ``"Bash"``.
            elapsed_s: Seconds since task start (shown next to the entry).
        """
        base = tool_name.split("(")[0]
        content = f"🔧 `{tool_name}` · {_fmt_elapsed(elapsed_s)}"
        key = f"tool|{base}"

        # Update last event in-place if same tool base (avoids duplicate entries).
        if self._events and self._events[-1].kind == "tool":
            last_base = self._events[-1].key.split("|", 1)[-1]
            if last_base == base:
                self._events[-1] = _Event(kind="tool", key=key, content=content)
                return

        self._tool_count += 1
        self._push(_Event(kind="tool", key=key, content=content))

    def add_text_chunk(self, delta: str) -> None:
        """Accumulate a text delta and refresh the single text slot in the window.

        Only the *tail* of the accumulated text (last ``_MAX_TEXT_PREVIEW``
        chars) is shown to keep the card concise.

        Args:
            delta: Incremental text token from the LLM.
        """
        if not delta:
            return
        self._text += delta
        tail = self._text
        if len(tail) > _MAX_TEXT_PREVIEW:
            tail = "…" + tail[-_MAX_TEXT_PREVIEW:]
        content = f"💬 {tail}"

        # Update the single "text" slot or push a new one.
        for i, e in enumerate(self._events):
            if e.kind == "text":
                self._events[i] = _Event(kind="text", key="text", content=content)
                return
        self._push(_Event(kind="text", key="text", content=content))

    # ------------------------------------------------------------------
    # Card builder
    # ------------------------------------------------------------------

    def build_card(self, status: str, project_tag: str) -> dict:
        """Build the full card dict for the current execution state.

        Args:
            status: One of ``"running"``, ``"done"``, ``"error"``,
                ``"cancelled"``.
            project_tag: Project label shown in the header, e.g.
                ``"【myproject】"``.

        Returns:
            A dict ready for ``json.dumps`` and sending to Feishu CardKit.
        """
        title, template = _resolve_header(status, project_tag)
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {"elements": self._render_elements()},
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _push(self, event: _Event) -> None:
        self._events.append(event)
        while len(self._events) > self._max_events:
            removed = self._events.pop(0)
            if removed.kind == "tool":
                self._omitted += 1

    def _render_elements(self) -> list[dict]:
        els: list[dict] = [{"tag": "markdown", "content": "**执行日志**"}]
        if self._omitted:
            els.append({
                "tag": "markdown",
                "content": f"_已省略 {self._omitted} 个早期事件_",
            })
        els.append({"tag": "hr"})
        for event in self._events:
            els.append({"tag": "markdown", "content": event.content})
        els.append({"tag": "hr"})
        els.append({
            "tag": "markdown",
            "content": f"🔧 {self._tool_count} 次工具调用 · ⏱ {_fmt_elapsed(self.elapsed_s)}",
        })
        return els


def _resolve_header(status: str, project_tag: str) -> tuple[str, str]:
    """Return ``(title, template)`` for the given *status*."""
    if status == "running":
        return f"⏳ 执行中 {project_tag}", "blue"
    if status == "done":
        return f"✅ 完成 {project_tag}", "green"
    if status == "error":
        return f"❌ 出错了 {project_tag}", "red"
    return f"🚫 已取消 {project_tag}", "grey"
