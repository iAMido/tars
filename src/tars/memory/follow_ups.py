"""Follow-up lifecycle.

Design (compass_artifact.md §5.j, PLAN.md §5 Phase 5):
  - A follow-up is attached to a source note (the one capturing the promise).
  - open_followup(note_id, due_at_iso, promised_to) inserts a row with status='open'.
  - close_followup(followup_id, resolving_note_id) is **citation-gated**:
    refuses to close without a valid resolving note row. This is what makes
    "follow-ups closed" actually trustworthy — you can't pretend you did
    something without leaving a note that says you did.
  - list_open() is used by the weekly Sunday reconcile (Phase 6).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from tars.db import Database

log = logging.getLogger("tars.memory.follow_ups")


class FollowUpError(ValueError):
    """Raised for invalid follow-up operations. Surfaced to the LLM as tool error."""


def _parse_due(iso: str | None) -> int | None:
    if not iso:
        return None
    # Accept naive ISO (assume the configured tz, but we store UTC unix ts),
    # and accept ISO with offset/Z.
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise FollowUpError(f"bad due_at_iso: {iso!r} ({e})") from e
    if dt.tzinfo is None:
        # Treat naive as UTC. The agent should produce TZ-aware iso anyway
        # if it called get_current_time first.
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Open / Close
# ---------------------------------------------------------------------------


async def open_followup(
    db: Database,
    note_id: int,
    due_at_iso: str | None = None,
    promised_to: str | None = None,
) -> int:
    """Open a follow-up linked to note_id. Returns the new followup_id."""
    note = await db.fetch_one("SELECT id FROM notes WHERE id = ?", (int(note_id),))
    if note is None:
        raise FollowUpError(f"note {note_id} does not exist")

    due_at = _parse_due(due_at_iso)
    cur = await db.execute(
        "INSERT INTO follow_ups(note_id, promised_to, due_at, status) "
        "VALUES (?, ?, ?, 'open')",
        (int(note_id), promised_to, due_at),
    )
    fu_id = int(cur.lastrowid or 0)
    # Mark the source note as 'open' too so it shows up in open-followup queries.
    await db.execute(
        "UPDATE notes SET status = 'open' WHERE id = ?", (int(note_id),)
    )
    log.info(
        "follow_up opened id=%d note_id=%d due_at=%s to=%s",
        fu_id, note_id, due_at_iso, promised_to,
    )
    return fu_id


async def close_followup(
    db: Database, followup_id: int, resolving_note_id: int
) -> None:
    """Close an open follow-up. Citation-gated:
      - the follow-up must exist and be 'open'
      - resolving_note_id must exist
    Anything else raises FollowUpError, which the tool layer surfaces to the LLM.
    """
    fu = await db.fetch_one(
        "SELECT id, note_id, status FROM follow_ups WHERE id = ?",
        (int(followup_id),),
    )
    if fu is None:
        raise FollowUpError(f"follow-up {followup_id} does not exist")
    if fu["status"] != "open":
        raise FollowUpError(
            f"follow-up {followup_id} is not open (status={fu['status']})"
        )

    note = await db.fetch_one(
        "SELECT id FROM notes WHERE id = ?", (int(resolving_note_id),)
    )
    if note is None:
        raise FollowUpError(
            f"resolving note {resolving_note_id} does not exist — "
            f"save_note first, then close_followup with the new id"
        )

    now = int(time.time())
    await db.execute(
        "UPDATE follow_ups SET status = 'closed' WHERE id = ?",
        (int(followup_id),),
    )
    await db.execute(
        "UPDATE notes SET status = 'closed', closes_note_id = ?, closed_at = ? "
        "WHERE id = ?",
        (int(resolving_note_id), now, int(fu["note_id"])),
    )
    log.info(
        "follow_up closed id=%d resolving_note_id=%d", followup_id, resolving_note_id
    )


# ---------------------------------------------------------------------------
# Listing / reconcile
# ---------------------------------------------------------------------------


async def list_open(db: Database, limit: int = 20) -> list[dict]:
    """Return open follow-ups, soonest due first. Used by Sunday reconcile."""
    rows = await db.fetch_all(
        "SELECT fu.id, fu.note_id, fu.promised_to, fu.due_at, fu.reopened_count, n.body "
        "FROM follow_ups fu JOIN notes n ON n.id = fu.note_id "
        "WHERE fu.status = 'open' "
        "ORDER BY COALESCE(fu.due_at, 9999999999) ASC LIMIT ?",
        (limit,),
    )
    return [
        {
            "followup_id": int(r["id"]),
            "note_id": int(r["note_id"]),
            "promised_to": r["promised_to"],
            "due_at": int(r["due_at"]) if r["due_at"] is not None else None,
            "reopened_count": int(r["reopened_count"] or 0),
            "body": r["body"],
        }
        for r in rows
    ]


async def reopen_stale(db: Database, now_ts: int | None = None) -> list[int]:
    """For any follow-up whose due_at has passed and the source note never got
    closed: bump its reopened_count. Used by the weekly Sunday reconcile job."""
    if now_ts is None:
        now_ts = int(time.time())
    rows = await db.fetch_all(
        "SELECT id FROM follow_ups "
        "WHERE status = 'open' AND due_at IS NOT NULL AND due_at < ?",
        (now_ts,),
    )
    ids = [int(r["id"]) for r in rows]
    for fu_id in ids:
        await db.execute(
            "UPDATE follow_ups SET reopened_count = reopened_count + 1 WHERE id = ?",
            (fu_id,),
        )
    return ids
