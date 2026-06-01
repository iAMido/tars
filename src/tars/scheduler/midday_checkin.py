"""The 13:00 daily midday check-in.

Lightweight counterpart to morning_briefing. Designed to keep TARS present
mid-day without spamming. Sections:

  *Status*       — one line: are you on track?
  *New since 5am* — emails, notes, calendar adds that postdate the morning brief
  *This afternoon* — calendar events 13:00 → end-of-day
  *Open* — follow-ups due today or tomorrow

No yesterday_summary, no semantic note retrieval — cheaper than morning brief.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.integrations.gcal import fetch_upcoming
from tars.integrations.gmail import fetch_unread_since
from tars.memory.follow_ups import list_open

log = logging.getLogger("tars.scheduler.midday_checkin")


PROMPT_TEMPLATE = (
    "Compose a midday check-in in TARS voice. Short, dense, no greeting.\n"
    "\n"
    "STRICT format:\n"
    "- Render sections ONLY when their data is present below. Omit empty.\n"
    "- Section headers in this order: *Status*, *New since 5am*, "
    "*This afternoon*, *Open*. Single asterisks (Telegram Markdown).\n"
    "- NEVER write placeholders like 'None.' — just omit the section.\n"
    "- Wrap follow-up tokens in backticks: `` `[followup:N]` ``.\n"
    "\n"
    "*Status* — REQUIRED. One sentence answering: how is the day tracking? "
    "If anything is overdue or burning, lead with it. If quiet, say so plainly.\n"
    "\n"
    "*New since 5am* — bullets for emails/notes arrived after the morning "
    "briefing. Skip newsletters/receipts. Skip if zero.\n"
    "\n"
    "*This afternoon* — calendar bullets for events starting >= now today. "
    "RTL-safe: wrap the time in backticks so bidi doesn't flip date "
    "components on Hebrew-title lines: `` - `HH:MM` Title ``. No em-dash. "
    "Skip if zero.\n"
    "\n"
    "*Open* — follow-ups due today or tomorrow. "
    "`` - <body> (due_human) `[followup:N]` ``. Skip if zero.\n"
    "\n"
    "Payload:\n{payload}\n"
    "\n"
    "Check-in:"
)


async def midday_checkin_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await midday_checkin(rt.agent, rt.db, rt.cfg)


def _human_due(due_ts: int | None, now_dt: datetime) -> str | None:
    if not due_ts:
        return None
    due = datetime.fromtimestamp(due_ts, tz=now_dt.tzinfo)
    days = (due.date() - now_dt.date()).days
    t = due.strftime("%H:%M")
    if days == 0:
        return f"today {t}"
    if days == 1:
        return f"tomorrow {t}"
    if 1 < days <= 7:
        return f"{due.strftime('%A')} {t}"
    return f"{due.date().isoformat()} {t}"


async def _new_since_morning(db, since_ts: int) -> dict[str, Any]:
    """Emails arrived since 05:00 + notes captured since 05:00. Cheaper than
    the morning intel — no per-attendee scan, no pending replies."""
    out: dict[str, Any] = {"emails": [], "notes": []}
    try:
        msgs = await fetch_unread_since(since_ts, max_results=10, include_body=False)
        out["emails"] = [
            {
                "from": m["from"],
                "subject": m["subject"],
                "snippet": m["snippet"][:200],
            }
            for m in msgs
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("midday: gmail fetch degraded (%s)", e)
    try:
        rows = await db.fetch_all(
            "SELECT id, body, tags FROM notes "
            "WHERE created_at >= ? AND status != 'deleted' "
            "  AND (tags IS NULL OR tags NOT LIKE '%source/reminder-ping%') "
            "ORDER BY id DESC LIMIT 15",
            (since_ts,),
        )
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except json.JSONDecodeError:
                tags = []
            out["notes"].append({
                "id": int(r["id"]),
                "preview": (r["body"] or "").strip().split("\n")[0][:160],
                "tags": tags,
            })
    except Exception as e:  # noqa: BLE001
        log.warning("midday: notes query failed (%s)", e)
    return out


async def midday_checkin(agent, db, cfg) -> dict:
    t0 = time.time()
    tz = ZoneInfo(cfg.timezone)
    now_dt = datetime.fromtimestamp(t0, tz=tz)
    today = now_dt.date().isoformat()

    morning_anchor = now_dt.replace(hour=5, minute=0, second=0, microsecond=0)
    since_ts = int(morning_anchor.timestamp())

    # Calendar: filter to today's events still upcoming.
    try:
        events = await fetch_upcoming(15)
    except Exception as e:  # noqa: BLE001
        log.warning("midday: cal fetch degraded (%s)", e)
        events = []
    end_of_day = now_dt.replace(hour=23, minute=59, second=59).timestamp()
    afternoon = [
        {
            "title": e["title"],
            "start_iso": datetime.fromtimestamp(e["start_ts"], tz=timezone.utc).isoformat(),
            "location": e.get("location") or "",
        }
        for e in events
        if e.get("start_ts") and t0 <= e["start_ts"] <= end_of_day
    ]

    new = await _new_since_morning(db, since_ts)

    # Open follow-ups due today/tomorrow.
    fus_all = await list_open(db, limit=20)
    horizon = int(t0) + 2 * 86400
    opens = []
    for f in fus_all:
        if f.get("due_at") and f["due_at"] <= horizon:
            opens.append({
                "id": f["followup_id"],
                "body": (f["body"] or "")[:160],
                "due_human": _human_due(f["due_at"], now_dt),
            })

    payload: dict[str, Any] = {"date": today, "now": now_dt.strftime("%H:%M")}
    if new["emails"]:
        payload["new_emails"] = new["emails"]
    if new["notes"]:
        payload["new_notes"] = new["notes"]
    if afternoon:
        payload["this_afternoon"] = afternoon
    if opens:
        payload["open_followups_soon"] = opens

    # Always include the "status" guidance even when empty so the LLM
    # always renders that section.
    out = await agent.chat(
        thread_key="job:midday_checkin",
        user_text=PROMPT_TEMPLATE.format(
            payload=json.dumps(payload, default=str, indent=2),
        ),
        tier="cron_default",
    )
    text = (out.get("text") or "").strip() or "(midday check-in empty)"
    text = re.sub(r"\*\*(\S[^*\n]*?\S)\*\*", r"*\1*", text)

    # Send to Telegram. Reuses the same plain-text send (no inline kb).
    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("midday: send_message to %s failed (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "midday_checkin: emails=%d notes=%d afternoon=%d open=%d sent=%d "
        "elapsed=%.2fs cost=$%.6f",
        len(new["emails"]), len(new["notes"]), len(afternoon),
        len(opens), sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "emails": len(new["emails"]),
        "notes": len(new["notes"]),
        "afternoon": len(afternoon),
        "open_followups_soon": len(opens),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
