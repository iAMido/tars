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
from tars.integrations.gmail import extract_email_addr, gather_briefing_intel
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
    # Matches any markdown section header (*X*, **X**, ***X***, # X, etc.)
    header_re = re.compile(r"^\s*(?:\*{1,3}|#+)\s*\w[\w\s-]*\*{0,3}\s*$")
    suggestions_re = re.compile(r"^\s*(?:\*{1,3}|#+)\s*Suggestions?\s*\*{0,3}\s*$", re.IGNORECASE)
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

OVERNIGHT_HOURS = 24            # was 12 — catches eve emails the user slept through
CAL_LOOKAHEAD = 6               # today's events; raised from 5 for fuller picture
FOLLOWUP_HORIZON_DAYS = 7
PENDING_REPLY_STALE_DAYS = 4    # treat unread > 4d as "you owe a reply"


async def _safe_email_intel(
    now: int, attendees: list[str],
) -> tuple[dict, str | None]:
    """Single call that pulls overnight unread + stale pending replies +
    per-attendee email context. Returns ({...}, error_or_None)."""
    since = now - OVERNIGHT_HOURS * 3600
    try:
        intel = await gather_briefing_intel(
            overnight_since_ts=since,
            attendee_addresses=attendees,
            pending_stale_days=PENDING_REPLY_STALE_DAYS,
        )
        return intel, None
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: gmail intel degraded (%s)", e)
        return (
            {"overnight_unread": [], "pending_replies": [], "by_attendee": {}},
            f"gmail unavailable: {type(e).__name__}",
        )


