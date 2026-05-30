"""Telegram bot — aiogram 3 dispatcher.

Handlers:
  /start           : presence ping
  /whoami          : prints chat_id (works even for unauthorized users — debug)
  /voice on|off    : voice toggle (placeholder — real voice in V1.1)
  /research <q>    : Agent.chat at web_research tier
  /tier            : prints current tier defaults
  note: <body>     : direct save_note, no LLM call (cheap, instant)
  <free text>      : Agent.chat at interactive_fast tier

Authorization:
  All handlers except /whoami are gated by an aiogram BaseFilter on
  cfg.telegram.allowed_chat_ids. Unauthorized messages get dropped silently.
  /whoami responds to anyone so you can recover if Telegram swaps your chat_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message

from tars.agent import Agent
from tars.bot.actions import handle_callback as handle_action_callback
from tars.config import Config
from tars.tools import save_note as tool_save_note

log = logging.getLogger("tars.bot")

TELEGRAM_MSG_LIMIT = 4000  # actual limit is 4096; leave headroom for prefixes


# ---------------------------------------------------------------------------
# Authorization filter
# ---------------------------------------------------------------------------


class AuthFilter(BaseFilter):
    """Drop messages from chat_ids not in the allowlist."""

    def __init__(self, allowed: list[int]) -> None:
        self.allowed: set[int] = set(allowed)

    async def __call__(self, m: Message) -> bool:
        ok = m.chat.id in self.allowed
        if not ok:
            log.warning(
                "Dropped message from unauthorized chat_id=%s text=%r",
                m.chat.id,
                (m.text or "")[:50],
            )
        return ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_long(bot: Bot, chat_id: int, text: str) -> None:
    """Send long text in chunks under Telegram's 4096-char per-message limit."""
    if not text:
        text = "(empty response)"
    while text:
        chunk, text = text[:TELEGRAM_MSG_LIMIT], text[TELEGRAM_MSG_LIMIT:]
        await bot.send_message(chat_id, chunk)


