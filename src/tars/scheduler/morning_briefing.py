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
from tars.integrations.gmail import (
    extract_email_addr,
    gather_briefing_intel,
    get_self_email,
)
from tars.integrations.quote import fetch_quote_of_the_day
from tars.integrations.running_coach import fetch_today_training
from tars.integrations.weather import fetch_today_forecast
from tars.memory.follow_ups import list_open
from tars.memory.search import hybrid_search
from tars.tools import _get_embedder

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
RECENT_NOTES_HOURS = 36         # everything jotted in the last day-and-a-half
RECENT_NOTES_MAX = 20
RELEVANT_NOTES_MAX = 10         # semantic retrieval cap (after recent_notes dedup)
RELEVANT_NOTES_PER_QUERY_K = 3  # top-k per individual context query
RELEVANT_NOTES_QUERY_CAP = 6    # max distinct queries we run (Voyage RPM)
NOTE_BODY_MAX = 400             # per-note body truncation for prompt


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


async def _recent_notes(db, now_ts: int) -> list[dict]:
    """Last 36h of notes, body included. Always-on context — the LLM should
    know what the user has been thinking about, even if no semantic search
    surfaces it. Excludes auto-generated reminder-close notes (noise)."""
    cutoff = now_ts - RECENT_NOTES_HOURS * 3600
    try:
        rows = await db.fetch_all(
            "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
            "       body, tags "
            "FROM notes "
            "WHERE created_at >= ? "
            "  AND status != 'closed' "
            "  AND (tags IS NULL OR tags NOT LIKE '%source/reminder-ping%') "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, RECENT_NOTES_MAX),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("recent_notes query failed (%s)", e)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        out.append({
            "id": int(r["id"]),
            "created": r["created"],
            "body": (r["body"] or "")[:NOTE_BODY_MAX],
            "tags": tags,
        })
    return out


