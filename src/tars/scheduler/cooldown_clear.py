"""Cooldown clear — every 5 min.

Clears any provider cooldowns whose expiry has passed. The router itself
already checks `_cooldowns[provider] > time.time()` so expired entries are
harmless, but this job keeps the dict from accumulating dead keys over months.

Also surfaces a clear log line if a provider is currently still cooled down,
so the dashboard / logs make any ongoing provider outage visible.
"""

from __future__ import annotations

import logging
import time

from tars import router

log = logging.getLogger("tars.scheduler.cooldown_clear")


async def cooldown_clear_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await cooldown_clear(rt.db, rt.cfg)


async def cooldown_clear(db, cfg) -> dict:
    now = time.time()
    snap = router._state_snapshot()
    cleared = []
    still_cooled = []
    for provider, until_ts in list(snap.get("cooldowns", {}).items()):
        if until_ts <= now:
            router._cooldowns.pop(provider, None)
            cleared.append(provider)
        else:
            still_cooled.append((provider, int(until_ts - now)))
    if cleared:
        log.info("cleared expired cooldowns: %s", cleared)
    if still_cooled:
        log.warning(
            "providers still cooled down: %s",
            ", ".join(f"{p} ({s}s left)" for p, s in still_cooled),
        )
    return {"cleared": cleared, "still_cooled": still_cooled}
