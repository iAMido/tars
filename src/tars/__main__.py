"""TARS entrypoint.

Phase 1: connect to SQLite, run migrations, log status, exit clean.
Phase 2: add `chat` subcommand for one-shot LLM calls from the shell.
Phase 3+ will add aiogram + APScheduler + FastAPI as long-running tasks.

Usage:
    uv run python -m tars                       # boot+migrate sanity check
    uv run python -m tars chat "hello"          # one-shot chat at interactive_fast tier
    uv run python -m tars chat -t cron_default "summarize my day"
"""

from __future__ import annotations

import argparse
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


async def _cmd_check() -> int:
    """Phase 1 boot + migrate + table inventory."""
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

        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        )
        names = [r["name"] for r in rows]
        log.info("Tables: %s", ", ".join(names))

        expected = {
            "brain_docs", "briefings", "conversations", "cost_ledger",
            "entities", "entity_aliases", "follow_ups", "jobs",
            "messages", "notes", "schema_versions", "vec_docs",
        }
        missing = expected - set(names)
        if missing:
            log.error("Missing expected tables: %s", sorted(missing))
            return 3
        log.info("All %d expected tables present. Phase 1 OK.", len(expected))
    finally:
        await db.close()
    return 0


async def _cmd_chat(text: str, tier: str, thread: str) -> int:
    """Phase 2 one-shot chat. Convenience for testing the router + agent."""
    from tars.agent import Agent

    log.info("TARS %s chat", __version__)
    cfg = load_config()
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        agent = Agent(db=db, cfg=cfg)
        out = await agent.chat(thread_key=thread, user_text=text, tier=tier)
        print()
        print("=" * 60)
        print(out["text"])
        print("=" * 60)
        print(
            f"model={out['model']} provider={out['provider']} "
            f"cached={out['cached_tokens']} cost=${out['cost_usd']:.6f} "
            f"steps={out['steps']}"
        )
        return 0
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tars")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", help="Boot + migrate + verify schema (Phase 1).")

    pchat = sub.add_parser("chat", help="One-shot chat call (Phase 2).")
    pchat.add_argument("text", help="Message to send.")
    pchat.add_argument(
        "-t", "--tier",
        default="interactive_fast",
        choices=["interactive_fast", "cron_default", "ingest", "web_research"],
    )
    pchat.add_argument("--thread", default="cli:smoke", help="Thread key (default: cli:smoke).")

    return p


def main() -> int:
    args = _build_parser().parse_args()
    if args.cmd is None or args.cmd == "check":
        return asyncio.run(_cmd_check())
    if args.cmd == "chat":
        return asyncio.run(_cmd_chat(args.text, args.tier, args.thread))
    return 1


if __name__ == "__main__":
    sys.exit(main())
