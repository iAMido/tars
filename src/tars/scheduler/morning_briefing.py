"""The 05:00 daily morning briefing.

Pulls overnight unread email (last 12h), today's calendar (next 5 events),
open follow-ups (next 7 days), composes via Agent at cron_default tier in
TARS voice, persists to the briefings table, sends to Telegram.

Designed for robust partial-degradation: if Gmail fails, the briefing still
goes out with calendar + follow-ups. If everything fails, we log loudly but
don't crash the scheduler.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot

from tars.integrations.gcal import fetch_upcoming
from tars.integrations.gmail import fetch_unread_since
from tars.memory.follow_ups import list_open

log = logging.getLogger("tars.scheduler.morning_briefing")

OVERNIGHT_HOURS = 12
CAL_LOOKAHEAD = 5
FOLLOWUP_HORIZON_DAYS = 7


async def _safe_gmail(now: int) -> tuple[list[dict], str | None]:
    since = now - OVERNIGHT_HOURS * 3600
    try:
        return await fetch_unread_since(since, max_results=15), None
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: gmail fetch degraded (%s)", e)
        return [], f"gmail unavailable: {type(e).__name__}"


async def _safe_calendar() -> tuple[list[dict], str | None]:
    try:
        events = await fetch_upcoming(CAL_LOOKAHEAD)
        # Strip the raw payload for the LLM prompt; only the summary lives there.
        return [
            {
                "title": e["title"],
                "start_iso": datetime.fromtimestamp(e["start_ts"], tz=timezone.utc).isoformat(),
                "attendees": e["attendees"],
                "location": e["location"],
            }
            for e in events
        ], None
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: calendar fetch degraded (%s)", e)
        return [], f"calendar unavailable: {type(e).__name__}"


async def _safe_followups(db) -> list[dict]:
    try:
        fus = await list_open(db, limit=10)
        horizon = int(time.time()) + FOLLOWUP_HORIZON_DAYS * 86400
        return [
            {
                "id": f["followup_id"],
                "note_id": f["note_id"],
                "promised_to": f["promised_to"],
                "due": (
                    datetime.fromtimestamp(f["due_at"], tz=timezone.utc).isoformat()
                    if f["due_at"]
                    else None
                ),
                "body": (f["body"] or "")[:200],
            }
            for f in fus
            if f["due_at"] is None or f["due_at"] <= horizon
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: follow-ups query failed (%s)", e)
        return []


PROMPT_TEMPLATE = (
    "Compose today's morning briefing. Tight, TARS voice. "
    "Sections only if non-empty: Email, Calendar, Open follow-ups, Heads-up.\n"
    "No greeting, no sign-off. Two to four sentences per section maximum.\n"
    "Cite follow-ups as [followup:N] and any source-note as [note:N] when relevant.\n"
    "\n"
    "Source data (JSON):\n{payload}\n"
    "\n"
    "Briefing:"
)


async def morning_briefing(agent, db, cfg) -> dict:
    """The job APScheduler calls. Returns a small summary dict for logging."""
    t0 = time.time()
    now = int(t0)
    today = datetime.fromtimestamp(t0).date().isoformat()
    log.info("morning_briefing: running for date=%s", today)

    emails, email_err = await _safe_gmail(now)
    cal, cal_err = await _safe_calendar()
    fus = await _safe_followups(db)

    payload = {
        "date": today,
        "emails": emails,
        "calendar": cal,
        "open_followups": fus,
        "warnings": [w for w in (email_err, cal_err) if w],
    }

    out = await agent.chat(
        thread_key="job:morning_briefing",
        user_text=PROMPT_TEMPLATE.format(payload=json.dumps(payload, default=str, indent=2)),
        tier="cron_default",
    )
    text = out["text"].strip() or "(briefing empty)"

    # Persist to briefings.
    await db.execute(
        "INSERT INTO briefings(date, summary, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET summary=excluded.summary, payload=excluded.payload",
        (today, text, json.dumps(payload, default=str)),
    )

    # Send to each allowed chat. Open a fresh Bot session so this is independent
    # of the long-polling bot lifecycle.
    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("morning_briefing: send_message to %s failed (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "morning_briefing: done date=%s emails=%d cal=%d followups=%d sent=%d elapsed=%.2fs cost=$%.6f",
        today, len(emails), len(cal), len(fus), sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "date": today,
        "emails": len(emails),
        "calendar": len(cal),
        "followups": len(fus),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
