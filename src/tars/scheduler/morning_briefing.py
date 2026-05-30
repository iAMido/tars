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
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.integrations.gcal import fetch_upcoming
from tars.integrations.gmail import fetch_unread_since
from tars.memory.follow_ups import list_open

log = logging.getLogger("tars.scheduler.morning_briefing")

# Matches a markdown numbered-list line: "1. <text>"  (>=1 chars)
_SUGGESTION_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")


def _extract_suggestions(briefing_text: str) -> list[str]:
    """Walk lines, find the *Suggestions* section, return each numbered item's text.

    Robust to a few format wobbles: header may be `*Suggestions*`, `**Suggestions**`,
    or `# Suggestions`. The section ends at the next header (`*Xxx*` or `**Xxx**`
    on a line by itself) or at end of text.
    """
    lines = briefing_text.splitlines()
    in_section = False
    items: list[str] = []
    header_re = re.compile(r"^\s*(?:\*+|#+)\s*\w[\w\s-]*\*+?\s*$")
    suggestions_re = re.compile(r"^\s*(?:\*+|#+)\s*Suggestions?\s*\*+?\s*$", re.IGNORECASE)
    for raw in lines:
        if not in_section:
            if suggestions_re.match(raw):
                in_section = True
            continue
        # In section: stop on a new header.
        if header_re.match(raw) and not suggestions_re.match(raw):
            break
        m = _SUGGESTION_LINE_RE.match(raw)
        if m:
            items.append(m.group(2))
    log.info("morning_briefing: parsed %d suggestion(s) from output", len(items))
    return items

OVERNIGHT_HOURS = 12
CAL_LOOKAHEAD = 5
FOLLOWUP_HORIZON_DAYS = 7


async def _safe_gmail(now: int) -> tuple[list[dict], str | None]:
    since = now - OVERNIGHT_HOURS * 3600
    try:
        # include_body=True so the LLM has real content to summarize and
        # extract action items from — not just snippets.
        return await fetch_unread_since(since, max_results=12, include_body=True), None
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


def _human_due(due_ts: int | None, now_dt: datetime) -> str | None:
    """Render a due timestamp as 'today 15:00', 'tomorrow 09:00', 'Friday 10:00',
    or 'YYYY-MM-DD HH:MM' for further-out items. Returns None for no-due."""
    if not due_ts:
        return None
    due = datetime.fromtimestamp(due_ts, tz=now_dt.tzinfo)
    today = now_dt.date()
    due_date = due.date()
    days = (due_date - today).days
    time_part = due.strftime("%H:%M")
    if days == 0:
        return f"today {time_part}"
    if days == 1:
        return f"tomorrow {time_part}"
    if 1 < days <= 7:
        return f"{due.strftime('%A')} {time_part}"
    return f"{due_date.isoformat()} {time_part}"


async def _safe_followups(db, now_dt: datetime) -> list[dict]:
    try:
        fus = await list_open(db, limit=10)
        horizon = int(now_dt.timestamp()) + FOLLOWUP_HORIZON_DAYS * 86400
        out = []
        for f in fus:
            due_ts = f.get("due_at")
            if due_ts is not None and due_ts > horizon:
                continue
            out.append(
                {
                    "id": f["followup_id"],
                    "note_id": f["note_id"],
                    "promised_to": f["promised_to"],
                    "due_human": _human_due(due_ts, now_dt),
                    "body": (f["body"] or "")[:200],
                }
            )
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: follow-ups query failed (%s)", e)
        return []


