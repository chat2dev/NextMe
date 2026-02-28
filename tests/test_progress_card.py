"""Tests for nextme.feishu.progress_card.RunProgressCard."""

import json
import time
import pytest

from nextme.feishu.progress_card import RunProgressCard, _fmt_elapsed, _resolve_header


# ---------------------------------------------------------------------------
# _fmt_elapsed helper
# ---------------------------------------------------------------------------


class TestFmtElapsed:
    def test_seconds_only(self):
        assert _fmt_elapsed(0) == "0s"
        assert _fmt_elapsed(59) == "59s"

    def test_round_minutes(self):
        assert _fmt_elapsed(60) == "1m"
        assert _fmt_elapsed(120) == "2m"

    def test_minutes_and_seconds(self):
        assert _fmt_elapsed(90) == "1m 30s"
        assert _fmt_elapsed(125) == "2m 5s"


# ---------------------------------------------------------------------------
# _resolve_header helper
# ---------------------------------------------------------------------------


class TestResolveHeader:
    def test_running(self):
        title, template = _resolve_header("running", "【proj】")
        assert "⏳" in title
        assert "【proj】" in title
        assert template == "blue"

    def test_done(self):
        title, template = _resolve_header("done", "【proj】")
        assert "✅" in title
        assert template == "green"

    def test_error(self):
        title, template = _resolve_header("error", "【proj】")
        assert "❌" in title
        assert template == "red"

    def test_cancelled(self):
        title, template = _resolve_header("cancelled", "【proj】")
        assert "🚫" in title
        assert template == "grey"

    def test_unknown_falls_through_to_cancelled(self):
        # Any unrecognised status uses the cancelled fallback.
        _, template = _resolve_header("something_else", "X")
        assert template == "grey"


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_tool_count_zero(self):
        card = RunProgressCard()
        assert card.tool_count == 0

    def test_elapsed_s_non_negative(self):
        card = RunProgressCard()
        assert card.elapsed_s >= 0

    def test_build_card_returns_dict(self):
        card = RunProgressCard()
        result = card.build_card("running", "【test】")
        assert isinstance(result, dict)

    def test_build_card_is_json_serialisable(self):
        card = RunProgressCard()
        result = json.dumps(card.build_card("running", "【test】"))
        assert isinstance(result, str)

    def test_schema_2_0(self):
        card = RunProgressCard()
        d = card.build_card("running", "【p】")
        assert d["schema"] == "2.0"

    def test_wide_screen_mode(self):
        card = RunProgressCard()
        d = card.build_card("running", "【p】")
        assert d["config"]["wide_screen_mode"] is True

    def test_enable_forward(self):
        card = RunProgressCard()
        d = card.build_card("running", "【p】")
        assert d["config"]["enable_forward"] is True


# ---------------------------------------------------------------------------
# add_tool
# ---------------------------------------------------------------------------


class TestAddTool:
    def test_tool_appears_in_body(self):
        card = RunProgressCard()
        card.add_tool("Bash(ls)", 3)
        d = card.build_card("running", "【p】")
        body_text = json.dumps(d["body"])
        assert "Bash(ls)" in body_text

    def test_elapsed_in_entry(self):
        card = RunProgressCard()
        card.add_tool("Read", 7)
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "7s" in body_text

    def test_tool_count_increments(self):
        card = RunProgressCard()
        card.add_tool("Bash", 1)
        card.add_tool("Read", 2)
        assert card.tool_count == 2

    def test_same_base_updates_in_place(self):
        """DirectClaudeRuntime pattern: bare name first, then formatted name."""
        card = RunProgressCard()
        card.add_tool("Bash", 1)
        card.add_tool("Bash(ls -la)", 2)
        # Only one tool event in the window.
        assert card.tool_count == 1
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "Bash(ls -la)" in body_text
        assert "Bash`" not in body_text  # bare name replaced

    def test_different_base_adds_new_entry(self):
        card = RunProgressCard()
        card.add_tool("Bash(cmd)", 1)
        card.add_tool("Read(path)", 2)
        assert card.tool_count == 2

    def test_emoji_in_tool_entry(self):
        card = RunProgressCard()
        card.add_tool("Bash", 0)
        body_text = json.dumps(card.build_card("running", "【p】")["body"], ensure_ascii=False)
        assert "🔧" in body_text


# ---------------------------------------------------------------------------
# add_text_chunk
# ---------------------------------------------------------------------------


