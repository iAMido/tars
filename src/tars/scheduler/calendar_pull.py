"""Calendar pull — every 15 min.

Refreshes the cal_events table with the next 50 events. No LLM, no Telegram.
Just keeps the cache warm so morning_briefing and ad-hoc calendar tools have
sub-100ms access without hitting Google's API on the hot path.
"""

from __future__ import annotations

import logging
import time

from tars.integrations.gcal import cache_upcoming

log = logging.getLogger("tars.scheduler.calendar_pull")


async def calendar_pull_job() -> dict:
    """Parameter-free wrapper invoked by APScheduler."""
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await calendar_pull(rt.db, rt.cfg)


async def calendar_pull(db, cfg, n: int = 50) -> dict:
    t0 = time.time()
    try:
        count = await cache_upcoming(db, n=n)
    except Exception as e:  # noqa: BLE001
        log.warning("calendar_pull failed (%s); cache may be stale", e)
        return {"cached": 0, "elapsed_s": time.time() - t0, "error": str(e)}
    elapsed = time.time() - t0
    log.info("calendar_pull: %d events cached in %.2fs", count, elapsed)
    return {"cached": count, "elapsed_s": elapsed}
