#!/usr/bin/env python3
"""Feishu Calendar booking helper.

Usage:
    python3 scripts/feishu_book_meeting.py \
        --title "Team Sync" \
        --start "2026-03-06T15:00:00+08:00" \
        --end   "2026-03-06T16:00:00+08:00" \
        [--attendees "ou_aaa,ou_bbb"] \
        [--room "极光"] \
        --config ~/.nextme/settings.json

Prints JSON result to stdout. Exits 1 on error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path

BASE_URL = "https://open.feishu.cn/open-apis"
CALENDAR_NAME = "NextMe 会议"
DEFAULT_CACHE = os.path.expanduser("~/.nextme/book_meeting_calendar_id")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (mockable in tests)
# ---------------------------------------------------------------------------

def _http_post(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------

def get_tenant_token(app_id: str, app_secret: str) -> str:
    resp = _http_post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        token="",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"tenant_access_token failed: {resp.get('msg')}")
    return resp["tenant_access_token"]


def get_or_create_calendar(token: str, cache_path: str = DEFAULT_CACHE) -> str:
    # Return cached calendar_id if available
    path = Path(cache_path)
    if path.exists():
        cached = path.read_text().strip()
        if cached:
            return cached

    # Search existing calendars
    resp = _http_get(f"{BASE_URL}/calendar/v4/calendars", token)
    if resp.get("code") != 0:
        raise RuntimeError(f"list calendars failed: {resp.get('msg')}")
    for cal in (resp.get("data") or {}).get("calendar_list") or []:
        if cal.get("summary") == CALENDAR_NAME:
            cal_id = cal["calendar_id"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cal_id + "\n")
            return cal_id

    # Create new calendar
    resp = _http_post(f"{BASE_URL}/calendar/v4/calendars", token, {
        "summary": CALENDAR_NAME,
        "description": "由 NextMe bot 创建的共享会议日历",
        "color": -1,
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"create calendar failed: {resp.get('msg')}")
    cal_id = resp["data"]["calendar"]["calendar_id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cal_id + "\n")
    return cal_id


def create_event(
    token: str,
    cal_id: str,
    title: str,
    start: str,
    end: str,
) -> tuple[str, str]:
    """Create calendar event. Returns (event_id, vchat_url)."""
    body = {
        "summary": title,
        "start_time": {"timestamp": _iso_to_timestamp(start), "timezone": "Asia/Shanghai"},
        "end_time":   {"timestamp": _iso_to_timestamp(end),   "timezone": "Asia/Shanghai"},
        "vchat": {"vc_type": "vc"},
        "free_busy_status": "busy",
        "visibility": "default",
        "attendee_ability": "can_see_others",
        "need_notification": True,
    }
    resp = _http_post(f"{BASE_URL}/calendar/v4/calendars/{cal_id}/events", token, body)
    if resp.get("code") != 0:
        raise RuntimeError(f"create event failed: {resp.get('msg')}")
    event = resp["data"]["event"]
    vchat_url = (event.get("vchat") or {}).get("meeting_url") or ""
    return event["event_id"], vchat_url


def add_attendees(
    token: str,
    cal_id: str,
    event_id: str,
    user_ids: list[str],
    room_id: str | None,
) -> None:
    attendees = [{"type": "user", "user_id": uid} for uid in user_ids if uid]
    if room_id:
        attendees.append({"type": "meeting_room", "room_id": room_id})
    if not attendees:
        return
    resp = _http_post(
        f"{BASE_URL}/calendar/v4/calendars/{cal_id}/events/{event_id}/attendees"
        "?user_id_type=open_id",
        token,
        {"attendees": attendees, "need_notification": True},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"add attendees failed: {resp.get('msg')}")


def search_room(token: str, keyword: str) -> tuple[str | None, str | None]:
    """Search meeting room by keyword. Returns (room_id, room_name) or (None, None)."""
    url = f"{BASE_URL}/vc/v1/resources?keyword={urllib.parse.quote(keyword)}&resource_type=meeting_room"
    resp = _http_get(url, token)
    resources = (resp.get("data") or {}).get("resources") or []
    if not resources:
        return None, None
    first = resources[0]
    return first.get("room_id"), first.get("name")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_to_timestamp(iso: str) -> str:
    """Convert ISO8601 string to Unix timestamp string (Python 3.12+ fromisoformat handles offsets)."""
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return str(int(dt.timestamp()))


def _format_dt(iso: str) -> str:
    from datetime import datetime
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu meeting booking helper")
    parser.add_argument("--title",     required=True)
    parser.add_argument("--start",     required=True, help="ISO8601+08:00")
    parser.add_argument("--end",       required=True, help="ISO8601+08:00")
    parser.add_argument("--attendees", default="", help="comma-separated open_ids")
    parser.add_argument("--room",      default="", help="room name keyword")
    parser.add_argument("--config",    default="~/.nextme/settings.json")
    args = parser.parse_args()

    config_path = os.path.expanduser(args.config)
    with open(config_path) as f:
        cfg = json.load(f)

    try:
        token = get_tenant_token(cfg["app_id"], cfg["app_secret"])
        cal_id = get_or_create_calendar(token)
        event_id, vchat_url = create_event(token, cal_id, args.title, args.start, args.end)

        user_ids = [u.strip() for u in args.attendees.split(",") if u.strip()]
        room_id = room_name = None
        if args.room:
            room_id, room_name = search_room(token, args.room)

        add_attendees(token, cal_id, event_id, user_ids, room_id)

        result = {
            "ok": True,
            "title":      args.title,
            "start":      _format_dt(args.start),
            "end":        _format_dt(args.end),
            "event_id":   event_id,
            "vchat_url":  vchat_url,
            "attendees":  user_ids,
            "room_name":  room_name or "",
            "room_booked": room_id is not None,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
