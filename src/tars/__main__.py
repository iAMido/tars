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


async def _cmd_reindex() -> int:
    """Phase 4: full reindex of brain_docs + vec_docs."""
    from tars.memory.embed import Embedder
    from tars.memory.index import reindex_brain_docs

    log.info("TARS %s reindex", __version__)
    cfg = load_config()
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        embedder = Embedder(api_key=cfg.voyage.api_key)
        summary = await reindex_brain_docs(db, embedder)
        log.info("reindex summary: %s", summary)
        return 0
    finally:
        await db.close()


async def _cmd_job(name: str) -> int:
    """Manually trigger any scheduler job. For testing."""
    from tars.agent import Agent

    log.info("TARS %s job (manual): %s", __version__, name)
    cfg = load_config()
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        agent = Agent(db=db, cfg=cfg)

        if name == "morning_briefing":
            from tars.scheduler.morning_briefing import morning_briefing
            summary = await morning_briefing(agent, db, cfg)
        elif name == "email_summary":
            from tars.scheduler.email_summary import email_summary
            summary = await email_summary(agent, db, cfg)
        elif name == "calendar_pull":
            from tars.scheduler.calendar_pull import calendar_pull
            summary = await calendar_pull(db, cfg)
        elif name == "brain_reindex":
            from tars.scheduler.brain_reindex import brain_reindex
            summary = await brain_reindex(db, cfg)
        elif name == "weekly_followup_reconcile":
            from tars.scheduler.weekly_followup_reconcile import weekly_followup_reconcile
            summary = await weekly_followup_reconcile(db, cfg)
        else:
            log.error("unknown job: %s", name)
            return 2
        log.info("job %s summary: %s", name, summary)
        return 0
    finally:
        await db.close()


# Back-compat alias so 'tars briefing' still works.
async def _cmd_briefing() -> int:
    return await _cmd_job("morning_briefing")


async def _cmd_bot() -> int:
    """Phase 3+: long-running Telegram bot + APScheduler.

    Ctrl+C stops cleanly. SIGTERM (systemd) does the same.
    Phase 4: one-shot reindex on startup so search_memory works against current data.
    Phase 6: AsyncIOScheduler shares the bot's event loop, persistent jobstore
    in the same SQLite file. Missed jobs are picked up after restart within
    misfire_grace_time."""
    import signal

    from tars.agent import Agent
    from tars.bot.handlers import run_bot
    from tars.memory.embed import Embedder
    from tars.memory.index import reindex_brain_docs
    from tars.scheduler.jobs import build_scheduler

    log.info("TARS %s bot", __version__)
    cfg = load_config()
    if not cfg.telegram.allowed_chat_ids:
        log.warning(
            "telegram.allowed_chat_ids is EMPTY. Nobody can talk to the bot "
            "except via /whoami. Add your chat_id to ~/.tars/config.toml."
        )
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        agent = Agent(db=db, cfg=cfg)

        # One-shot reindex on startup. Phase 6 will move this to APScheduler.
        try:
            summary = await reindex_brain_docs(db, Embedder(api_key=cfg.voyage.api_key))
            log.info("startup reindex: %s", summary)
        except Exception as e:  # noqa: BLE001
            log.exception("startup reindex failed (%s); search_memory will still work but may be stale", e)

        # Build + start APScheduler on the SAME event loop as aiogram + FastAPI.
        # Persistent jobstore so missed jobs recover after restart.
        sched = build_scheduler(agent, db, cfg)
        sched.start()
        log.info(
            "scheduler started with %d job(s): %s",
            len(sched.get_jobs()),
            [j.id for j in sched.get_jobs()],
        )

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        try:
            loop.add_signal_handler(signal.SIGINT, stop.set)
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            # add_signal_handler is unimplemented on Windows; KeyboardInterrupt
            # still cancels the task, which is fine for dev.
            pass

        bot_task = asyncio.create_task(run_bot(agent, cfg))
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                log.exception("Bot task crashed", exc_info=exc)
                return 1
        # Stop scheduler before closing DB so any in-flight job completes first.
        sched.shutdown(wait=False)
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

    sub.add_parser("bot", help="Run the Telegram bot (long polling). Ctrl+C to stop.")
    sub.add_parser("reindex", help="Rebuild FTS5 + vec0 indices from notes/messages/briefings.")
    sub.add_parser("briefing", help="Manually run the morning briefing once (alias for `job morning_briefing`).")

    pjob = sub.add_parser("job", help="Manually trigger a scheduler job by name.")
    pjob.add_argument(
        "name",
        choices=[
            "morning_briefing",
            "email_summary",
            "calendar_pull",
            "brain_reindex",
            "weekly_followup_reconcile",
        ],
    )

    return p


def main() -> int:
    args = _build_parser().parse_args()
    if args.cmd is None or args.cmd == "check":
        return asyncio.run(_cmd_check())
    if args.cmd == "chat":
        return asyncio.run(_cmd_chat(args.text, args.tier, args.thread))
    if args.cmd == "bot":
        return asyncio.run(_cmd_bot())
    if args.cmd == "reindex":
        return asyncio.run(_cmd_reindex())
    if args.cmd == "briefing":
        return asyncio.run(_cmd_briefing())
    if args.cmd == "job":
        return asyncio.run(_cmd_job(args.name))
    return 1


if __name__ == "__main__":
    sys.exit(main())