PROMPT_TEMPLATE = (
    "Compose today's morning briefing in TARS voice.\n"
    "\n"
    "STRICT format rules:\n"
    "- Render ONLY sections whose JSON key is present in the payload. If a "
    "section's data is absent below, do not write its header at all.\n"
    "- Possible section headers (only when data exists): *Email*, *Calendar*, "
    "*Open follow-ups*, *Suggestions*, *Warnings*.\n"
    "- No greeting, no sign-off, no commentary outside section bodies.\n"
    "\n"
    "*Email* — for each email, output a single line:\n"
    "  `<From shortened to name or org> — <one-line summary of what the email actually says, not just the subject>`\n"
    "  Read the body. Mention concrete facts (numbers, deadlines, names) the user "
    "should know. Skip footer/unsubscribe/legalese. Skip purely promotional fluff "
    "but DO mention if a newsletter has 1-2 things worth noticing.\n"
    "\n"
    "*Calendar* — one line per event: `HH:MM — Title` if today, else `YYYY-MM-DD HH:MM — Title`.\n"
    "\n"
    "*Open follow-ups* — one line per item: `<body> (due_human) [followup:N]`.\n"
    "\n"
    "*Suggestions* — purely OPTIONAL ideas the user might want to act on. "
    "These are NOT things you (TARS) are doing — just things the user could ask "
    "you to do. Format as a numbered list, plain language. No verb prefixes like "
    "'Reply:' or 'note:' or 'remind me to'.\n"
    "End each suggestion line with the hashtag `#briefing/{today}` so the user "
    "can copy any line into a note and the hashtag survives into Obsidian for "
    "later filtering. Example:\n"
    "  1. Open a Portuguese Revolut local account — Revolut email mentioned smoother cross-border payments #briefing/{today}\n"
    "  2. Reply to Sarah re Q3 budget — she's asking by Thursday #briefing/{today}\n"
    "  3. Read Globerman's piece this weekend #briefing/{today}\n"
    "\n"
    "FILTER STRICTLY. Include only items that:\n"
    "  - Require an actual decision, reply, or time-sensitive action from the user\n"
    "  - Mention a deadline, ask a question, or propose something significant\n"
    "Skip ALL of these:\n"
    "  - Log/archive/track a receipt (\"Wolt receipt 316.90\" — NOT a suggestion)\n"
    "  - Fill out routine forms (\"daily activity tracker\" — too noisy)\n"
    "  - Newsletters / promotional content / one-way notifications\n"
    "  - Reminders to read/check generic content unless the user explicitly cares\n"
    "  - Anything the user obviously already knows or has handled\n"
    "If after filtering there are zero items worth suggesting, OMIT the entire "
    "*Suggestions* section. Better silent than noisy.\n"
    "After the list (only if any items): one final line exactly:\n"
    "  `Reply with what you want me to do — e.g. 'note: <copy line here>' to save (hashtag included), 'remind me to X' to schedule.`\n"
    "\n"
    "*Warnings* — one line per warning, terse.\n"
    "\n"
    "Payload:\n{payload}\n"
    "\n"
    "Briefing:"
)


async def morning_briefing_job() -> dict:
    """The parameter-free wrapper APScheduler invokes. Reads runtime state
    from the scheduler.runtime module so it can be pickled by the jobstore."""
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await morning_briefing(rt.agent, rt.db, rt.cfg)


async def morning_briefing(agent, db, cfg) -> dict:
    """The actual briefing logic. Callable directly for manual triggers
    (the `tars briefing` CLI subcommand)."""
    t0 = time.time()
    now = int(t0)
    tz = ZoneInfo(cfg.timezone)
    now_dt = datetime.fromtimestamp(t0, tz=tz)
    today = now_dt.date().isoformat()
    log.info("morning_briefing: running for date=%s", today)

    emails, email_err = await _safe_gmail(now)
    cal, cal_err = await _safe_calendar()
    fus = await _safe_followups(db, now_dt)
    warnings = [w for w in (email_err, cal_err) if w]

    # Build payload skipping empty sections — the LLM only renders headers
    # for keys actually present.
    payload: dict[str, Any] = {"date": today}
    if emails:
        payload["emails"] = emails
    if cal:
        payload["calendar"] = cal
    if fus:
        payload["open_followups"] = fus
    if warnings:
        payload["warnings"] = warnings

    out = await agent.chat(
        thread_key="job:morning_briefing",
        user_text=PROMPT_TEMPLATE.format(
            today=today,
            payload=json.dumps(payload, default=str, indent=2),
        ),
        tier="cron_default",
    )
    text = out["text"].strip() or "(briefing empty)"

    # Persist to briefings.
    await db.execute(
        "INSERT INTO briefings(date, summary, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET summary=excluded.summary, payload=excluded.payload",
        (today, text, json.dumps(payload, default=str)),
    )

    # Mirror to vault (Obsidian-readable). Non-fatal on failure.
    try:
        from tars.integrations.vault import write_briefing
        write_briefing(cfg, today, text)
    except Exception as e:  # noqa: BLE001
        log.warning("vault briefing mirror failed: %s", e)

    # Pull suggestion lines out so we can attach an inline keyboard to each.
    suggestion_texts = _extract_suggestions(text)

    # Send to each allowed chat. Open a fresh Bot session so this is independent
    # of the long-polling bot lifecycle.
    from tars.bot.actions import build_suggestion_keyboard, create_pending

    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                if suggestion_texts:
                    pending_ids = await create_pending(
                        db, chat_id=chat_id,
                        suggestions=[{"text": s} for s in suggestion_texts],
                        briefing_date=today,
                    )
                    kb = build_suggestion_keyboard(pending_ids)
                    await bot.send_message(chat_id, text, reply_markup=kb)
                else:
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
