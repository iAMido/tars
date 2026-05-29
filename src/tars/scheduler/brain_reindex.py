"""Brain-docs reindex — every 15 min.

Runs the diff-mode reindex (skip docs whose body_hash matches what's already
indexed). Catches any documents that drifted between the inline save_note
indexing and the FTS5 / vec0 tables — for example if the bot crashed mid-
indexing, or once the Obsidian phase introduces vault-side edits.

Cheap by design: at normal usage almost everything is skipped. Voyage cost
is bounded by what actually changed since last fire."""

from __future__ import annotations

import logging
import time

from tars.memory.embed import Embedder
from tars.memory.index import reindex_brain_docs

log = logging.getLogger("tars.scheduler.brain_reindex")


async def brain_reindex_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await brain_reindex(rt.db, rt.cfg)


async def brain_reindex(db, cfg) -> dict:
    t0 = time.time()
    embedder = Embedder(api_key=cfg.voyage.api_key)
    try:
        summary = await reindex_brain_docs(db, embedder, full=False)
    except Exception as e:  # noqa: BLE001
        log.warning("brain_reindex failed (%s)", e)
        return {"indexed": 0, "skipped": 0, "elapsed_s": time.time() - t0, "error": str(e)}
    log.info("brain_reindex: %s", summary)
    return summary
