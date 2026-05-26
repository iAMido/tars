"""TARS entrypoint.

Phase 1: connect to SQLite, run migrations, log status, exit clean.
Phase 2+ will add Agent/router; Phase 3 adds aiogram + APScheduler + FastAPI.

Run with:
    uv run python -m tars
"""

from __future__ import annotations

import asyncio
import logging
import sys

from tars import __version__
from tars.config import load_config
from tars.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("tars")


async def main() -> int:
    log.info("TARS %s booting", __version__)

    try:
        cfg = load_config()
    except FileNotFoundError as e:
        log.error("Config error: %s", e)
        return 2

    log.info("Config loaded. timezone=%s db=%s", cfg.timezone, cfg.paths.db)

    db = await Database.connect(cfg.paths.db)
    try:
        version = await db.migrate()
        log.info("DB migrated to schema version %d", version)

        # Phase 1 smoke check: verify all expected tables exist.
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view') ORDER BY name"
        )
        names = [r["name"] for r in rows]
        log.info("Tables: %s", ", ".join(names))

        expected = {
            "brain_docs",
            "briefings",
            "conversations",
            "cost_ledger",
            "entities",
            "entity_aliases",
            "follow_ups",
            "jobs",
            "messages",
            "notes",
            "schema_versions",
            "vec_docs",
        }
        missing = expected - set(names)
        if missing:
            log.error("Missing expected tables: %s", sorted(missing))
            return 3
        log.info("All %d expected tables present. Phase 1 OK.", len(expected))
    finally:
        await db.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
