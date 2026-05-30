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


async def _cmd_vault_backfill() -> int:
    """Walk the DB and emit every note + briefing + follow-up list to the vault."""
    from tars.integrations.vault import backfill_from_db

    log.info("TARS %s vault-backfill", __version__)
    cfg = load_config()
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        summary = await backfill_from_db(db, cfg)
        log.info("vault backfill: %s", summary)
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
        elif name == "cooldown_clear":
            from tars.scheduler.cooldown_clear import cooldown_clear
            summary = await cooldown_clear(db, cfg)
        elif name == "cost_rollup_daily":
            from tars.scheduler.cost_rollup_daily import cost_rollup_daily
            summary = await cost_rollup_daily(db, cfg)
        elif name == "vault_sweep":
            from tars.scheduler.vault_sweep import vault_sweep
            summary = await vault_sweep(db, cfg)
        elif name == "news_sources_refresh":
            from tars.scheduler.news_sources_refresh import news_sources_refresh
            summary = await news_sources_refresh(db, cfg)
        elif name == "competitive_intel_scan":
            from tars.scheduler.competitive_intel_scan import competitive_intel_scan
            summary = await competitive_intel_scan(agent, db, cfg)
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
    from tars.dashboard.app import run_dashboard
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

        bot_task = asyncio.create_task(run_bot(agent, cfg), name="bot")
        dash_task = asyncio.create_task(run_dashboard(db, cfg), name="dashboard")
        stop_task = asyncio.create_task(stop.wait(), name="stop")
        done, pending = await asyncio.wait(
            {bot_task, dash_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                log.exception("%s task crashed", t.get_name(), exc_info=exc)
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
    sub.add_parser("vault-backfill", help="Emit all notes/briefings/follow-ups as markdown files under cfg.paths.vault.")
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
            "cooldown_clear",
            "cost_rollup_daily",
            "vault_sweep",
            "news_sources_refresh",
            "competitive_intel_scan",
        ],
    )

    pfeeds = sub.add_parser("feeds", help="Manage RSS feeds (list / add).")
    fsub = pfeeds.add_subparsers(dest="feeds_cmd")
    fsub.add_parser("list", help="List all feeds")
    padd = fsub.add_parser("add", help="Add a feed")
    padd.add_argument("--name", required=True)
    padd.add_argument("--url", required=True)
    padd.add_argument("--kind", default="news", choices=["news", "competitive"])
    padd.add_argument("--notes", default=None)

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
    if args.cmd == "vault-backfill":
        return asyncio.run(_cmd_vault_backfill())
    if args.cmd == "briefing":
        return asyncio.run(_cmd_briefing())
    if args.cmd == "job":
        return asyncio.run(_cmd_job(args.name))
    if args.cmd == "feeds":
        return asyncio.run(_cmd_feeds(args))
    return 1


async def _cmd_feeds(args) -> int:
    from tars.integrations.news import add_feed, list_feeds
    cfg = load_config()
    db = await Database.connect(cfg.paths.db)
    try:
        await db.migrate()
        if args.feeds_cmd == "list":
            for f in await list_feeds(db, enabled_only=False):
                print(
                    f"#{f['id']:3d} [{f['kind']:11s}] "
                    f"{'on ' if f['enabled'] else 'off'} "
                    f"{f['name']!r:<30} {f['feed_url']}"
                )
            return 0
        if args.feeds_cmd == "add":
            fid = await add_feed(db, args.name, args.url, kind=args.kind, notes=args.notes)
            print(f"added feed #{fid}: {args.name} ({args.kind})")
            return 0
        print("usage: tars feeds {list|add ...}")
        return 1
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(main())
