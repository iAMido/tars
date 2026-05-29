"""Calendar integration — read-only, async wrapper around google-api-python-client.

Caches fetched events into the cal_events table so the briefing job has
sub-100ms access to today's schedule even when Google is slow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import build

from tars.db import Database
from tars.integrations.google_auth import load_credentials

log = logging.getLogger("tars.integrations.gcal")


def _build_service(creds):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_event_time(t: dict) -> int:
    """Calendar gives dateTime (timestamp) for timed events and date (YYYY-MM-DD)
    for all-day events. Normalize to unix ts (start-of-day for all-day)."""
    if "dateTime" in t:
        return int(datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00")).timestamp())
    if "date" in t:
        d = datetime.fromisoformat(t["date"]).replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    return 0


def _fetch_upcoming_sync(n: int) -> list[dict]:
    creds = load_credentials()
    svc = _build_service(creds)
    now = datetime.now(timezone.utc).isoformat()
    res = svc.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=n,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = res.get("items") or []
    out: list[dict] = []
    for e in events:
        out.append(
            {
                "ical_uid": e.get("iCalUID") or e.get("id"),
                "title": e.get("summary") or "(no title)",
                "start_ts": _parse_event_time(e.get("start") or {}),
                "end_ts": _parse_event_time(e.get("end") or {}),
                "attendees": [
                    a.get("email") for a in (e.get("attendees") or []) if a.get("email")
                ],
                "location": e.get("location") or "",
                "raw": e,
            }
        )
    return out


async def fetch_upcoming(n: int = 5) -> list[dict]:
    """Next n events from the primary calendar, soonest first."""
    try:
        events = await asyncio.to_thread(_fetch_upcoming_sync, n)
    except Exception as e:  # noqa: BLE001
        log.warning("calendar fetch failed: %s", e)
        raise
    log.info("calendar: %d upcoming events", len(events))
    return events


async def cache_upcoming(db: Database, n: int = 50) -> int:
    """Pull next n events and upsert into cal_events. Returns count written.

    Used by the calendar_pull scheduled job and at briefing time so the
    briefing composer has fast access without a live API round trip."""
    events = await fetch_upcoming(n)
    now = int(time.time())
    for e in events:
        await db.execute(
            "INSERT INTO cal_events("
            " ical_uid, start_ts, end_ts, title, attendees, location, payload, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ical_uid) DO UPDATE SET "
            " start_ts=excluded.start_ts, end_ts=excluded.end_ts, title=excluded.title,"
            " attendees=excluded.attendees, location=excluded.location, "
            " payload=excluded.payload, fetched_at=excluded.fetched_at",
            (
                e["ical_uid"],
                e["start_ts"],
                e["end_ts"],
                e["title"],
                json.dumps(e["attendees"]),
                e["location"],
                json.dumps(e["raw"], default=str),
                now,
            ),
        )
    return len(events)


# --- ad-hoc CLI smoke test ---


async def _smoke() -> None:
    events = await fetch_upcoming(5)
    for e in events:
        start = datetime.fromtimestamp(e["start_ts"]).isoformat() if e["start_ts"] else "?"
        print(f"  - {start}  {e['title']}  ({len(e['attendees'])} attendees)")


if __name__ == "__main__":
    asyncio.run(_smoke())
