"""Pending-action callback handling for briefing suggestion buttons.

Each suggestion in a morning briefing gets a row in `pending_actions` and an
inline keyboard row of 4 buttons. Tapping a button dispatches here.

Callback data format (≤64 bytes per Telegram limit):
    b:<action>:<pending_action_id>

Actions:
    s   → save as note (tags include briefing date hashtag)
    r1  → save as note + open follow-up due tomorrow 09:00 local
    r7  → save as note + open follow-up due 7d from now 09:00 local
    x   → dismiss (no action; mark consumed)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from tars.memory.follow_ups import close_followup, open_followup
from tars.tools import save_note as tool_save_note

log = logging.getLogger("tars.bot.actions")


def build_suggestion_keyboard(suggestion_ids: Sequence[int]) -> InlineKeyboardMarkup:
    """One row of 4 buttons per suggestion id, in input order.

    Actions:
      📝 Note     → save as note (no reminder)
      ⏰ Tomorrow → save + reminder tomorrow 09:00 local
      ⏰ Custom   → bot asks "When?", user replies with free-text time
      ✖          → dismiss
    """
    rows: list[list[InlineKeyboardButton]] = []
    for sid in suggestion_ids:
        rows.append([
            InlineKeyboardButton(text="📝 Note",     callback_data=f"b:s:{sid}"),
            InlineKeyboardButton(text="⏰ Tomorrow", callback_data=f"b:r1:{sid}"),
            InlineKeyboardButton(text="⏰ Custom",   callback_data=f"b:rc:{sid}"),
            InlineKeyboardButton(text="✖",          callback_data=f"b:x:{sid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def create_pending(
    db, chat_id: int, suggestions: list[dict], briefing_date: str,
) -> list[int]:
    """Insert a row per suggestion. Returns ids in input order."""
    ids: list[int] = []
    now = int(time.time())
    for s in suggestions:
        # Obsidian best-practice tags: hierarchical with /, no # prefix in
        # frontmatter. The inline #briefing/<date> in the body (from the LLM)
        # plus these frontmatter tags both render cleanly in Obsidian.
        extra = json.dumps({
            "briefing_date": briefing_date,
            "hashtag": f"#briefing/{briefing_date}",
            "frontmatter_tags": ["briefing", f"briefing/{briefing_date}", "source/suggestion"],
        })
        cur = await db.execute(
            "INSERT INTO pending_actions("
            " chat_id, kind, text, extra, created_at"
            ") VALUES (?, 'briefing_suggestion', ?, ?, ?)",
            (int(chat_id), s["text"], extra, now),
        )
        ids.append(int(cur.lastrowid or 0))
    return ids


# ---------------------------------------------------------------------------
# CallbackQuery handler — registered in handlers.py
# ---------------------------------------------------------------------------


async def handle_callback(callback, bot: Bot, agent, cfg) -> None:
    data = (callback.data or "").strip()
    if not data.startswith("b:"):
        await callback.answer("unknown action")
        return

    try:
        _, action, sid_str = data.split(":", 2)
        sid = int(sid_str)
    except (ValueError, IndexError):
        await callback.answer("bad data")
        return

    row = await agent.db.fetch_one(
        "SELECT id, chat_id, text, extra, consumed_at FROM pending_actions WHERE id = ?",
        (sid,),
    )
    if row is None:
        await callback.answer("already gone")
        return
    if row["consumed_at"] is not None:
        await callback.answer("already done")
        await _replace_row_with_status(callback, bot, sid, "already done")
        return

    text = row["text"]
    try:
        extra = json.loads(row["extra"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    # Obsidian-clean tags from the extra payload (no `#` prefix, hierarchical).
    tags = extra.get("frontmatter_tags") or ["briefing"]

    status_text = ""
    result_payload: dict = {}

    try:
        if action == "s":
            r = json.loads(await tool_save_note(agent.db, {"body": text, "tags": tags}))
            note_id = r.get("note_id")
            status_text = f"✓ saved [note:{note_id}]"
            result_payload = {"note_id": note_id}

        elif action == "r1":
            r = json.loads(await tool_save_note(agent.db, {"body": text, "tags": tags}))
            note_id = r.get("note_id")
            tz = ZoneInfo(cfg.timezone)
            due = (datetime.now(tz) + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0,
            )
            fu_id = await open_followup(
                agent.db, note_id=int(note_id),
                due_at_iso=due.isoformat(timespec="seconds"),
            )
            due_human = due.strftime("%a %Y-%m-%d %H:%M")
            status_text = f"✓ reminder set {due_human} [followup:{fu_id}]"
            result_payload = {"note_id": note_id, "followup_id": fu_id}

        elif action == "rc":
            # Custom-time reminder: send a ForceReply prompt — Telegram opens
            # the user's keyboard pre-set to reply to THIS message, so we can
            # cleanly detect the reply via message.reply_to_message.message_id.
            prompt = (
                f"⏰ When should I remind you about:\n_{text[:200]}_\n\n"
                f"Reply with a time — e.g. `in 2 hours`, `tomorrow 3pm`, "
                f"`friday 5pm`, `2026-06-15 14:00`."
            )
            sent = await bot.send_message(
                chat_id=callback.message.chat.id,
                text=prompt,
                parse_mode="Markdown",
                reply_markup=ForceReply(
                    input_field_placeholder="e.g. tomorrow 3pm",
                    selective=True,
                ),
            )
            await agent.db.execute(
                "UPDATE pending_actions SET awaiting_kind = ?, "
                " prompt_message_id = ? WHERE id = ?",
                ("custom_remind", sent.message_id, sid),
            )
            await callback.answer("Reply with the time")
            await _replace_row_with_status(callback, bot, sid, "⌛ awaiting time…")
            return  # do NOT mark consumed yet

        elif action == "x":
            status_text = "✖ dismissed"
            result_payload = {}

        else:
            status_text = f"unknown action {action!r}"

    except Exception as e:  # noqa: BLE001
        log.exception("pending_action %d action=%s failed", sid, action)
        status_text = f"error: {type(e).__name__}"
        result_payload = {"error": str(e)}

    # Mark consumed.
    await agent.db.execute(
        "UPDATE pending_actions SET consumed_at = ?, consumed_action = ?, "
        " consumed_result = ? WHERE id = ?",
        (int(time.time()), action, json.dumps(result_payload), sid),
    )

    # Edit the original message — replace the button row for this suggestion.
    await _replace_row_with_status(callback, bot, sid, status_text)
    await callback.answer(status_text)


# ---------------------------------------------------------------------------
# Reply handler — when user replies to a "When?" prompt
# ---------------------------------------------------------------------------


async def handle_custom_remind_reply(message, agent, cfg) -> bool:
    """If the user's message is a reply to one of our "When?" prompts, parse
    the time deterministically (dateparser) and create the follow-up directly.
    No LLM in the loop — LLM call had hallucination risk (would pull adjacent
    notes via search_memory and answer about the wrong subject).

    Returns True if handled (so the bot's free-chat handler skips it)."""
    if message.reply_to_message is None:
        return False
    prompt_msg_id = message.reply_to_message.message_id
    row = await agent.db.fetch_one(
        "SELECT id, text, extra FROM pending_actions "
        "WHERE awaiting_kind = 'custom_remind' AND prompt_message_id = ? "
        "  AND consumed_at IS NULL",
        (prompt_msg_id,),
    )
    if row is None:
        return False

    sid = int(row["id"])
    suggestion_text = row["text"]
    try:
        extra = json.loads(row["extra"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    tags = extra.get("frontmatter_tags") or ["briefing"]
    user_time = (message.text or "").strip()

    # Parse the time string deterministically.
    import dateparser  # local import to avoid top-level cost when unused
    tz = ZoneInfo(cfg.timezone)
    dt = dateparser.parse(
        user_time,
        settings={
            "TIMEZONE": cfg.timezone,
            "TO_TIMEZONE": cfg.timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if dt is None:
        await message.reply(
            f"Couldn't parse `{user_time}` as a time. Try `tomorrow 3pm`, "
            f"`in 2 hours`, `next Monday 9am`, or `2026-06-15 14:00`.\n"
            f"Reply to the same prompt to try again.",
            parse_mode="Markdown",
        )
        return True  # we did handle it (asked for retry)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    # Refuse past times (off by >5min — gives small tolerance for clock skew).
    now_dt = datetime.now(tz)
    if (now_dt - dt).total_seconds() > 300:
        await message.reply(
            f"`{user_time}` resolves to {dt.strftime('%a %Y-%m-%d %H:%M')} which is in the past. "
            f"Reply with a future time.",
            parse_mode="Markdown",
        )
        return True

    # Save the suggestion as a note (with the briefing frontmatter tags).
    r = json.loads(await tool_save_note(agent.db, {"body": suggestion_text, "tags": tags}))
    note_id = r.get("note_id")

    # Open the follow-up.
    try:
        fu_id = await open_followup(
            agent.db, note_id=int(note_id),
            due_at_iso=dt.isoformat(timespec="seconds"),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("custom_remind: open_followup failed (%s)", e)
        await message.reply(f"Couldn't set reminder: {e}")
        return True

    # Mark consumed.
    await agent.db.execute(
        "UPDATE pending_actions SET consumed_at = ?, consumed_action = ?, "
        " consumed_result = ? WHERE id = ?",
        (
            int(time.time()), "rc",
            json.dumps({
                "note_id": note_id, "followup_id": fu_id,
                "user_time": user_time, "resolved_iso": dt.isoformat(),
            }),
            sid,
        ),
    )

    due_human = dt.strftime("%a %Y-%m-%d %H:%M")
    preview = suggestion_text[:60] + ("…" if len(suggestion_text) > 60 else "")
    await message.reply(
        f"✓ Reminder set {due_human} for: _{preview}_\n[note:{note_id}] [followup:{fu_id}]",
        parse_mode="Markdown",
    )
    return True


# ---------------------------------------------------------------------------
# Follow-up due-time nudges (sent by scheduler/followup_due_scan.py)
# ---------------------------------------------------------------------------
#
# Callback data format: fu:<verb>:<followup_id>
#   d  → done    (close follow-up; auto-creates resolving "marked done" note)
#   s1 → snooze 1h
#   s9 → snooze to tomorrow 09:00 local
#   sc → snooze custom (ForceReply prompt → dateparser)
#
# We piggyback on `pending_actions` to track the ForceReply state for custom
# snoozes — same mechanism as briefing-suggestion custom reminders.


def build_followup_nudge_keyboard(fu_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Done",      callback_data=f"fu:d:{fu_id}"),
        InlineKeyboardButton(text="⏰ +1h",       callback_data=f"fu:s1:{fu_id}"),
        InlineKeyboardButton(text="⏰ Tomorrow",  callback_data=f"fu:s9:{fu_id}"),
        InlineKeyboardButton(text="⏰ Custom",    callback_data=f"fu:sc:{fu_id}"),
    ]])


async def _snooze_followup(db, fu_id: int, new_due_ts: int) -> None:
    """Move due_at forward. last_nudged_at stays where it is so the row
    becomes eligible for re-nudge once new_due_ts passes (because then
    last_nudged_at < due_at again)."""
    await db.execute(
        "UPDATE follow_ups SET due_at = ? WHERE id = ? AND status = 'open'",
        (int(new_due_ts), int(fu_id)),
    )


async def _close_followup_with_synthetic_note(
    db, fu_id: int, original_body: str, tz_name: str,
) -> int:
    """Close a follow-up via the citation-gated path. We can't just flip the
    status — close_followup() requires a resolving note. Create one
    automatically so the audit trail stays honest."""
    now_local = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    body = (
        f"Marked done via reminder ping at {now_local}.\n"
        f"Original: {original_body[:200]}"
    )
    r = json.loads(await tool_save_note(
        db, {"body": body, "tags": ["source/reminder-ping", "auto/closed"]},
    ))
    note_id = int(r["note_id"])
    await close_followup(db, followup_id=fu_id, resolving_note_id=note_id)
    return note_id


async def handle_followup_nudge_callback(callback, bot: Bot, agent, cfg) -> None:
    """Route fu:* callback queries fired from a reminder nudge message."""
    data = (callback.data or "").strip()
    try:
        _, verb, fu_str = data.split(":", 2)
        fu_id = int(fu_str)
    except (ValueError, IndexError):
        await callback.answer("bad data")
        return

    row = await agent.db.fetch_one(
        "SELECT id, note_id, status, due_at FROM follow_ups WHERE id = ?",
        (fu_id,),
    )
    if row is None:
        await callback.answer("follow-up gone")
        return
    if row["status"] != "open":
        await callback.answer(f"already {row['status']}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return

    note_row = await agent.db.fetch_one(
        "SELECT body FROM notes WHERE id = ?", (int(row["note_id"]),)
    )
    original_body = (note_row["body"] if note_row else "") or ""
    tz = ZoneInfo(cfg.timezone)
    now_dt = datetime.now(tz)
    status_text = ""

    try:
        if verb == "d":
            resolving = await _close_followup_with_synthetic_note(
                agent.db, fu_id, original_body, cfg.timezone,
            )
            status_text = f"✅ done [note:{resolving}]"

        elif verb == "s1":
            new_due = now_dt + timedelta(hours=1)
            await _snooze_followup(agent.db, fu_id, int(new_due.timestamp()))
            status_text = f"⏰ snoozed → {new_due.strftime('%a %H:%M')}"

        elif verb == "s9":
            new_due = (now_dt + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0,
            )
            await _snooze_followup(agent.db, fu_id, int(new_due.timestamp()))
            status_text = f"⏰ snoozed → {new_due.strftime('%a %Y-%m-%d %H:%M')}"

        elif verb == "sc":
            # ForceReply prompt — user replies with a free-text time.
            # We piggyback on pending_actions for the reply-tracking state.
            prompt = (
                f"⏰ Snooze until when?\n_{original_body[:200]}_\n\n"
                f"Reply with a time — e.g. `in 30 min`, `tomorrow 9am`, "
                f"`friday 5pm`, `2026-06-15 14:00`."
            )
            sent = await bot.send_message(
                chat_id=callback.message.chat.id,
                text=prompt,
                parse_mode="Markdown",
                reply_markup=ForceReply(
                    input_field_placeholder="e.g. tomorrow 9am", selective=True,
                ),
            )
            extra = json.dumps({"fu_id": fu_id})
            await agent.db.execute(
                "INSERT INTO pending_actions("
                " chat_id, kind, text, extra, created_at, "
                " awaiting_kind, prompt_message_id) "
                "VALUES (?, 'followup_snooze', ?, ?, ?, 'followup_snooze', ?)",
                (int(callback.message.chat.id), original_body[:200], extra,
                 int(time.time()), sent.message_id),
            )
            await callback.answer("Reply with the time")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return

        else:
            status_text = f"unknown verb {verb!r}"

    except Exception as e:  # noqa: BLE001
        log.exception("followup nudge %s for fu=%d failed", verb, fu_id)
        status_text = f"error: {type(e).__name__}"

    # Replace the keyboard with a single status pill.
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=status_text, callback_data=f"fu:done:{fu_id}")
            ]]),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("edit_message_reply_markup (nudge) failed: %s", e)
    await callback.answer(status_text)


async def handle_followup_snooze_reply(message, agent, cfg) -> bool:
    """If the user's message is a reply to a "Snooze until when?" prompt,
    parse the time deterministically and move the follow-up's due_at.

    Returns True if handled (so the bot's free-chat handler skips it)."""
    if message.reply_to_message is None:
        return False
    prompt_msg_id = message.reply_to_message.message_id
    row = await agent.db.fetch_one(
        "SELECT id, extra FROM pending_actions "
        "WHERE awaiting_kind = 'followup_snooze' AND prompt_message_id = ? "
        "  AND consumed_at IS NULL",
        (prompt_msg_id,),
    )
    if row is None:
        return False

    sid = int(row["id"])
    try:
        extra = json.loads(row["extra"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    fu_id = int(extra.get("fu_id") or 0)
    if not fu_id:
        await message.reply("internal: snooze prompt missing fu_id")
        return True

    user_time = (message.text or "").strip()
    import dateparser
    tz = ZoneInfo(cfg.timezone)
    dt = dateparser.parse(
        user_time,
        settings={
            "TIMEZONE": cfg.timezone,
            "TO_TIMEZONE": cfg.timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if dt is None:
        await message.reply(
            f"Couldn't parse `{user_time}` as a time. Try `in 2 hours`, "
            f"`tomorrow 9am`, or `2026-06-15 14:00`. Reply to the same "
            f"prompt to try again.",
            parse_mode="Markdown",
        )
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    now_dt = datetime.now(tz)
    if (now_dt - dt).total_seconds() > 300:
        await message.reply(
            f"`{user_time}` resolves to {dt.strftime('%a %Y-%m-%d %H:%M')} "
            f"which is in the past. Reply with a future time.",
            parse_mode="Markdown",
        )
        return True

    await _snooze_followup(agent.db, fu_id, int(dt.timestamp()))
    await agent.db.execute(
        "UPDATE pending_actions SET consumed_at = ?, consumed_action = 'sc', "
        " consumed_result = ? WHERE id = ?",
        (
            int(time.time()),
            json.dumps({"fu_id": fu_id, "new_due_iso": dt.isoformat()}),
            sid,
        ),
    )
    await message.reply(
        f"⏰ Snoozed [followup:{fu_id}] → {dt.strftime('%a %Y-%m-%d %H:%M')}",
        parse_mode="Markdown",
    )
    return True


async def _replace_row_with_status(callback, bot: Bot, sid: int, status: str) -> None:
    """Replace the button row for the given suggestion id with a single
    status button so the user sees what happened without losing context."""
    msg = callback.message
    if msg is None or not msg.reply_markup:
        return
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in msg.reply_markup.inline_keyboard:
        if row and any(
            (btn.callback_data or "").endswith(f":{sid}") for btn in row
        ):
            new_rows.append([
                InlineKeyboardButton(text=status, callback_data=f"b:done:{sid}")
            ])
        else:
            new_rows.append(row)
    try:
        await bot.edit_message_reply_markup(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("edit_message_reply_markup failed: %s", e)
