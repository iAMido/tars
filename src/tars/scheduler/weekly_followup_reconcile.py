"""Weekly follow-up reconcile — Sunday 18:00.

The accountability nag. Reopens follow-ups whose due_at passed (bumps
reopened_count). Then composes a Telegram message listing all currently
open follow-ups with their due times, ages, and reopen counts.

No LLM call — the format is deterministic and TARS-voice already.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.memory.follow_ups import list_open, reopen_stale

log = logging.getLogger("tars.scheduler.weekly_followup_reconcile")


async def weekly_followup_reconcile_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await weekly_followup_reconcile(rt.db, rt.cfg)


def _human_due(due_ts: int | None, now_dt: datetime) -> str:
    if not due_ts:
        return "no due date"
    due = datetime.fromtimestamp(due_ts, tz=now_dt.tzinfo)
    delta_days = (due.date() - now_dt.date()).days
    time_part = due.strftime("%H:%M")
    if delta_days < 0:
        return f"overdue {abs(delta_days)}d"
    if delta_days == 0:
        return f"today {time_part}"
    if delta_days == 1:
        return f"tomorrow {time_part}"
    if delta_days <= 7:
        return f"{due.strftime('%A')} {time_part}"
    return f"{due.date().isoformat()} {time_part}"


async def weekly_followup_reconcile(db, cfg) -> dict:
    t0 = time.time()
    tz = ZoneInfo(cfg.timezone)
    now_dt = datetime.now(tz)

    reopened = await reopen_stale(db, now_ts=int(t0))
    open_fus = await list_open(db, limit=30)
    log.info(
        "weekly_followup_reconcile: reopened=%d open=%d",
        len(reopened), len(open_fus),
    )

    if not open_fus:
        return {"reopened": len(reopened), "open": 0, "sent": 0, "elapsed_s": time.time() - t0}

    # Format deterministically. TARS voice = terse table.
    lines = [f"*Open follow-ups ({len(open_fus)})*"]
    for f in open_fus:
        due_human = _human_due(f.get("due_at"), now_dt)
        body = (f.get("body") or "").split("\n")[0][:80]
        reopens = f.get("reopened_count") or 0
        reopen_str = f" reopens={reopens}" if reopens else ""
        lines.append(f"- [followup:{f['followup_id']}] {body} ({due_human}{reopen_str})")
    text = "\n".join(lines)

    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile: send failed to %s (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "weekly_followup_reconcile: reopened=%d open=%d sent=%d elapsed=%.2fs",
        len(reopened), len(open_fus), sent, elapsed,
    )
    return {
        "reopened": len(reopened),
        "open": len(open_fus),
        "sent": sent,
        "elapsed_s": elapsed,
    }