async def _relevant_notes_for_today(
    db, cfg, queries: list[str], exclude_ids: set[int],
) -> list[dict]:
    """For each query (calendar title / email subject / follow-up body),
    semantically retrieve top-k notes. Dedupe, exclude ids already in
    recent_notes, cap globally. Returns a small set the LLM can use to
    connect today's items to past thinking."""
    if not queries:
        return []
    try:
        embedder = _get_embedder(db, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("relevant_notes: embedder unavailable (%s)", e)
        return []

    seen: dict[int, dict] = {}
    consecutive_failures = 0
    issued = 0
    for q in queries:
        if issued >= RELEVANT_NOTES_QUERY_CAP:
            break
        q = (q or "").strip()
        if not q or len(q) < 4:
            continue
        issued += 1
        try:
            hits = await hybrid_search(
                db, embedder, query=q, k=RELEVANT_NOTES_PER_QUERY_K,
            )
            consecutive_failures = 0
        except Exception as e:  # noqa: BLE001
            log.warning("relevant_notes search %r failed (%s)", q[:40], e)
            consecutive_failures += 1
            # Rate-limit or auth failure → all subsequent calls will fail
            # the same way. Bail rather than spam the log.
            if consecutive_failures >= 2:
                log.warning(
                    "relevant_notes: 2 consecutive failures — skipping rest"
                )
                break
            continue
        for h in hits:
            if h.get("source") != "note":
                continue
            doc_id = int(h.get("doc_id") or 0)
            if doc_id in exclude_ids or doc_id in seen:
                continue
            seen[doc_id] = {
                "id": doc_id,
                "title": h.get("title") or "",
                "body": (h.get("body") or "")[:NOTE_BODY_MAX],
                "matched_query": q[:80],
                "score": round(float(h.get("score") or 0.0), 4),
            }
            if len(seen) >= RELEVANT_NOTES_MAX:
                break
        if len(seen) >= RELEVANT_NOTES_MAX:
            break
    # Best matches first.
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


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
    "- NEVER write 'None.', 'Nothing.', 'N/A', '(empty)', or any placeholder "
    "in place of a section body. If after your own filtering a section has "
    "nothing worth including, OMIT the header AND body completely.\n"
    "- When you cite a follow-up id, wrap the bracketed token in backticks: "
    "`` `[followup:N]` `` — Telegram's Markdown swallows `[...]` otherwise.\n"
    "- Possible section headers IN THIS ORDER: *Weather*, *Quote*, "
    "*Training*, *Today*, *Email*, *Pending replies*, *Calendar*, "
    "*Open follow-ups*, *Open todos*, *Suggestions*, *Warnings*.\n"
    "- Headers use SINGLE asterisks: `*Email*`. Never `**double**` — "
    "Telegram renders that literally.\n"
    "- No greeting, no sign-off, no commentary outside section bodies.\n"
    "- Do NOT repeat anything verbatim from `yesterday_summary` (provided for "
    "context only — so today doesn't sound like a copy of yesterday).\n"
    "\n"
    "*Weather* — ONE line. Use payload.weather. Format the line as "
    "`<location> <min>–<max>°C, <headline>` and append `, <N>mm rain` only "
    "if rain_today_mm > 1. Example output: `Kfar Saba 19–27°C, clear sky`. "
    "No bullets, no extra lines.\n"
    "\n"
    "*Training* — payload.training present. EXACTLY 2 lines, no bullets:\n"
    "  Line 1: ``<plan.type> · Week <plan.current_week>/<plan.total_weeks>``\n"
    "  Line 2: ``Today (<day_of_week>): <workout>`` — synthesize <workout> "
    "from today.type + today.distance + today.description in your own "
    "concise wording. If today is rest, write `Today: rest day`.\n"
    "  DO NOT add a week-outline line. Ignore week_outline in the payload.\n"
    "  No emoji. No motivational fluff. Just the prescription.\n"
    "\n"
    "*Today* — the user reads this FIRST. Make it earn its place. "
    "Format: 2-4 BULLETS (`- `), each one ONE short sentence. NOT a "
    "paragraph. Each bullet is a distinct thing the user should know.\n"
    "Priority ordering (top to bottom):\n"
    "  1. Anything URGENT today (overdue follow-up, deadline today, "
    "burning email)\n"
    "  2. Today's most important event/decision/meeting + any prep "
    "context (notes, prior emails)\n"
    "  3. A non-obvious connection (e.g. \"three things today all touch "
    "your Portugal move\")\n"
    "  4. One forward-looking item if a near-future event needs prep "
    "(this week)\n"
    "STRICT skips:\n"
    "  - Don't list things already DONE today (closed follow-ups, "
    "shipped fixes) — they belong in evening_wrapup, not morning *Today*\n"
    "  - Don't restate Calendar verbatim — *Today* is interpretation, "
    "not duplication\n"
    "  - Don't pad. 2 strong bullets > 4 weak ones. If only one thing "
    "matters, ONE bullet.\n"
    "Cite notes inline with backticks: `` `[note:N]` ``. Only cite ids "
    "present in recent_notes or relevant_notes payload below.\n"
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
    "*Calendar* — one bullet per event. RTL-safe format — wrap the time "
    "in backticks so Telegram's bidi algorithm doesn't flip date components "
    "on lines containing Hebrew titles:\n"
    "  `` - `HH:MM` Title `` (today) or `` - `YYYY-MM-DD HH:MM` Title `` "
    "(other day). No em-dash before the title — the backticks provide the "
    "visual separation.\n"
    "  If an event has `email_context`, add an INDENTED sub-bullet RIGHT "
    "BELOW it:\n"
    "  `   · prep: <1-line synthesis of the recent email thread with this "
    "attendee — what did they last say, what are they likely to ask?>`\n"
    "  Only add the prep line when the email_context actually contains "
    "something useful. If the events with attendees have no email_context, "
    "no sub-bullet.\n"
    "  If a recent_note or relevant_note relates to this meeting (same "
    "person, project, or topic), MENTION it in the prep line and cite "
    "with `` `[note:N]` ``.\n"
    "\n"
    "*Open follow-ups* — one bullet per item: ``- <body> (due_human) `[followup:N]` ``.\n"
    "\n"
    "*Open todos* — surface open `- [ ]` items from PARA files. Group by "
    "FILE (one sub-section per file with the file as bold). Cap at 3 files "
    "and 4 items per file. Skip any file with zero open. Format:\n"
    "  *<path>* (<open_count> open)\n"
    "  - <item 1>\n"
    "  - <item 2>\n"
    "Choose items that look most action-worthy (deadlines, names, "
    "specific actions). Skip vague placeholders ('-', single-word items). "
    "Skip the entire section if payload.open_todos is empty or all the "
    "items are noise.\n"
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
    "*Quote* — payload.quote present? Render as: `_\"<text>\"_ — <author>` "
    "(italic with underscores, em-dash before author). No leading bullet, "
    "no commentary. Skip if no quote in payload.\n"
    "\n"
    "NOTES IN THE PAYLOAD — read carefully:\n"
    "  - `recent_notes` = everything the user jotted in the last 36h. Use "
    "this as context for what's on their mind. Don't list them as their "
    "own section — instead weave them into *Today*, meeting prep, or "
    "*Suggestions* when they connect.\n"
    "  - `relevant_notes` = older notes that semantically match today's "
    "calendar/email/follow-ups. Each has a `matched_query` showing WHY it "
    "matched. Use it to surface forgotten context (\"you flagged this 3 "
    "weeks ago\").\n"
    "  - Only cite `[note:N]` when N actually appears in `recent_notes` or "
    "`relevant_notes` payload below. Never invent ids.\n"
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
    self_email = await get_self_email()  # cached after first call
    attendee_set: set[str] = set()
    for ev in cal:
        for raw in (ev.get("attendees") or []):
            addr = extract_email_addr(raw)
            if addr and "@" in addr and addr != self_email:
                attendee_set.add(addr)

    intel, email_err = await _safe_email_intel(now, sorted(attendee_set))
    fus = await _safe_followups(db, now_dt)
    yesterday = await _yesterday_summary(db, today)
    weather = await fetch_today_forecast(cfg)       # None on failure or no key
    quote = await fetch_quote_of_the_day()          # None on failure
    training = await fetch_today_training(cfg)      # None when no plan or not configured

    # Open todos scanned from PARA files. Capped tight to keep payload sane.
    open_todos: list[dict] = []
    try:
        from tars.tools import list_open_todos
        import json as _json_inner
        raw = await list_open_todos(db, {"max_per_file": 5, "max_total": 12})
        parsed = _json_inner.loads(raw)
        if parsed.get("total_open"):
            open_todos = parsed.get("files") or []
    except Exception as e:  # noqa: BLE001
        log.warning("morning_briefing: todos scan failed (%s)", e)
    warnings = [w for w in (email_err, cal_err) if w]

    # Cross-reference: attach by_attendee context onto each calendar event.
    by_att = intel.get("by_attendee") or {}
    enriched_cal: list[dict] = []
    for ev in cal:
        ctx: list[dict] = []
        for raw in (ev.get("attendees") or []):
            addr = extract_email_addr(raw)
            if addr == self_email:
                continue
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

    # Notes context — what the user has been thinking about + which past
    # notes connect to today's items.
    recent_notes = await _recent_notes(db, now)
    recent_ids = {n["id"] for n in recent_notes}
    # Build semantic queries from today's surface area. We deliberately limit
    # each item to a short string — the search is hybrid (FTS5 + vec), so
    # short queries work fine and keep embedding cost negligible.
    queries: list[str] = []
    for ev in enriched_cal:
        title = (ev.get("title") or "").strip()
        if title and title not in queries:
            queries.append(title)
    for m in emails[:5]:
        subj = (m.get("subject") or "").strip()
        if subj and subj not in queries:
            queries.append(subj)
    for f in fus[:5]:
        body = (f.get("body") or "").strip().split("\n")[0]
        if body and body not in queries:
            queries.append(body)
    relevant_notes = await _relevant_notes_for_today(
        db, cfg, queries, exclude_ids=recent_ids,
    )

    # Build payload — drop empty keys so the LLM omits their headers entirely.
    payload: dict[str, Any] = {"date": today}
    if weather:
        payload["weather"] = weather
    if training:
        payload["training"] = training
    if quote:
        payload["quote"] = quote
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
    if open_todos:
        payload["open_todos"] = open_todos
    if recent_notes:
        payload["recent_notes"] = recent_notes
    if relevant_notes:
        payload["relevant_notes"] = relevant_notes
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
    from tars.bot.send import safe_send

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
                    await safe_send(bot, chat_id, text, reply_markup=kb)
                else:
                    await safe_send(bot, chat_id, text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("morning_briefing: send_message to %s failed (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "morning_briefing: done date=%s emails=%d pending=%d cal=%d att=%d "
        "followups=%d recent_notes=%d relevant_notes=%d yest=%s wx=%s "
        "quote=%s train=%s sent=%d elapsed=%.2fs cost=$%.6f",
        today, len(emails), len(pending), len(cal), len(attendee_set),
        len(fus), len(recent_notes), len(relevant_notes), bool(yesterday),
        bool(weather), bool(quote), bool(training),
        sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "date": today,
        "emails": len(emails),
        "pending_replies": len(pending),
        "calendar": len(cal),
        "attendees_enriched": sum(1 for e in enriched_cal if e.get("email_context")),
        "followups": len(fus),
        "recent_notes": len(recent_notes),
        "relevant_notes": len(relevant_notes),
        "yesterday_carried": bool(yesterday),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