class TestAddTextChunk:
    def test_text_appears_in_body(self):
        card = RunProgressCard()
        card.add_text_chunk("Hello world")
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "Hello world" in body_text

    def test_empty_delta_ignored(self):
        card = RunProgressCard()
        card.add_text_chunk("")
        d = card.build_card("running", "【p】")
        # No text event means no "💬" in body
        body_text = json.dumps(d["body"])
        assert "💬" not in body_text

    def test_accumulates_across_chunks(self):
        card = RunProgressCard()
        card.add_text_chunk("Hello ")
        card.add_text_chunk("world")
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "Hello" in body_text
        assert "world" in body_text

    def test_single_text_slot_not_duplicated(self):
        card = RunProgressCard()
        card.add_text_chunk("first")
        card.add_text_chunk("second")
        elements = card.build_card("running", "【p】")["body"]["elements"]
        text_elements = [e for e in elements if "💬" in e.get("content", "")]
        assert len(text_elements) == 1

    def test_tail_truncation_for_long_text(self):
        card = RunProgressCard()
        long_text = "A" * 200
        card.add_text_chunk(long_text)
        body_text = json.dumps(card.build_card("running", "【p】")["body"], ensure_ascii=False)
        # Tail of 120 chars plus ellipsis prefix
        assert "…" in body_text

    def test_short_text_not_truncated(self):
        card = RunProgressCard()
        card.add_text_chunk("short")
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "short" in body_text
        assert "…" not in body_text

    def test_emoji_in_text_entry(self):
        card = RunProgressCard()
        card.add_text_chunk("hi")
        body_text = json.dumps(card.build_card("running", "【p】")["body"], ensure_ascii=False)
        assert "💬" in body_text


# ---------------------------------------------------------------------------
# Sliding-window overflow
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_omitted_count_tracks_overflow(self):
        card = RunProgressCard(max_events=2)
        card.add_tool("T1", 1)
        card.add_tool("T2", 2)
        card.add_tool("T3", 3)  # overflows: T1 pushed out
        # T1 was removed → omitted count = 1
        body_text = json.dumps(card.build_card("running", "【p】")["body"], ensure_ascii=False)
        assert "已省略" in body_text
        assert "1" in body_text

    def test_text_slot_evicted_increments_no_omit_count(self):
        """Text events that are evicted don't increment the tool omitted count."""
        card = RunProgressCard(max_events=2)
        card.add_text_chunk("text")    # occupies 1 slot
        card.add_tool("T1", 1)         # occupies 2nd slot
        card.add_tool("T2", 2)         # evicts text (kind="text") → omitted stays 0 for tools
        # tool_count should be 2
        assert card.tool_count == 2

    def test_window_never_exceeds_max(self):
        card = RunProgressCard(max_events=3)
        for i in range(10):
            card.add_tool(f"T{i}", i)
        elements = card.build_card("running", "【p】")["body"]["elements"]
        # Elements include: header, (maybe omission line), hr, ≤3 events, hr, footer
        # Tool entries use backtick format: 🔧 `ToolName` · Xs (footer does not)
        tool_entries = [e for e in elements if "🔧 `" in e.get("content", "")]
        assert len(tool_entries) <= 3

    def test_max_events_1_still_works(self):
        card = RunProgressCard(max_events=1)
        card.add_tool("T1", 1)
        card.add_tool("T2", 2)
        # Only T2 visible; T1 omitted
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "T2" in body_text


# ---------------------------------------------------------------------------
# Footer / summary
# ---------------------------------------------------------------------------


class TestFooter:
    def test_footer_contains_tool_count(self):
        card = RunProgressCard()
        card.add_tool("Bash", 1)
        card.add_tool("Read", 2)
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "2" in body_text

    def test_footer_contains_elapsed(self):
        card = RunProgressCard()
        body_text = json.dumps(card.build_card("running", "【p】")["body"])
        assert "s" in body_text  # elapsed always contains 's' suffix

    def test_footer_has_timer_emoji(self):
        card = RunProgressCard()
        body_text = json.dumps(card.build_card("running", "【p】")["body"], ensure_ascii=False)
        assert "⏱" in body_text


# ---------------------------------------------------------------------------
# Header transitions
# ---------------------------------------------------------------------------


class TestHeaderTransitions:
    def test_running_header_is_blue(self):
        card = RunProgressCard()
        d = card.build_card("running", "【p】")
        assert d["header"]["template"] == "blue"

    def test_done_header_is_green(self):
        card = RunProgressCard()
        d = card.build_card("done", "【p】")
        assert d["header"]["template"] == "green"

    def test_error_header_is_red(self):
        card = RunProgressCard()
        d = card.build_card("error", "【p】")
        assert d["header"]["template"] == "red"

    def test_cancelled_header_is_grey(self):
        card = RunProgressCard()
        d = card.build_card("cancelled", "【p】")
        assert d["header"]["template"] == "grey"

    def test_project_tag_in_header_title(self):
        card = RunProgressCard()
        d = card.build_card("running", "【myproj】")
        assert "【myproj】" in d["header"]["title"]["content"]
