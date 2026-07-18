"""Deterministic intent router — runs BEFORE the LLM on free-chat messages.

The root fix for the recurring "LLM fakes tool confirmations" class of bug:
DeepSeek would reply "Added." / "Done." / "Promoted." without calling the
tool. Prompt rules reduced it; this eliminates it for the command shapes we
can parse deterministically, and forces tool_choice="required" for command-
shaped messages we can't fully parse.

Three outcomes for a message:
  1. DIRECT  — regex fully parsed the command; execute the tool right here,
               zero LLM involvement (like the `note:` fast-path — which has
               never lied once). Returns the reply text.
  2. REQUIRE — message is command-shaped (imperative verb + object) but not
               fully parseable. The LLM handles it, but with
               tool_choice="required" so a text-only fake confirmation is
               impossible on the first turn.
  3. NONE    — open question / chat. LLM as usual.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("tars.bot.intent")


@dataclass
class DirectResult:
    """A command executed deterministically. `reply` goes straight to the user."""
    reply: str


# ---------------------------------------------------------------------------
# Patterns — conservative on purpose. A pattern only qualifies for DIRECT
# when the parse is unambiguous; anything fuzzy falls to REQUIRE (LLM with
# forced tool use) which is still lie-proof.
# ---------------------------------------------------------------------------

_DELETE_NOTE_RE = re.compile(
    r"(?i)^\s*(?:delete|remove|erase)\s+note[\s:#-]*(\d{1,8})\s*$"
)
_PROMOTE_RE = re.compile(
    r"(?i)^\s*promote\s+note[\s:#-]*(\d{1,8})\s+to\s+([\w/ .-]+?)\s*$"
)
_CLOSE_FU_RE = re.compile(
    r"(?i)^\s*(?:close|done)\s+follow[\s-]?up[\s:#-]*(\d{1,8})\s*$"
)
_SHOW_NOTE_RE = re.compile(
    r"(?i)^\s*(?:show|get|open)\s+(?:me\s+)?note[\s:#-]*(\d{1,8})\s*$"
)

# Imperative verbs that indicate the user wants an ACTION performed. If the
# message starts with one of these (EN or HE) and no DIRECT pattern matched,
# the LLM call goes out with tool_choice="required".
_COMMAND_VERB_RE = re.compile(
    r"(?i)^\s*(?:"
    r"add|append|prepend|update|edit|replace|mark|set|create|make|write|"
    r"delete|remove|erase|close|promote|remind|save|open|file|move|rename|"
    r"תוסיף|תוסיפי|הוסף|עדכן|תעדכן|מחק|תמחק|סגור|תסגור|צור|תצור|תזכיר|שמור"
    r")\b"
)


def looks_like_command(text: str) -> bool:
    """True when the message is imperative/command-shaped — the LLM should be
    forced to call a tool rather than free-texting a confirmation."""
    return bool(_COMMAND_VERB_RE.match(text or ""))


async def try_direct(text: str, agent, cfg) -> DirectResult | None:
    """Attempt to fully parse + execute the command without the LLM.
    Returns DirectResult on success, None when no direct pattern matches.
    Tool errors are returned as the reply (not raised) — the user should see
    them verbatim, exactly like the slash commands do."""
    t = (text or "").strip()

    m = _DELETE_NOTE_RE.match(t)
    if m:
        from tars.tools import delete_note
        payload = json.loads(await delete_note(agent.db, {"note_id": int(m.group(1))}))
        if payload.get("ok"):
            if payload.get("already_deleted"):
                return DirectResult(f"[note:{m.group(1)}] was already deleted.")
            return DirectResult(f"🗑 Deleted [note:{m.group(1)}].")
        return DirectResult(f"Delete failed: {payload.get('error', 'unknown')}")

    m = _PROMOTE_RE.match(t)
    if m:
        from tars.tools import promote_note
        nid, folder = int(m.group(1)), m.group(2).strip()
        payload = json.loads(await promote_note(
            agent.db, {"note_id": nid, "dest_folder": folder},
        ))
        if payload.get("ok"):
            return DirectResult(f"📌 Promoted [note:{nid}] → {payload['path']}")
        return DirectResult(f"Promote failed: {payload.get('error', 'unknown')}")

    m = _CLOSE_FU_RE.match(t)
    if m:
        fu_id = int(m.group(1))
        row = await agent.db.fetch_one(
            "SELECT fu.status, n.body FROM follow_ups fu "
            "JOIN notes n ON n.id = fu.note_id WHERE fu.id = ?",
            (fu_id,),
        )
        if row is None:
            return DirectResult(f"Follow-up #{fu_id} does not exist.")
        if row["status"] != "open":
            return DirectResult(f"Follow-up #{fu_id} is already {row['status']}.")
        from tars.bot.actions import _close_followup_with_synthetic_note
        try:
            resolving = await _close_followup_with_synthetic_note(
                agent.db, fu_id, row["body"] or "", cfg.timezone,
            )
            return DirectResult(
                f"✅ Closed [followup:{fu_id}]. Resolving note [note:{resolving}]."
            )
        except Exception as e:  # noqa: BLE001
            return DirectResult(f"Close failed: {e}")

    m = _SHOW_NOTE_RE.match(t)
    if m:
        from tars.tools import get_note
        payload = json.loads(await get_note(agent.db, {"note_id": int(m.group(1))}))
        if payload.get("error"):
            return DirectResult(payload["error"])
        body = (payload.get("body") or "").strip()
        return DirectResult(
            f"[note:{payload['id']}] ({payload.get('created', '?')}, "
            f"{payload.get('status', '?')})\n{body[:1500]}"
        )

    return None