async def _yesterday_summary(db, today_iso: str) -> str | None:
    """Fetch yesterday's briefing summary if it exists, trimmed for the prompt.
    Used so the LLM doesn't repeat yesterday's takeaways verbatim."""
    from datetime import date, timedelta
    try:
        y = (date.fromisoformat(today_iso) - timedelta(days=1)).isoformat()
        row = await db.fetch_one(
            "SELECT summary FROM briefings WHERE date = ?", (y,)
        )
        if row and row["summary"]:
            # Keep it short — we only want the LLM to know "you said X yesterday".
            return (row["summary"] or "")[:1500]
    except Exception as e:  # noqa: BLE001
        log.warning("yesterday-summary lookup failed (%s)", e)
    return None


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
    "Compose today's morning briefing in TARS voice. You are not summarizing "
    "feeds — you are reasoning over the user's morning and telling him what "
    "actually matters today.\n"
    "\n"
    "STRICT format rules:\n"
    "- Render ONLY sections whose data is present below. If absent, OMIT the "
    "header entirely. Better silent than padded.\n"
    "- Possible section headers IN THIS ORDER: *Today*, *Email*, "
    "*Pending replies*, *Calendar*, *Open follow-ups*, *Suggestions*, *Warnings*.\n"
    "- Headers use SINGLE asterisks: `*Email*`. Never `**double**` — "
    "Telegram renders that literally.\n"
    "- No greeting, no sign-off, no commentary outside section bodies.\n"
    "- Do NOT repeat anything verbatim from `yesterday_summary` (provided for "
    "context only — so today doesn't sound like a copy of yesterday).\n"
    "\n"
    "*Today* — REQUIRED IF the payload is non-trivial. 1-3 short lines "
    "synthesizing what the day is actually about. Connect the dots: if a "
    "meeting has unread email from the attendee, say so. If a follow-up is "
    "due today, lead with it. This is the line the user reads first — make "
    "it earn its place. If nothing meaningful, skip this section.\n"
    "\n"
    "*Email* — one bullet per overnight unread email starting with `- `:\n"
    "  `- <From shortened to name or org> — <one-line summary of what the "
    "email actually says, not just the subject>`\n"
    "  Read the body. Mention concrete facts (numbers, deadlines, names) the "
    "user should know. Skip footer/unsubscribe/legalese. Skip pure promo "
    "fluff but DO mention 1-2 useful items from a newsletter if there are any.\n"
    "\n"
    "*Pending replies* — emails sitting unread for "
    f"{PENDING_REPLY_STALE_DAYS}+ days from real people (not lists). One "
    "bullet each: `- <From> — <Subject> (Nd ago)`. ONLY include if the "
    "sender seems to actually expect a reply. Skip if all of pending_replies "
    "are noise.\n"
    "\n"
    "*Calendar* — one bullet per event:\n"
    "  `- HH:MM — Title` (today) or `- YYYY-MM-DD HH:MM — Title` (other day).\n"
    "  If an event has `email_context`, add an INDENTED sub-bullet RIGHT "
    "BELOW it:\n"
    "  `   · prep: <1-line synthesis of the recent email thread with this "
    "attendee — what did they last say, what are they likely to ask?>`\n"
    "  Only add the prep line when the email_context actually contains "
    "something useful. If the events with attendees have no email_context, "
    "no sub-bullet.\n"
    "\n"
    "*Open follow-ups* — one bullet per item: `- <body> (due_human) [followup:N]`.\n"
    "\n"
    "*Suggestions* — purely OPTIONAL ideas the user might want to act on. "
    "These are NOT things you (TARS) are doing — just things the user could "
    "ask you to do. Numbered list, plain language. No verb prefixes like "
    "'Reply:' or 'note:' or 'remind me to'. NO hashtags — tags are "
    "auto-applied to saved notes already.\n"
    "Example:\n"
    "  1. Open a Portuguese Revolut local account — Revolut email mentioned "
    "smoother cross-border payments\n"
    "  2. Reply to Sarah re Q3 budget — she's asking by Thursday\n"
    "\n"
    "FILTER STRICTLY. Include only items that:\n"
    "  - Require an actual decision, reply, or time-sensitive action\n"
    "  - Mention a deadline, ask a question, or propose something significant\n"
    "Skip ALL of:\n"
    "  - Log/archive/track a receipt (\"Wolt receipt 316.90\" — NOT a suggestion)\n"
    "  - Routine forms (\"daily activity tracker\" — too noisy)\n"
    "  - Newsletters / promotional / one-way notifications\n"
    "  - Reminders to read generic content unless the user explicitly cares\n"
    "  - Anything the user obviously already knows or has handled\n"
    "If zero items pass the filter, OMIT the entire *Suggestions* section.\n"
    "No footer line — the user has tap-to-act buttons attached.\n"
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

    # Calendar first so we know which attendees to enrich with email context.
    cal, cal_err = await _safe_calendar()
    attendee_set: set[str] = set()
    for ev in cal:
        for raw in (ev.get("attendees") or []):
            addr = extract_email_addr(raw)
            if addr and "@" in addr:
                attendee_set.add(addr)

    intel, email_err = await _safe_email_intel(now, sorted(attendee_set))
    fus = await _safe_followups(db, now_dt)
    yesterday = await _yesterday_summary(db, today)
    warnings = [w for w in (email_err, cal_err) if w]

    # Cross-reference: attach by_attendee context onto each calendar event.
    by_att = intel.get("by_attendee") or {}
    enriched_cal: list[dict] = []
    for ev in cal:
        ctx: list[dict] = []
        for raw in (ev.get("attendees") or []):
            addr = extract_email_addr(raw)
            for m in (by_att.get(addr) or [])[:3]:
                ctx.append({
                    "from": m["from"], "subject": m["subject"],
                    "date": m["date"], "snippet": m["snippet"],
                })
        ev_out = dict(ev)
        if ctx:
            ev_out["email_context"] = ctx
        enriched_cal.append(ev_out)

    # Annotate pending replies with how stale each one is so the LLM can
    # phrase the urgency correctly.
    pending: list[dict] = []
    for m in (intel.get("pending_replies") or []):
        ts = m.get("internal_ts") or 0
        days_old = max(1, int((now - ts) / 86400)) if ts else None
        pending.append({
            "from": m["from"], "subject": m["subject"],
            "date": m["date"], "snippet": m["snippet"],
            "days_old": days_old,
        })

    emails = intel.get("overnight_unread") or []

    # Build payload — drop empty keys so the LLM omits their headers entirely.
    payload: dict[str, Any] = {"date": today}
    if yesterday:
        payload["yesterday_summary"] = yesterday
    if emails:
        payload["emails"] = emails
    if pending:
        payload["pending_replies"] = pending
    if enriched_cal:
        payload["calendar"] = enriched_cal
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
    # DeepSeek sometimes emits **double** asterisks despite the prompt rule;
    # collapse to single so Telegram's Markdown parser renders them as bold.
    text = re.sub(r"\*\*(\S[^*\n]*?\S)\*\*", r"*\1*", text)

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
                    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")
                else:
                    await bot.send_message(chat_id, text, parse_mode="Markdown")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("morning_briefing: send_message to %s failed (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "morning_briefing: done date=%s emails=%d pending=%d cal=%d att=%d "
        "followups=%d yest=%s sent=%d elapsed=%.2fs cost=$%.6f",
        today, len(emails), len(pending), len(cal), len(attendee_set),
        len(fus), bool(yesterday), sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "date": today,
        "emails": len(emails),
        "pending_replies": len(pending),
        "calendar": len(cal),
        "attendees_enriched": sum(1 for e in enriched_cal if e.get("email_context")),
        "followups": len(fus),
        "yesterday_carried": bool(yesterday),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