async def _typing_until(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    """Show 'typing...' until the stop event is set. Telegram resets the
    indicator every ~5s, so we refresh every 4s while a slow LLM call runs."""
    try:
        while not stop.is_set():
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue
    except Exception:  # noqa: BLE001
        # never let the typing task crash the handler
        log.exception("typing-indicator task crashed")


async def _with_typing(bot: Bot, chat_id: int, coro):
    """Run an awaitable while keeping the 'typing...' indicator alive."""
    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_until(bot, chat_id, stop))
    try:
        return await coro
    finally:
        stop.set()
        await asyncio.gather(typing_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Dispatcher builder
# ---------------------------------------------------------------------------


def build_dispatcher(agent: Agent, cfg: Config) -> tuple[Dispatcher, Bot]:
    bot = Bot(token=cfg.telegram.bot_token)
    dp = Dispatcher()

    # /whoami is special — it responds to ANYONE so you can recover your
    # chat_id if Telegram ever changes it on you. Register it BEFORE the
    # auth filter is applied.
    whoami_router = dp

    @whoami_router.message(Command("whoami"))
    async def _whoami(m: Message) -> None:
        await m.answer(
            f"chat_id: <code>{m.chat.id}</code>\n"
            f"user_id: <code>{m.from_user.id if m.from_user else 'n/a'}</code>\n"
            f"username: @{m.from_user.username if m.from_user else 'n/a'}",
            parse_mode="HTML",
        )

    # Everything below this is auth-gated.
    auth = AuthFilter(cfg.telegram.allowed_chat_ids)

    @dp.message(CommandStart(), auth)
    async def _start(m: Message) -> None:
        await m.answer("TARS online.")

    @dp.message(Command("voice"), auth)
    async def _voice_toggle(m: Message) -> None:
        # V1.1 will wire this to a per-thread setting; for now it's stub.
        await m.answer("Voice control is queued for V1.1. Text-only for now.")

    @dp.message(Command("feeds"), auth)
    async def _feeds(m: Message) -> None:
        """Manage RSS feeds from Telegram.

        Usage:
          /feeds                                  → list all feeds
          /feeds add news <name> <url>            → add a news feed
          /feeds add competitive <name> <url>     → add a competitive feed
          /feeds remove <id>                      → hard-delete a feed
          /feeds disable <id>                     → keep but stop fetching
          /feeds enable <id>                      → re-enable
        Names with spaces: wrap in double quotes.
        """
        import shlex
        from tars.integrations.news import (
            add_feed, list_feeds, refresh_feed, remove_feed, set_feed_enabled,
        )

        text = (m.text or "").removeprefix("/feeds").strip()
        try:
            parts = shlex.split(text) if text else []
        except ValueError as e:
            await m.answer(f"parse error: {e}")
            return

        # default: list
        if not parts:
            feeds = await list_feeds(agent.db, enabled_only=False)
            if not feeds:
                await m.answer("No feeds yet. Add one:\n`/feeds add news \"Hacker News\" https://news.ycombinator.com/rss`", parse_mode="Markdown")
                return
            lines = ["*Feeds*"]
            for f in feeds:
                status = "✓" if f["enabled"] else "✗"
                lines.append(
                    f"`#{f['id']}` {status} [{f['kind']}] *{f['name']}*\n   {f['feed_url']}"
                )
            lines.append("\n`/feeds add news|competitive <name> <url>`\n`/feeds remove|enable|disable <id>`")
            await m.answer("\n".join(lines), parse_mode="Markdown",
                           disable_web_page_preview=True)
            return

        cmd = parts[0].lower()

        if cmd == "add":
            if len(parts) < 4 or parts[1] not in ("news", "competitive"):
                await m.answer(
                    "Usage: `/feeds add news|competitive \"<name>\" <url>`",
                    parse_mode="Markdown",
                )
                return
            kind = parts[1]
            name = parts[2]
            url = parts[3]
            fid = await add_feed(agent.db, name=name, feed_url=url, kind=kind)
            # Try one immediate refresh so the user sees if the URL works.
            try:
                feed_row = await agent.db.fetch_one(
                    "SELECT id, name, feed_url, last_seen_guid FROM feeds WHERE id = ?",
                    (fid,),
                )
                new = await refresh_feed(agent.db, dict(feed_row))
                await m.answer(
                    f"Added feed `#{fid}` [{kind}] *{name}*.\nFirst fetch: {len(new)} items.",
                    parse_mode="Markdown",
                )
            except Exception as e:  # noqa: BLE001
                await m.answer(
                    f"Added feed `#{fid}` but first fetch failed: `{e}`\n"
                    f"Check the URL — leave it enabled to retry on the next schedule.",
                    parse_mode="Markdown",
                )
            return

        if cmd in ("remove", "delete", "rm"):
            if len(parts) < 2 or not parts[1].isdigit():
                await m.answer("Usage: `/feeds remove <id>`", parse_mode="Markdown")
                return
            ok = await remove_feed(agent.db, int(parts[1]))
            await m.answer(f"{'Removed' if ok else 'Not found'} feed `#{parts[1]}`.",
                           parse_mode="Markdown")
            return

        if cmd in ("enable", "disable"):
            if len(parts) < 2 or not parts[1].isdigit():
                await m.answer(f"Usage: `/feeds {cmd} <id>`", parse_mode="Markdown")
                return
            ok = await set_feed_enabled(agent.db, int(parts[1]), cmd == "enable")
            await m.answer(
                f"{'Enabled' if cmd == 'enable' else 'Disabled'} feed `#{parts[1]}` "
                f"{'(was missing)' if not ok else ''}".strip(),
                parse_mode="Markdown",
            )
            return

        await m.answer(
            "Commands: `/feeds`, `/feeds add news|competitive \"<name>\" <url>`, "
            "`/feeds remove|enable|disable <id>`",
            parse_mode="Markdown",
        )

    @dp.message(Command("clear"), auth)
    async def _clear(m: Message) -> None:
        """Wipe conversation history for this chat (but keep notes, follow-ups, ledger)."""
        thread_key = f"tg:{m.chat.id}"
        await agent.db.execute(
            "DELETE FROM messages WHERE thread_key = ?", (thread_key,)
        )
        await m.answer("Conversation cleared. Notes and follow-ups preserved.")

    @dp.message(Command("stats"), auth)
    async def _stats(m: Message) -> None:
        """One-shot snapshot: recent cost, notes, open follow-ups, scheduled jobs."""
        import time as _time
        db = agent.db

        now = int(_time.time())
        today_start = now - (now % 86400)

        # Costs
        row = await db.fetch_one(
            "SELECT ROUND(SUM(cost_usd),6) AS c, COUNT(*) AS n FROM cost_ledger WHERE ts >= ?",
            (now - 7 * 86400,),
        )
        cost_7d = row["c"] if row and row["c"] else 0.0
        calls_7d = row["n"] if row else 0
        row = await db.fetch_one(
            "SELECT ROUND(SUM(cost_usd),6) AS c, COUNT(*) AS n FROM cost_ledger WHERE ts >= ?",
            (today_start,),
        )
        cost_today = row["c"] if row and row["c"] else 0.0
        calls_today = row["n"] if row else 0

        # Counts
        n_notes = (await db.fetch_one("SELECT COUNT(*) AS n FROM notes"))["n"]
        n_open_fu = (await db.fetch_one(
            "SELECT COUNT(*) AS n FROM follow_ups WHERE status='open'"
        ))["n"]
        n_entities = (await db.fetch_one("SELECT COUNT(*) AS n FROM entities"))["n"]

        # Scheduled jobs
        try:
            jobs = await db.fetch_all(
                "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time"
            )
            jobs_lines = []
            for j in jobs[:5]:
                ts = float(j["next_run_time"]) if j["next_run_time"] else None
                if ts:
                    delta = int(ts - now)
                    if delta < 0:
                        when = "overdue"
                    elif delta < 3600:
                        when = f"in {delta // 60}m"
                    elif delta < 86400:
                        when = f"in {delta // 3600}h"
                    else:
                        when = f"in {delta // 86400}d"
                else:
                    when = "?"
                jobs_lines.append(f"- {j['id']}: {when}")
            jobs_text = "\n".join(jobs_lines) if jobs_lines else "no jobs"
        except Exception:  # noqa: BLE001
            jobs_text = "scheduler offline"

        text = (
            f"Today: ${cost_today:.4f} / {calls_today} calls\n"
            f"7d:    ${cost_7d:.4f} / {calls_7d} calls\n"
            f"Notes: {n_notes}, open follow-ups: {n_open_fu}, entities: {n_entities}\n"
            f"\nNext jobs:\n{jobs_text}"
        )
        await m.answer(text)

    @dp.message(Command("tier"), auth)
    async def _tier_info(m: Message) -> None:
        t = cfg.tiers
        await m.answer(
            "Current tier mapping:\n"
            f"  interactive_fast = {t.interactive_fast}\n"
            f"  cron_default     = {t.cron_default}\n"
            f"  ingest           = {t.ingest}\n"
            f"  web_research     = {t.web_research}"
        )

    @dp.message(Command("research"), auth)
    async def _research(m: Message) -> None:
        text = (m.text or "").removeprefix("/research").strip()
        if not text:
            await m.answer("Usage: /research <question>")
            return
        thread_key = f"tg:{m.chat.id}"
        try:
            out = await _with_typing(
                bot,
                m.chat.id,
                # web_research can need several tool iterations; override the
                # interactive default of 2.
                agent.chat(
                    thread_key=thread_key,
                    user_text=text,
                    tier="web_research",
                    tool_loop_max=6,
                ),
            )
            await _send_long(bot, m.chat.id, out["text"])
        except Exception as e:  # noqa: BLE001
            log.exception("research failed")
            await m.answer(f"Research failed: {e}")

    @dp.message(F.text.regexp(r"(?is)^\s*note\s*:\s*(.+)"), auth)
    async def _take_note(m: Message) -> None:
        # Direct save_note — no LLM, no cost.
        body = (m.text or "").split(":", 1)[1].strip()
        if not body:
            await m.answer("Empty note. Try: note: bought milk")
            return
        result = await tool_save_note(agent.db, {"body": body, "tags": ["telegram"]})
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = {}
        note_id = payload.get("note_id")
        if note_id:
            await m.answer(f"Noted. [note:{note_id}]")
        else:
            await m.answer(f"Note save error: {result}")

    @dp.message(F.text, auth)
    async def _free_chat(m: Message) -> None:
        thread_key = f"tg:{m.chat.id}"
        try:
            out = await _with_typing(
                bot,
                m.chat.id,
                agent.chat(thread_key=thread_key, user_text=m.text or "", tier="interactive_fast"),
            )
            await _send_long(bot, m.chat.id, out["text"])
            log.info(
                "tg chat done chat_id=%s tokens=%d/%d cached=%d cost=$%.6f steps=%d model=%s",
                m.chat.id,
                0,  # not exposing per-call tokens in this hot path (see cost_ledger)
                0,
                out["cached_tokens"],
                out["cost_usd"],
                out["steps"],
                out["model"],
            )
        except Exception as e:  # noqa: BLE001
            log.exception("free chat failed")
            await m.answer(f"Failed: {e}")

    # Inline-keyboard callback handler — must be gated to your chat_id too.
    @dp.callback_query(F.data.startswith("b:"))
    async def _action_cb(cq: CallbackQuery) -> None:
        if cq.from_user is None or cq.from_user.id not in {
            uid for uid in cfg.telegram.allowed_chat_ids
        }:
            await cq.answer("not authorized")
            return
        await handle_action_callback(cq, bot, agent, cfg)

    return dp, bot


# ---------------------------------------------------------------------------
# Long-running entry point — used by `python -m tars bot`.
# ---------------------------------------------------------------------------


async def run_bot(agent: Agent, cfg: Config) -> None:
    dp, bot = build_dispatcher(agent, cfg)
    log.info("Bot starting (long polling). Allowed chat_ids=%s", cfg.telegram.allowed_chat_ids)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Bot stopped.")
