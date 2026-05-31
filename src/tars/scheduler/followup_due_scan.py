"""Follow-up due-time nudge — runs every 2 minutes.

The accountability ping. When a follow-up's `due_at` has passed and we
haven't sent a Telegram nudge for it yet, send one with inline action
buttons (✅ Done, ⏰ +1h, ⏰ Tomorrow 9am, ⏰ Custom).

Why this is a separate job from `weekly_followup_reconcile`:
  - The weekly job is the Sunday-evening "you still owe these" digest.
  - This job is the moment-of-due-time ping. Different cadence, different
    UX (single follow-up per message with action buttons, not a list).

Idempotency: `last_nudged_at` is updated after each successful send. The
condition `(last_nudged_at IS NULL OR last_nudged_at < due_at)` makes
re-snoozes naturally re-eligible — the snooze handler moves `due_at`
forward but leaves `last_nudged_at` alone, so the new due_at > last
nudge → the row qualifies again when the new due_at passes.

No LLM call — deterministic format, TARS-voice already.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.bot.actions import build_followup_nudge_keyboard

log = logging.getLogger("tars.scheduler.followup_due_scan")


async def followup_due_scan_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await followup_due_scan(rt.db, rt.cfg)


def _human_age(due_ts: int, now_ts: int) -> str:
    delta = now_ts - due_ts
    if delta < 0:
        return "due now"
    if delta < 3600:
        return f"due {delta // 60}m ago"
    if delta < 86400:
        return f"due {delta // 3600}h ago"
    return f"due {delta // 86400}d ago"


async def followup_due_scan(db, cfg) -> dict:
    """Find every open follow-up whose due_at has passed and isn't yet pinged.
    Send one Telegram message per follow-up with inline action buttons."""
    t0 = time.time()
    now_ts = int(t0)
    tz = ZoneInfo(cfg.timezone)

    rows = await db.fetch_all(
        "SELECT fu.id AS fu_id, fu.note_id, fu.due_at, fu.promised_to, "
        "       fu.reopened_count, n.body "
        "FROM follow_ups fu JOIN notes n ON n.id = fu.note_id "
        "WHERE fu.status = 'open' "
        "  AND fu.due_at IS NOT NULL "
        "  AND fu.due_at <= ? "
        "  AND (fu.last_nudged_at IS NULL OR fu.last_nudged_at < fu.due_at) "
        "ORDER BY fu.due_at ASC",
        (now_ts,),
    )

    if not rows:
        return {"checked_at": now_ts, "pinged": 0, "elapsed_s": time.time() - t0}

    bot = Bot(token=cfg.telegram.bot_token)
    pinged = 0
    try:
        for r in rows:
            fu_id = int(r["fu_id"])
            due_ts = int(r["due_at"])
            body = (r["body"] or "").strip().split("\n")[0][:200]
            promised = f" · to {r['promised_to']}" if r["promised_to"] else ""
            reopens = int(r["reopened_count"] or 0)
            reopen_str = f" · reopens={reopens}" if reopens else ""
            due_local = datetime.fromtimestamp(due_ts, tz=tz).strftime("%a %H:%M")
            age = _human_age(due_ts, now_ts)

            text = (
                f"⏰ *Reminder*\n"
                f"_{body}_\n"
                f"({due_local} — {age}{promised}{reopen_str}) "
                f"[note:{r['note_id']}] [followup:{fu_id}]"
            )
            kb = build_followup_nudge_keyboard(fu_id)

            for chat_id in cfg.telegram.allowed_chat_ids:
                try:
                    await bot.send_message(
                        chat_id, text, parse_mode="Markdown", reply_markup=kb
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "followup_due_scan: send failed fu=%d chat=%s (%s)",
                        fu_id, chat_id, e,
                    )

            # Record the ping even if some recipients failed; otherwise a
            # single bad chat_id would cause infinite re-pinging.
            await db.execute(
                "UPDATE follow_ups SET last_nudged_at = ? WHERE id = ?",
                (now_ts, fu_id),
            )
            pinged += 1
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "followup_due_scan: pinged=%d elapsed=%.2fs",
        pinged, elapsed,
    )
    return {"checked_at": now_ts, "pinged": pinged, "elapsed_s": elapsed}
