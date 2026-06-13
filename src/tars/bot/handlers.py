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
from tars.bot.actions import (
    handle_callback as handle_action_callback,
    handle_custom_remind_reply,
    handle_followup_nudge_callback,
    handle_followup_snooze_reply,
    handle_midday_todo_callback,
    handle_midday_todo_eta_reply,
    handle_triage_callback,
    handle_triage_folder_reply,
)
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

    @dp.message(Command("help"), auth)
    async def _help(m: Message) -> None:
        await m.answer(
            "*TARS commands*\n"
            "\n"
            "*Reading*\n"
            "/notes [N] — last N notes (default 10)\n"
            "/followups — open follow-ups with action buttons\n"
            "/todos [folder] — open `- [ ]` items\n"
            "/triage — un-promoted notes worth filing\n"
            "/stats — costs, counts, next jobs\n"
            "/feeds — RSS feeds (see /feeds for sub-commands)\n"
            "\n"
            "*Writing (fast-path, no LLM)*\n"
            "`note: <body>` — instant save (also: `add note`, `הערה:`, etc.)\n"
            "/promote <note\\_id> <folder> — file a note into PARA\n"
            "/delete <note\\_id> — soft-delete a TARS note\n"
            "/done <followup\\_id> — close a follow-up\n"
            "\n"
            "*Manual triggers*\n"
            "/briefing — fire morning briefing now\n"
            "/midday — fire midday check-in now\n"
            "/evening — fire evening wrap-up now\n"
            "\n"
            "*Misc*\n"
            "/research <q> — web-RAG search (gpt-5:online)\n"
            "/tier — current tier→model mapping\n"
            "/clear — wipe my conversation memory\n"
            "/whoami — debug: show your chat_id\n"
            "\n"
            "Or just type — I'll route to tools.",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    @dp.message(Command("notes"), auth)
    async def _notes(m: Message) -> None:
        from tars.tools import list_notes as _list_notes
        text = (m.text or "").removeprefix("/notes").strip()
        try:
            limit = int(text) if text else 10
        except ValueError:
            limit = 10
        raw = await _list_notes(agent.db, {"limit": limit})
        payload = json.loads(raw)
        items = payload.get("notes") or []
        if not items:
            await m.answer("No notes yet.")
            return
        lines = [f"*Last {len(items)} notes*"]
        for n in items:
            lines.append(f"`[note:{n['id']}]` {n.get('preview','')[:120]}")
        await m.answer(
            "\n".join(lines), parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    @dp.message(Command("followups"), auth)
    async def _followups(m: Message) -> None:
        from aiogram.types import InlineKeyboardMarkup
        from tars.bot.actions import build_followups_briefing_rows
        from tars.memory.follow_ups import list_open as _list_open
        opens = await _list_open(agent.db, limit=10)
        if not opens:
            await m.answer("No open follow-ups.")
            return
        lines = ["*Open follow-ups*"]
        ids: list[int] = []
        for f in opens:
            due_ts = f.get("due_at")
            if due_ts:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _Zi
                due = _dt.fromtimestamp(due_ts, tz=_Zi(cfg.timezone)).strftime("%a %m-%d %H:%M")
            else:
                due = "no due"
            body = (f.get("body") or "")[:100]
            lines.append(f"`[followup:{f['followup_id']}]` {body} ({due})")
            ids.append(int(f["followup_id"]))
        kb = InlineKeyboardMarkup(
            inline_keyboard=build_followups_briefing_rows(ids[:5])
        )
        await m.answer(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=kb, disable_web_page_preview=True,
        )

    @dp.message(Command("todos"), auth)
    async def _todos(m: Message) -> None:
        from tars.tools import list_open_todos as _todos_tool
        text = (m.text or "").removeprefix("/todos").strip()
        args = {"max_per_file": 8, "max_total": 30}
        if text:
            args["folder"] = text
        raw = await _todos_tool(agent.db, args)
        p = json.loads(raw)
        files = p.get("files") or []
        if not files:
            await m.answer("No open todos found.")
            return
        lines = [f"*Open todos* ({p.get('total_open',0)} total)"]
        for f in files[:5]:
            lines.append(f"\n*{f['path']}* ({f['open_count']} open)")
            for item in f["items"][:5]:
                lines.append(f"- {item[:120]}")
        await m.answer(
            "\n".join(lines), parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    @dp.message(Command("triage"), auth)
    async def _triage(m: Message) -> None:
        from aiogram.types import InlineKeyboardMarkup
        from tars.bot.actions import build_triage_rows, create_triage_pendings
        from tars.tools import suggest_promotions as _suggest
        raw = await _suggest(agent.db, {"since_days": 30, "limit": 5, "min_score": 3})
        p = json.loads(raw)
        suggs = p.get("suggestions") or []
        if not suggs:
            await m.answer("Nothing worth triaging right now.")
            return
        lines = ["*Triage — promote or skip*"]
        for i, s in enumerate(suggs, 1):
            lines.append(
                f"{i}. `[note:{s['id']}]` (score {s['score']}/10) "
                f"{s.get('preview','')[:120]}"
            )
        pending_ids = await create_triage_pendings(
            agent.db, chat_id=m.chat.id, suggestions=suggs,
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=build_triage_rows(pending_ids)
        )
        await m.answer(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=kb, disable_web_page_preview=True,
        )

    @dp.message(Command("delete"), auth)
    async def _delete(m: Message) -> None:
        """`/delete <note_id>` — soft-delete a TARS note (DB + vault file)."""
        from tars.tools import delete_note as _delete_note
        raw = (m.text or "").removeprefix("/delete").strip()
        if not raw.isdigit():
            await m.answer(
                "Usage: `/delete <note_id>`\nExample: `/delete 113`",
                parse_mode="Markdown",
            )
            return
        nid = int(raw)
        out = await _delete_note(agent.db, {"note_id": nid})
        payload = json.loads(out)
        if payload.get("ok"):
            extra = (
                " (file removed)" if payload.get("file_removed")
                else " (DB only — no vault file)"
            )
            if payload.get("already_deleted"):
                await m.answer(f"`[note:{nid}]` was already deleted.", parse_mode="Markdown")
            else:
                await m.answer(
                    f"🗑 Deleted `[note:{nid}]`{extra}",
                    parse_mode="Markdown",
                )
        else:
            await m.answer(f"Delete failed: {payload.get('error','unknown')}")

    @dp.message(Command("promote"), auth)
    async def _promote(m: Message) -> None:
        """`/promote <note_id> <dest_folder>` — direct call to promote_note,
        no LLM. Example: `/promote 45 01_Projects/Work`."""
        from tars.tools import promote_note as _promote_note
        raw = (m.text or "").removeprefix("/promote").strip()
        # Accept tail after note_id as the folder, allowing spaces.
        parts = raw.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            await m.answer(
                "Usage: `/promote <note_id> <dest_folder>`\n"
                "Example: `/promote 45 01_Projects/Work`",
                parse_mode="Markdown",
            )
            return
        nid = int(parts[0])
        folder = parts[1].strip()
        out = await _promote_note(agent.db, {"note_id": nid, "dest_folder": folder})
        payload = json.loads(out)
        if payload.get("ok"):
            await m.answer(
                f"📌 Promoted `[note:{nid}]` → `{payload['path']}`",
                parse_mode="Markdown",
            )
        else:
            await m.answer(f"Promote failed: {payload.get('error','unknown')}")

    @dp.message(Command("done"), auth)
    async def _done(m: Message) -> None:
        """`/done <followup_id>` — close a follow-up with an auto-generated
        resolving note (same path as the ✅ Done button)."""
        from tars.bot.actions import _close_followup_with_synthetic_note
        raw = (m.text or "").removeprefix("/done").strip()
        if not raw.isdigit():
            await m.answer(
                "Usage: `/done <followup_id>`\nExample: `/done 5`",
                parse_mode="Markdown",
            )
            return
        fu_id = int(raw)
        row = await agent.db.fetch_one(
            "SELECT fu.note_id, fu.status, n.body "
            "FROM follow_ups fu JOIN notes n ON n.id = fu.note_id "
            "WHERE fu.id = ?",
            (fu_id,),
        )
        if row is None:
            await m.answer(f"Follow-up #{fu_id} does not exist.")
            return
        if row["status"] != "open":
            await m.answer(f"Follow-up #{fu_id} is already {row['status']}.")
            return
        try:
            resolving = await _close_followup_with_synthetic_note(
                agent.db, fu_id, row["body"] or "", cfg.timezone,
            )
            await m.answer(
                f"✅ Closed `[followup:{fu_id}]`. Resolving note "
                f"`[note:{resolving}]`.",
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            await m.answer(f"Close failed: {e}")

    @dp.message(Command("briefing"), auth)
    async def _briefing(m: Message) -> None:
        from tars.scheduler.morning_briefing import morning_briefing as _job
        await m.answer("Firing morning briefing… (this takes ~30s)")
        try:
            r = await _job(agent, agent.db, cfg)
            await m.answer(
                f"✓ briefing sent. cost ${r.get('cost_usd',0):.4f} "
                f"emails={r.get('emails',0)} cal={r.get('calendar',0)} "
                f"followups={r.get('followups',0)}"
            )
        except Exception as e:  # noqa: BLE001
            await m.answer(f"Briefing failed: {e}")

    @dp.message(Command("midday"), auth)
    async def _midday(m: Message) -> None:
        from tars.scheduler.midday_checkin import midday_checkin as _job
        await m.answer("Firing midday check-in…")
        try:
            r = await _job(agent, agent.db, cfg)
            await m.answer(f"✓ midday sent. cost ${r.get('cost_usd',0):.4f}")
        except Exception as e:  # noqa: BLE001
            await m.answer(f"Midday failed: {e}")

    @dp.message(Command("evening"), auth)
    async def _evening(m: Message) -> None:
        from tars.scheduler.evening_wrapup import evening_wrapup as _job
        await m.answer("Firing evening wrap-up…")
        try:
            r = await _job(agent, agent.db, cfg)
            if r.get("skipped"):
                await m.answer("Nothing to wrap up.")
            else:
                await m.answer(f"✓ evening sent. cost ${r.get('cost_usd',0):.4f}")
        except Exception as e:  # noqa: BLE001
            await m.answer(f"Evening failed: {e}")

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

    # Direct-save prefixes — bypass the LLM entirely.
    # Matches: "note:", "add note:", "take note:", "new note:", "note this:",
    # plus Hebrew "הערה:", "רשום:", "הוסף הערה:". Separator after prefix
    # word is colon OR whitespace ("add note <body>" works without colon —
    # user kept hitting this and the LLM was hallucinating "Noted." for it).
    # The boundary (\b/whitespace before "note") guards against "notes are
    # stupid" / "notepad" type false positives.
    NOTE_PREFIX_RE = (
        r"(?is)^\s*(?:"
        r"(?:add|take|new)\s+note"
        r"|note(?:\s+this)?"
        r"|הוסף\s+הערה"
        r"|הערה"
        r"|רשום"
        r")[\s:]+(.+)"
    )

    @dp.message(F.text.regexp(NOTE_PREFIX_RE), auth)
    async def _take_note(m: Message) -> None:
        # Direct save_note — no LLM, no cost, no hallucination possible.
        import re as _re
        m_match = _re.match(NOTE_PREFIX_RE, m.text or "")
        body = (m_match.group(1) if m_match else "").strip()
        if not body:
            await m.answer("Empty note. Try: add note bought milk")
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
        # Hand-off check first: is this a reply to a "When?" prompt we sent?
        try:
            if await handle_custom_remind_reply(m, agent, cfg):
                return
        except Exception as e:  # noqa: BLE001
            log.exception("custom-remind reply handler failed (%s); falling through", e)

        # And a reply to a follow-up snooze "Snooze until when?" prompt.
        try:
            if await handle_followup_snooze_reply(m, agent, cfg):
                return
        except Exception as e:  # noqa: BLE001
            log.exception("followup-snooze reply handler failed (%s); falling through", e)

        # And a reply to a midday todo "ETA?" prompt.
        try:
            if await handle_midday_todo_eta_reply(m, agent, cfg):
                return
        except Exception as e:  # noqa: BLE001
            log.exception("midday-todo eta reply handler failed (%s); falling through", e)

        # And a reply to a triage "Which folder?" prompt.
        try:
            if await handle_triage_folder_reply(m, agent, cfg):
                return
        except Exception as e:  # noqa: BLE001
            log.exception("triage folder reply handler failed (%s); falling through", e)

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

    # Follow-up reminder nudge callbacks (✅ Done / ⏰ +1h / Tomorrow / Custom).
    @dp.callback_query(F.data.startswith("fu:"))
    async def _followup_cb(cq: CallbackQuery) -> None:
        if cq.from_user is None or cq.from_user.id not in {
            uid for uid in cfg.telegram.allowed_chat_ids
        }:
            await cq.answer("not authorized")
            return
        await handle_followup_nudge_callback(cq, bot, agent, cfg)

    # Midday review-todo callbacks (⏰ ETA / 📌 Followup / ✖ Skip).
    @dp.callback_query(F.data.startswith("mt:"))
    async def _midday_todo_cb(cq: CallbackQuery) -> None:
        if cq.from_user is None or cq.from_user.id not in {
            uid for uid in cfg.telegram.allowed_chat_ids
        }:
            await cq.answer("not authorized")
            return
        await handle_midday_todo_callback(cq, bot, agent, cfg)

    # Morning *Triage* section callbacks (📌 Promote / ✖ Skip).
    @dp.callback_query(F.data.startswith("tp:"))
    async def _triage_cb(cq: CallbackQuery) -> None:
        if cq.from_user is None or cq.from_user.id not in {
            uid for uid in cfg.telegram.allowed_chat_ids
        }:
            await cq.answer("not authorized")
            return
        await handle_triage_callback(cq, bot, agent, cfg)

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
