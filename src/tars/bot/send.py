"""Safe Telegram send helper.

LLM-generated text frequently contains malformed Markdown — unclosed
backticks, asymmetric asterisks, smart-quoted brackets — which Telegram
rejects with `Bad Request: can't parse entities`. The whole briefing
then fails to deliver.

`safe_send` tries Markdown first; on a parse error retries with
parse_mode stripped and Markdown control characters removed so the plain
text reads cleanly. Better an ugly message than no message.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger("tars.bot.send")

# Strip Markdown formatting glyphs for the fallback path. Brackets stay
# (they're meaningful — [note:N] / [followup:N]).
_MD_GLYPH_RE = re.compile(r"[*_`]")


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup=None,
) -> object:
    """Send `text` to `chat_id`. Try Markdown first; on parse failure
    re-send as plain text with Markdown glyphs removed.

    Returns the aiogram Message object on success."""
    try:
        return await bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        msg = str(e).lower()
        # Only fall through for actual parse-level errors. Anything else
        # (chat blocked, etc.) re-raises so the caller sees the real cause.
        if not any(kw in msg for kw in ("parse", "entity", "entities", "markdown")):
            raise
        log.warning(
            "safe_send: Markdown parse failed for chat %s (%s); retrying plain",
            chat_id, e,
        )
        cleaned = _MD_GLYPH_RE.sub("", text)
        return await bot.send_message(
            chat_id,
            cleaned,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
