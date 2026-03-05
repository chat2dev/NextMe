"""Unit tests for scripts/feishu_book_meeting.py.

Tests mock all HTTP calls — no real Feishu API access.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

# Add scripts/ to path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import feishu_book_meeting as bm


class TestGetToken:
    def test_returns_token_on_success(self):
        response_data = {"code": 0, "tenant_access_token": "t-abc123", "expire": 7200}
        with patch.object(bm, "_http_post", return_value=response_data):
            token = bm.get_tenant_token("app_id", "app_secret")
        assert token == "t-abc123"

    def test_raises_on_error_code(self):
        with patch.object(bm, "_http_post", return_value={"code": 99991663, "msg": "bad"}):
            with pytest.raises(RuntimeError, match="tenant_access_token"):
                bm.get_tenant_token("app_id", "app_secret")


class TestGetOrCreateCalendar:
    def test_returns_existing_calendar(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        cache_file.write_text("cal_cached_id\n")
        cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_cached_id"

    def test_creates_calendar_when_cache_missing(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        list_resp = {"code": 0, "data": {"calendar_list": []}}
        create_resp = {"code": 0, "data": {"calendar": {"calendar_id": "cal_new_id"}}}
        with patch.object(bm, "_http_get", return_value=list_resp), \
             patch.object(bm, "_http_post", return_value=create_resp):
            cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_new_id"
        assert cache_file.read_text().strip() == "cal_new_id"

    def test_finds_existing_calendar_by_name(self, tmp_path):
        cache_file = tmp_path / "cal_id"
        list_resp = {"code": 0, "data": {"calendar_list": [
            {"calendar_id": "cal_existing", "summary": "NextMe 会议"}
        ]}}
        with patch.object(bm, "_http_get", return_value=list_resp):
            cal_id = bm.get_or_create_calendar("token", cache_path=str(cache_file))
        assert cal_id == "cal_existing"


class TestCreateEvent:
    def test_returns_event_id_and_vchat(self):
        resp = {"code": 0, "data": {"event": {
            "event_id": "ev_001",
            "vchat": {"meeting_url": "https://meeting.feishu.cn/abc"},
        }}}
        with patch.object(bm, "_http_post", return_value=resp) as mock_post:
            event_id, vchat_url = bm.create_event(
                "token", "cal_id",
                title="Team Meeting",
                start="2026-03-06T15:00:00+08:00",
                end="2026-03-06T16:00:00+08:00",
            )
        assert event_id == "ev_001"
        assert vchat_url == "https://meeting.feishu.cn/abc"
        body = mock_post.call_args[0][2]
        assert body.get("attendee_ability") == "can_see_others"

    def test_raises_on_api_error(self):
        with patch.object(bm, "_http_post", return_value={"code": 1, "msg": "error"}):
            with pytest.raises(RuntimeError, match="create event"):
                bm.create_event("token", "cal_id", "title",
                                "2026-03-06T15:00:00+08:00", "2026-03-06T16:00:00+08:00")


class TestAddAttendees:
    def test_adds_users_successfully(self):
        resp = {"code": 0, "data": {"attendees": []}}
        with patch.object(bm, "_http_post", return_value=resp):
            bm.add_attendees("token", "cal_id", "ev_id",
                             user_ids=["ou_aaa", "ou_bbb"], room_id=None)

    def test_adds_room_when_room_id_provided(self):
        calls = []
        def fake_post(url, token, body):
            calls.append(body)
            return {"code": 0, "data": {"attendees": []}}
        with patch.object(bm, "_http_post", side_effect=fake_post):
            bm.add_attendees("token", "cal_id", "ev_id",
                             user_ids=[], room_id="omm_room_abc")
        assert any(
            any(a.get("type") == "meeting_room" for a in c.get("attendees", []))
            for c in calls
        )

    def test_raises_on_api_error(self):
        with patch.object(bm, "_http_post", return_value={"code": 1, "msg": "fail"}):
            with pytest.raises(RuntimeError, match="add attendees"):
                bm.add_attendees("token", "cal_id", "ev_id", ["ou_x"], None)

    def test_no_op_when_no_attendees_and_no_room(self):
        with patch.object(bm, "_http_post") as mock_post:
            bm.add_attendees("token", "cal_id", "ev_id", [], None)
        mock_post.assert_not_called()


class TestSearchRoom:
    def test_returns_first_match(self):
        resp = {"code": 0, "data": {"resources": [
            {"room_id": "omm_abc", "name": "极光会议室"},
        ]}}
        with patch.object(bm, "_http_get", return_value=resp):
            room_id, room_name = bm.search_room("token", "极光")
        assert room_id == "omm_abc"
        assert room_name == "极光会议室"

    def test_returns_none_when_no_match(self):
        resp = {"code": 0, "data": {"resources": []}}
        with patch.object(bm, "_http_get", return_value=resp):
            room_id, room_name = bm.search_room("token", "不存在的会议室")
        assert room_id is None
        assert room_name is None
