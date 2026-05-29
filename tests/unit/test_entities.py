"""Entity store: upsert, alias resolution, JSON parser tolerance."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tars.db import Database
from tars.memory.entities import _safe_parse_json, resolve_aliases, upsert_entity


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


@pytest.mark.asyncio
async def test_upsert_then_resolve_alias(db: Database) -> None:
    eid = await upsert_entity(db, "OpenAI", "org", ["OAI", "Open AI"])
    assert eid > 0

    expansions = await resolve_aliases(db, "what did OAI announce yesterday")
    assert "OpenAI" in expansions


@pytest.mark.asyncio
async def test_canonical_resolves_only_when_alias_present(db: Database) -> None:
    await upsert_entity(db, "OpenAI", "org", ["OAI"])
    # Query mentions "OpenAI" directly — no expansion needed.
    expansions = await resolve_aliases(db, "what did OpenAI say")
    assert "OpenAI" not in expansions  # excluded since already in query
    # Unrelated query → no expansions.
    none = await resolve_aliases(db, "buy milk tomorrow")
    assert none == set()


@pytest.mark.asyncio
async def test_upsert_idempotent(db: Database) -> None:
    e1 = await upsert_entity(db, "TARS", "project", [])
    e2 = await upsert_entity(db, "TARS", "project", ["tars-agent"])
    assert e1 == e2  # same id
    # Now the new alias also resolves.
    expansions = await resolve_aliases(db, "tars-agent status")
    assert "TARS" in expansions


@pytest.mark.asyncio
async def test_empty_canonical_skipped(db: Database) -> None:
    eid = await upsert_entity(db, "", "person", [])
    assert eid == 0


@pytest.mark.asyncio
async def test_resolve_handles_punctuation(db: Database) -> None:
    await upsert_entity(db, "OpenAI", "org", ["OAI"])
    expansions = await resolve_aliases(db, "OAI?? OAI! oai.")
    assert "OpenAI" in expansions


def test_safe_parse_json_direct() -> None:
    assert _safe_parse_json('{"entities":[]}') == {"entities": []}


def test_safe_parse_json_extracts_from_prose() -> None:
    text = 'Here is the JSON:\n```json\n{"entities":[{"canonical":"X","kind":"person","aliases":[]}]}\n```\nDone.'
    parsed = _safe_parse_json(text)
    assert parsed is not None
    assert parsed["entities"][0]["canonical"] == "X"


def test_safe_parse_json_returns_none_for_garbage() -> None:
    assert _safe_parse_json("not json at all") is None
    assert _safe_parse_json("") is None
