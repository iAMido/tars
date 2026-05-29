"""Email summary — every 30 min.

Pulls unread Gmail since the last check. If >= MIN_NEW_THREADS new threads
accumulated, composes a 2-line summary at cron_default tier and pushes to
Telegram. Otherwise stays silent.

Persists state in scheduler_state(key=email_summary.last_seen_ts).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.integrations.gmail import fetch_unread_since

log = logging.getLogger("tars.scheduler.email_summary")

MIN_NEW_THREADS = 3
DEFAULT_LOOKBACK_SECONDS = 12 * 3600  # first run after a clean DB looks back 12h

# Quiet hours (inclusive start, exclusive end). Don't ping Telegram between
# QUIET_START and QUIET_END local time. Still INGEST email so we don't
# re-summarize the same backlog at 07:00 — just suppress the message.
# Morning briefing at 05:00 handles the wake-up catch-up.
QUIET_START_HOUR = 22  # 22:00 — stop pinging
QUIET_END_HOUR = 7    # 07:00 — resume pinging


def _in_quiet_hours(now_dt: datetime) -> bool:
    """Quiet block wraps midnight: [22:00 -> 07:00) is silent."""
    h = now_dt.hour
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= h < QUIET_END_HOUR
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


# ---------------------------------------------------------------------------
# scheduler_state helpers (used by multiple jobs)
# ---------------------------------------------------------------------------


async def state_get(db, key: str) -> str | None:
    row = await db.fetch_one("SELECT value FROM scheduler_state WHERE key = ?", (key,))
    return row["value"] if row else None


async def state_set(db, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO scheduler_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, int(time.time())),
    )


# ---------------------------------------------------------------------------
# Job entrypoint
# ---------------------------------------------------------------------------


async def email_summary_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await email_summary(rt.agent, rt.db, rt.cfg)


PROMPT_TEMPLATE = (
    "Compose a 2-3 line interim email update in TARS voice. "
    "List by thread, 'From — Subject (snippet)'. No greeting. No 'check your inbox' suggestion.\n"
    "\n"
    "Threads (JSON):\n{payload}\n"
    "\n"
    "Update:"
)


async def email_summary(agent, db, cfg) -> dict:
    t0 = time.time()
    now = int(t0)
    now_dt = datetime.now(ZoneInfo(cfg.timezone))

    # Read last_seen_ts; default to lookback window for first run.
    last_seen_str = await state_get(db, "email_summary.last_seen_ts")
    if last_seen_str:
        last_seen = int(last_seen_str)
    else:
        last_seen = now - DEFAULT_LOOKBACK_SECONDS

    try:
        msgs = await fetch_unread_since(last_seen, max_results=20)
    except Exception as e:  # noqa: BLE001
        log.warning("email_summary: gmail fetch failed (%s); skipping this fire", e)
        return {"checked": True, "new": 0, "sent": 0, "elapsed_s": time.time() - t0}

    # Bookkeep last_seen regardless of whether we send so we don't re-summarize.
    await state_set(db, "email_summary.last_seen_ts", str(now))

    # Quiet hours: ingest only, no Telegram. The next non-quiet fire will see
    # accumulated unread since last_seen was just updated to NOW. To avoid
    # losing the backlog: roll last_seen back if we suppress the send.
    if _in_quiet_hours(now_dt):
        # Restore the prior last_seen so the first non-quiet fire summarizes
        # everything that arrived during quiet hours, not just the most recent.
        await state_set(db, "email_summary.last_seen_ts", str(last_seen))
        log.info(
            "email_summary: in quiet hours (%02d:%02d) — ingested %d, not sending",
            now_dt.hour, now_dt.minute, len(msgs),
        )
        return {
            "new": len(msgs),
            "sent": 0,
            "quiet_hours": True,
            "elapsed_s": time.time() - t0,
        }

    if len(msgs) < MIN_NEW_THREADS:
        log.info(
            "email_summary: %d new (< floor=%d), staying silent",
            len(msgs), MIN_NEW_THREADS,
        )
        return {"new": len(msgs), "sent": 0, "elapsed_s": time.time() - t0}

    # Compose.
    payload = [
        {"from": m["from"], "subject": m["subject"], "snippet": m["snippet"][:140]}
        for m in msgs
    ]
    out = await agent.chat(
        thread_key="job:email_summary",
        user_text=PROMPT_TEMPLATE.format(payload=json.dumps(payload, ensure_ascii=False, indent=2)),
        tier="cron_default",
    )
    text = out["text"].strip() or "(empty summary)"

    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("email_summary: send failed to %s (%s)", chat_id, e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "email_summary: new=%d sent=%d elapsed=%.2fs cost=$%.6f",
        len(msgs), sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "new": len(msgs),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
