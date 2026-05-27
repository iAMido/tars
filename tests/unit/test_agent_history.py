"""Regression: history must never contain orphaned assistant-tool-calls or tool-role
messages, which break OpenAI/OpenRouter strict validation on subsequent calls.

This bug bit us live in Phase 3 — after a /research call that internally did
a tool-loop iteration, the next chat() loaded the intermediate tool_calls
message back into history without a matching tool_call_id linkage, resulting
in a 400 Bad Request from both providers and CircuitOpen at the bot layer.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tars.agent import Agent
from tars.db import Database


@pytest.fixture
async def temp_db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = await Database.connect(path)
        await db.migrate()
        yield db
        await db.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = Path(path + suffix)
            if p.exists():
                p.unlink()


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        tiers=SimpleNamespace(model_dump=lambda: {"interactive_fast": "openai/gpt-5-mini"}),
    )


@pytest.mark.asyncio
async def test_load_history_filters_tool_roles(temp_db: Database) -> None:
    """Even if the DB has stray tool-role rows from older code, _load_history skips them."""
    agent = Agent(db=temp_db, cfg=_cfg())
    await agent._ensure_thread("test:1")

    # Direct inserts simulating a buggy older session.
    await temp_db.execute(
        "INSERT INTO messages(thread_key, ts, role, content) VALUES ('test:1', 1, 'user', 'hi')",
    )
    await temp_db.execute(
        "INSERT INTO messages(thread_key, ts, role, content, tool_calls) "
        "VALUES ('test:1', 2, 'assistant', '', '[{\"id\":\"x\",\"function\":{\"name\":\"search_memory\"}}]')",
    )
    await temp_db.execute(
        "INSERT INTO messages(thread_key, ts, role, content) VALUES ('test:1', 3, 'tool', '{\"results\":[]}')",
    )
    await temp_db.execute(
        "INSERT INTO messages(thread_key, ts, role, content) VALUES ('test:1', 4, 'assistant', 'final answer')",
    )

    history = await agent._load_history("test:1")

    roles = [m["role"] for m in history]
    # Only user + final assistant survive (tool_calls IS NULL filter excludes the buggy assistant row too).
    assert roles == ["user", "assistant"]
    assert history[0]["content"] == "hi"
    assert history[1]["content"] == "final answer"

    # No message in the loaded history should have tool_calls or tool_call_id keys.
    for m in history:
        assert "tool_calls" not in m
        assert "tool_call_id" not in m
