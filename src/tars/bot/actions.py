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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tars.memory.follow_ups import open_followup
from tars.tools import save_note as tool_save_note

log = logging.getLogger("tars.bot.actions")


def build_suggestion_keyboard(suggestion_ids: Sequence[int]) -> InlineKeyboardMarkup:
    """One row of 4 buttons per suggestion id, in input order."""
    rows: list[list[InlineKeyboardButton]] = []
    for sid in suggestion_ids:
        rows.append([
            InlineKeyboardButton(text="📝 Note",      callback_data=f"b:s:{sid}"),
            InlineKeyboardButton(text="⏰ Tomorrow",  callback_data=f"b:r1:{sid}"),
            InlineKeyboardButton(text="⏰ Next week", callback_data=f"b:r7:{sid}"),
            InlineKeyboardButton(text="✖",           callback_data=f"b:x:{sid}"),
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

        elif action in ("r1", "r7"):
            r = json.loads(await tool_save_note(agent.db, {"body": text, "tags": tags}))
            note_id = r.get("note_id")
            tz = ZoneInfo(cfg.timezone)
            days = 1 if action == "r1" else 7
            due = (datetime.now(tz) + timedelta(days=days)).replace(
                hour=9, minute=0, second=0, microsecond=0,
            )
            fu_id = await open_followup(
                agent.db, note_id=int(note_id),
                due_at_iso=due.isoformat(timespec="seconds"),
            )
            due_human = due.strftime("%a %Y-%m-%d %H:%M")
            status_text = f"✓ reminder set {due_human} [followup:{fu_id}]"
            result_payload = {"note_id": note_id, "followup_id": fu_id}

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
