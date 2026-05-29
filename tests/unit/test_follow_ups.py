"""Follow-up lifecycle tests. Real SQLite tempfile DB, no mocks."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from tars.db import Database
from tars.memory.follow_ups import (
    FollowUpError,
    close_followup,
    list_open,
    open_followup,
    reopen_stale,
)


@pytest.fixture
async def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        d = await Database.connect(path)
        await d.migrate()
        yield d
        await d.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = Path(path + suffix)
            if p.exists():
                p.unlink()


async def _new_note(db: Database, body: str = "test note") -> int:
    cur = await db.execute(
        "INSERT INTO notes(created_at, source, body) VALUES (?, 'test', ?)",
        (int(time.time()), body),
    )
    return int(cur.lastrowid or 0)


@pytest.mark.asyncio
async def test_open_then_close_happy_path(db: Database) -> None:
    src_note = await _new_note(db, "ping Alice next Tuesday")
    fu_id = await open_followup(
        db, src_note, due_at_iso="2026-06-02T10:00:00+03:00", promised_to="Alice"
    )
    assert fu_id > 0

    # Source note status should now be 'open'.
    row = await db.fetch_one("SELECT status FROM notes WHERE id = ?", (src_note,))
    assert row["status"] == "open"

    resolving = await _new_note(db, "pinged Alice")
    await close_followup(db, fu_id, resolving)

    # FU now closed, source note now closed and points at resolver.
    fu_row = await db.fetch_one("SELECT status FROM follow_ups WHERE id = ?", (fu_id,))
    assert fu_row["status"] == "closed"
    src_row = await db.fetch_one(
        "SELECT status, closes_note_id FROM notes WHERE id = ?", (src_note,)
    )
    assert src_row["status"] == "closed"
    assert int(src_row["closes_note_id"]) == resolving


@pytest.mark.asyncio
async def test_close_without_resolving_note_raises(db: Database) -> None:
    src = await _new_note(db, "pay rent")
    fu_id = await open_followup(db, src)
    with pytest.raises(FollowUpError, match="resolving note .* does not exist"):
        await close_followup(db, fu_id, resolving_note_id=99999)


@pytest.mark.asyncio
async def test_open_followup_unknown_note_raises(db: Database) -> None:
    with pytest.raises(FollowUpError, match="note .* does not exist"):
        await open_followup(db, note_id=99999)


@pytest.mark.asyncio
async def test_close_already_closed_raises(db: Database) -> None:
    src = await _new_note(db, "ship feature")
    fu = await open_followup(db, src)
    resolving = await _new_note(db, "shipped")
    await close_followup(db, fu, resolving)
    # Second close attempt must fail.
    with pytest.raises(FollowUpError, match="not open"):
        await close_followup(db, fu, resolving)


@pytest.mark.asyncio
async def test_bad_iso_raises(db: Database) -> None:
    src = await _new_note(db)
    with pytest.raises(FollowUpError, match="bad due_at_iso"):
        await open_followup(db, src, due_at_iso="not a date")


@pytest.mark.asyncio
async def test_list_open_orders_by_due(db: Database) -> None:
    a = await _new_note(db, "A")
    b = await _new_note(db, "B")
    c = await _new_note(db, "C")
    await open_followup(db, a, due_at_iso="2026-07-01T00:00:00+00:00")
    await open_followup(db, b, due_at_iso="2026-06-01T00:00:00+00:00")
    await open_followup(db, c)  # no due → sorts last

    rows = await list_open(db)
    bodies = [r["body"] for r in rows]
    assert bodies == ["B", "A", "C"]


@pytest.mark.asyncio
async def test_reopen_stale_bumps_counter(db: Database) -> None:
    src = await _new_note(db)
    past = "1990-01-01T00:00:00+00:00"
    fu = await open_followup(db, src, due_at_iso=past)
    ids = await reopen_stale(db)
    assert fu in ids
    row = await db.fetch_one(
        "SELECT reopened_count FROM follow_ups WHERE id = ?", (fu,)
    )
    assert int(row["reopened_count"]) == 1
