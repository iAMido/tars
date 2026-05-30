"""News sources refresh — hourly.

Refreshes every enabled feed where kind='news'. Stores new entries into
feed_items. Does NOT send Telegram messages — news accumulates silently
in the brain so the morning briefing and search_memory can use it.

The brain_reindex job (every 15m) picks up new feed_items and embeds them
into brain_docs the next time it runs (Phase 6.1 — wired via _collect_documents
extension once feeds exist).
"""

from __future__ import annotations

import logging
import time

from tars.integrations.news import refresh_all

log = logging.getLogger("tars.scheduler.news_sources_refresh")


async def news_sources_refresh_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await news_sources_refresh(rt.db, rt.cfg)


async def news_sources_refresh(db, cfg) -> dict:
    t0 = time.time()
    summary = await refresh_all(db, kind="news")
    summary["elapsed_s"] = time.time() - t0
    log.info("news_sources_refresh: %s", summary)
    return summary
