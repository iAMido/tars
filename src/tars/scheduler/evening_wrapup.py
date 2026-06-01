"""The 18:00 daily evening wrap-up.

The closing-time handoff. Focus on retrospection + tomorrow's setup:

  *Today*      — 1-2 lines: how did the day go
  *Closed*    — what got resolved (follow-ups closed, fixes captured)
  *Still open* — outstanding items, especially overdue
  *Tomorrow*   — calendar preview + follow-ups due tomorrow

Designed to NOT overlap with morning_briefing in topic — this is the
"what happened" side, morning is "what's coming." Together they bracket
the day cleanly.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.integrations.gcal import fetch_upcoming
from tars.memory.follow_ups import list_open

log = logging.getLogger("tars.scheduler.evening_wrapup")


PROMPT_TEMPLATE = (
    "Compose an evening wrap-up in TARS voice. Closing-time tone — what "
    "happened, what's still open, what's tomorrow. No greeting.\n"
    "\n"
    "STRICT format:\n"
    "- Render sections ONLY when their data is present. Omit empty.\n"
    "- Section headers in this order: *Today*, *Closed*, *Still open*, "
    "*Tomorrow*. Single asterisks.\n"
    "- NEVER write placeholders like 'None.' — just omit.\n"
    "- Wrap follow-up tokens: `` `[followup:N]` ``. Note ids: `` `[note:N]` ``.\n"
    "\n"
    "*Today* — REQUIRED if data present. 1-2 lines synthesizing the day. "
    "Was it productive (closures, action notes)? Stagnant (open items "
    "untouched)? Mention concrete resolutions if any.\n"
    "\n"
    "*Closed* — bullets per follow-up closed today + per resolution note "
    "saved today. Format: `- <body or summary> `[note:N]` ``. Skip if zero.\n"
    "\n"
    "*Still open* — follow-ups still 'open' status, soonest-due first. "
    "Highlight any OVERDUE. Format: `` - <body> (due_human / overdue Nd) "
    "`[followup:N]` ``. Skip if zero.\n"
    "\n"
    "*Tomorrow* — calendar events + follow-ups due tomorrow. RTL-safe: wrap "
    "times in backticks so bidi doesn't flip them on Hebrew-title lines. "
    "Calendar: `` - `HH:MM` Title ``. Follow-ups: `` - due `HH:MM`: <body> "
    "`[followup:N]` ``. Skip if zero of both.\n"
    "\n"
    "Payload:\n{payload}\n"
    "\n"
    "Wrap-up:"
)


async def evening_wrapup_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await evening_wrapup(rt.agent, rt.db, rt.cfg)


def _human_due_or_overdue(due_ts: int | None, now_dt: datetime) -> str:
    if not due_ts:
        return "no due"
    due = datetime.fromtimestamp(due_ts, tz=now_dt.tzinfo)
    delta_days = (due.date() - now_dt.date()).days
    t = due.strftime("%H:%M")
    if delta_days < 0:
        return f"overdue {abs(delta_days)}d"
    if delta_days == 0:
        return f"today {t}"
    if delta_days == 1:
        return f"tomorrow {t}"
    if 1 < delta_days <= 7:
        return f"{due.strftime('%A')} {t}"
    return f"{due.date().isoformat()} {t}"


async def _closed_today(db, day_start_ts: int) -> list[dict]:
    """Follow-ups closed today + their resolving notes. Joins via
    notes.closes_note_id."""
    try:
        rows = await db.fetch_all(
            "SELECT fu.id AS fu_id, fu.note_id AS source_note_id, "
            "       n.body AS source_body, "
            "       r.id AS resolving_note_id, r.body AS resolving_body, "
            "       datetime(r.closed_at,'unixepoch','localtime') AS closed_local "
            "FROM follow_ups fu "
            "  JOIN notes n ON n.id = fu.note_id "
            "  LEFT JOIN notes r ON r.id = n.closes_note_id "
            "WHERE fu.status = 'closed' AND n.closed_at >= ? "
            "ORDER BY n.closed_at DESC LIMIT 20",
            (day_start_ts,),
        )
        return [
            {
                "followup_id": int(r["fu_id"]),
                "source_note_id": int(r["source_note_id"]),
                "source_body": (r["source_body"] or "")[:160],
                "resolving_note_id": (
                    int(r["resolving_note_id"]) if r["resolving_note_id"] else None
                ),
                "resolving_body": (r["resolving_body"] or "")[:160],
                "closed_local": r["closed_local"],
            }
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("evening: closed_today query failed (%s)", e)
        return []


async def _resolution_notes_today(db, day_start_ts: int) -> list[dict]:
    """Notes captured today that look like resolutions/fixes — heuristic
    on body prefix. Light signal, useful for the *Today* synthesis."""
    try:
        rows = await db.fetch_all(
            "SELECT id, body FROM notes "
            "WHERE created_at >= ? AND status != 'deleted' "
            "  AND (LOWER(body) LIKE 'fixed %' OR LOWER(body) LIKE 'done: %' "
            "       OR LOWER(body) LIKE 'completed %' OR LOWER(body) LIKE 'resolved %' "
            "       OR LOWER(body) LIKE 'closed %' OR LOWER(body) LIKE 'shipped %') "
            "ORDER BY id DESC LIMIT 10",
            (day_start_ts,),
        )
        return [
            {"id": int(r["id"]), "body": (r["body"] or "")[:200]}
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("evening: resolutions query failed (%s)", e)
        return []


async def evening_wrapup(agent, db, cfg) -> dict:
    t0 = time.time()
    tz = ZoneInfo(cfg.timezone)
    now_dt = datetime.fromtimestamp(t0, tz=tz)
    today = now_dt.date().isoformat()

    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_ts = int(day_start.timestamp())

    closed = await _closed_today(db, day_start_ts)
    resolutions = await _resolution_notes_today(db, day_start_ts)

    # Open follow-ups, sorted by soonest due.
    all_open = await list_open(db, limit=30)
    still_open = [
        {
            "id": f["followup_id"],
            "body": (f.get("body") or "")[:160],
            "due_human": _human_due_or_overdue(f.get("due_at"), now_dt),
            "is_overdue": bool(f.get("due_at") and f["due_at"] < int(t0)),
        }
        for f in all_open
    ]

    # Tomorrow's calendar + follow-ups.
    tomorrow_start = (day_start + timedelta(days=1)).timestamp()
    tomorrow_end = tomorrow_start + 86400
    try:
        events = await fetch_upcoming(20)
    except Exception as e:  # noqa: BLE001
        log.warning("evening: cal fetch degraded (%s)", e)
        events = []
    tomorrow_cal = [
        {
            "title": e["title"],
            "start_iso": datetime.fromtimestamp(
                e["start_ts"], tz=timezone.utc,
            ).isoformat(),
            "location": e.get("location") or "",
        }
        for e in events
        if e.get("start_ts")
        and tomorrow_start <= e["start_ts"] < tomorrow_end
    ]
    tomorrow_fus = [
        {
            "id": f["followup_id"],
            "body": (f.get("body") or "")[:160],
            "due_human": _human_due_or_overdue(f.get("due_at"), now_dt),
        }
        for f in all_open
        if f.get("due_at") and tomorrow_start <= f["due_at"] < tomorrow_end
    ]

    payload: dict[str, Any] = {"date": today}
    if closed:
        payload["closed_today"] = closed
    if resolutions:
        payload["resolution_notes_today"] = resolutions
    if still_open:
        payload["still_open"] = still_open
    if tomorrow_cal:
        payload["tomorrow_calendar"] = tomorrow_cal
    if tomorrow_fus:
        payload["tomorrow_followups"] = tomorrow_fus

    # Skip the LLM call entirely if there's literally nothing to say.
    if not (closed or resolutions or still_open or tomorrow_cal or tomorrow_fus):
        log.info("evening_wrapup: nothing to say, skipping")
        return {"sent": 0, "elapsed_s": time.time() - t0, "skipped": True}

    out = await agent.chat(
        thread_key="job:evening_wrapup",
        user_text=PROMPT_TEMPLATE.format(
            payload=json.dumps(payload, default=str, indent=2),
        ),
        tier="cron_default",
    )
    text = (out.get("text") or "").strip() or "(evening wrap-up empty)"
    text = re.sub(r"\*\*(\S[^*\n]*?\S)\*\*", r"*\1*", text)

    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown", disable_web_page_preview=True)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("evening: send_message to %s failed (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "evening_wrapup: closed=%d resolutions=%d still_open=%d "
        "tomorrow_cal=%d tomorrow_fu=%d sent=%d elapsed=%.2fs cost=$%.6f",
        len(closed), len(resolutions), len(still_open),
        len(tomorrow_cal), len(tomorrow_fus), sent, elapsed,
        out.get("cost_usd", 0.0),
    )
    return {
        "closed": len(closed),
        "resolutions": len(resolutions),
        "still_open": len(still_open),
        "tomorrow_cal": len(tomorrow_cal),
        "tomorrow_fu": len(tomorrow_fus),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
