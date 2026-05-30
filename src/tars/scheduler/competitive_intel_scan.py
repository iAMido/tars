"""Competitive intel scan — 09:00, 13:00, 17:00 daily.

Refreshes every enabled feed where kind='competitive', then summarizes
NEW items (since last fire) via the Agent at cron_default tier and pushes
to Telegram. Silent if no new items.

Differs from news_sources_refresh by:
  - kind='competitive' (you actively want a heads-up, vs general news)
  - posts to Telegram on new items (news stays in the brain quietly)
  - tracked through scheduler_state.competitive_intel.last_scan_ts so we
    don't re-announce items across fires within the same window.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from tars.integrations.news import refresh_all
from tars.scheduler.email_summary import state_get, state_set

log = logging.getLogger("tars.scheduler.competitive_intel_scan")

PROMPT_TEMPLATE = (
    "TARS voice. Heads-up on tracked competitors / domains. Format:\n"
    "*Competitive heads-up ({count})*\n"
    "- *<name>* — <one-line takeaway from the title/summary>\n"
    "Keep one line per item. No commentary. No greeting.\n"
    "\n"
    "New items (JSON):\n{payload}\n"
    "\n"
    "Message:"
)


async def competitive_intel_scan_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await competitive_intel_scan(rt.agent, rt.db, rt.cfg)


async def competitive_intel_scan(agent, db, cfg) -> dict:
    t0 = time.time()
    now = int(t0)
    tz = ZoneInfo(cfg.timezone)

    # Refresh all competitive feeds.
    refresh_summary = await refresh_all(db, kind="competitive")

    # Find items added since last scan.
    last_scan_str = await state_get(db, "competitive_intel.last_scan_ts")
    last_scan = int(last_scan_str) if last_scan_str else (now - 24 * 3600)

    rows = await db.fetch_all(
        "SELECT fi.id, fi.title, fi.url, fi.summary, fi.published_at, "
        " f.name AS feed_name "
        "FROM feed_items fi JOIN feeds f ON f.id = fi.feed_id "
        "WHERE f.kind = 'competitive' AND fi.fetched_at >= ? "
        "ORDER BY fi.fetched_at DESC LIMIT 20",
        (last_scan,),
    )
    new_items = [dict(r) for r in rows]
    await state_set(db, "competitive_intel.last_scan_ts", str(now))

    if not new_items:
        log.info("competitive_intel_scan: no new items since %d, silent",
                 last_scan)
        return {
            "refresh": refresh_summary,
            "new_items": 0,
            "sent": 0,
            "elapsed_s": time.time() - t0,
        }

    # Compose via Agent at cron_default tier.
    payload = [
        {
            "feed": it["feed_name"],
            "title": it["title"],
            "summary": (it["summary"] or "")[:300],
            "url": it["url"],
        }
        for it in new_items
    ]
    out = await agent.chat(
        thread_key="job:competitive_intel_scan",
        user_text=PROMPT_TEMPLATE.format(
            count=len(new_items),
            payload=json.dumps(payload, ensure_ascii=False, indent=2),
        ),
        tier="cron_default",
    )
    text = out["text"].strip() or "(empty)"

    bot = Bot(token=cfg.telegram.bot_token)
    sent = 0
    try:
        for chat_id in cfg.telegram.allowed_chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("competitive_intel_scan: send failed (%s)", e)
    finally:
        await bot.session.close()

    elapsed = time.time() - t0
    log.info(
        "competitive_intel_scan: new=%d sent=%d elapsed=%.2fs cost=$%.6f",
        len(new_items), sent, elapsed, out.get("cost_usd", 0.0),
    )
    return {
        "refresh": refresh_summary,
        "new_items": len(new_items),
        "sent": sent,
        "elapsed_s": elapsed,
        "cost_usd": out.get("cost_usd", 0.0),
    }
