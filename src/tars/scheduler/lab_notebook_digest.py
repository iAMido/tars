"""Lab notebook digest — Thursdays at 16:00.

Pulls every note from the last 7 days (excluding our own thread summaries),
composes a TARS-voice weekly review with themes / accomplishments / open
questions, sends to Telegram, and saves as a note (so it's searchable later).

Thursdays specifically because a Friday-arrival weekly review lands after
people have already mentally moved on. Thursday afternoon gives you a
chance to act on what you notice before the weekend.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

log = logging.getLogger("tars.scheduler.lab_notebook_digest")

WEEK_DAYS = 7

PROMPT_TEMPLATE = (
    "TARS voice. Weekly review of notes from the last 7 days.\n"
    "\n"
    "Structure (skip any section without content):\n"
    "*Themes* — recurring topics across the week, 1-2 lines.\n"
    "*Decisions* — concrete decisions or commitments made, cite [note:N].\n"
    "*Open questions* — things still unresolved.\n"
    "*Follow-ups noted* — anything that should become a follow-up but isn't yet.\n"
    "\n"
    "Terse. Bullets only inside listed sections. No greeting. No 'this week was...'\n"
    "\n"
    "Notes from the last 7 days (JSON):\n{payload}\n"
    "\n"
    "Review:"
)


async def lab_notebook_digest_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await lab_notebook_digest(rt.agent, rt.db, rt.cfg)


async def lab_notebook_digest(agent, db, cfg) -> dict:
    t0 = time.time()
    now = int(t0)
    tz = ZoneInfo(cfg.timezone)
    cutoff = now - WEEK_DAYS * 86400

    rows = await db.fetch_all(
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       source, body "
        "FROM notes "
        "WHERE created_at >= ? AND source NOT IN ('thread_summary', 'weekly_digest') "
        "ORDER BY id",
        (cutoff,),
    )

    if not rows:
        log.info("lab_notebook_digest: no notes in last %dd, silent", WEEK_DAYS)
        return {"notes": 0, "sent": 0, "elapsed_s": time.time() - t0}

    payload = [
        {
            "id": int(r["id"]),
            "created": r["created"],
            "source": r["source"],
            "body": (r["body"] or "")[:400],
        }
        for r in rows
    ]

    out = await agent.chat(
        thread_key="job:lab_notebook_digest",
        user_text=PROMPT_TEMPLATE.format(
            payload=json.dumps(payload, ensure_ascii=False, indent=2),
        ),
        tier="cron_default",
    )
    text = (out["text"] or "").strip() or "(empty digest)"

    # Save the digest as a note so it's searchable.
    week_label = datetime.fromtimestamp(t0, tz=tz).strftime("%Y-W%V")
    cur = await db.execute(
        "INSERT INTO notes(created_at, source, body, tags) VALUES (?, ?, ?, ?)",
        (
            now, "weekly_digest",
            f"Weekly digest [{week_label}]:\n\n{text}",
            json.dumps(["weekly_digest", week_label]),
        ),
    )
    digest_note_id = cur.lastrowid

    # Send to Telegram.
    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("lab_notebook_digest: send failed (%s)", e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "lab_notebook_digest: notes=%d week=%s digest_note=%s sent=%d elapsed=%.2fs cost=$%.6f",
        len(rows), week_label, digest_note_id, sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "notes": len(rows),
        "week": week_label,
        "digest_note_id": digest_note_id,
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
