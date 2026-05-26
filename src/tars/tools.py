"""Tool implementations called by the Agent's tool loop.

Phase 2 set:
  - save_note: real implementation (writes to notes table)
  - search_memory: stub returning a placeholder (real impl in Phase 4)
  - open_followup / close_followup: stubs (real impl in Phase 5)
  - web_research: stub (Phase 6+)

Each tool function takes (db, args_dict) and returns a JSON-serializable result
(a string that gets fed back to the model as the tool's content).
"""

from __future__ import annotations

import json
import time
from typing import Any


async def save_note(db, args: dict[str, Any]) -> str:
    body = (args.get("body") or "").strip()
    if not body:
        return json.dumps({"error": "empty note body"})
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    cur = await db.execute(
        "INSERT INTO notes(created_at, source, body, tags) VALUES (?, ?, ?, ?)",
        (int(time.time()), "agent", body, json.dumps(tags)),
    )
    note_id = cur.lastrowid
    return json.dumps({"ok": True, "note_id": note_id})


async def search_memory(db, args: dict[str, Any]) -> str:
    # Phase 4 will replace this with the FTS5 + sqlite-vec hybrid search.
    return json.dumps(
        {
            "status": "not_yet_implemented",
            "note": "search_memory will be wired up in Phase 4 (FTS5 + sqlite-vec + Voyage rerank).",
            "results": [],
        }
    )


async def open_followup(db, args: dict[str, Any]) -> str:
    # Phase 5 will replace this with real follow-up lifecycle.
    return json.dumps(
        {"status": "not_yet_implemented", "note": "follow-up lifecycle lands in Phase 5"}
    )


async def close_followup(db, args: dict[str, Any]) -> str:
    return json.dumps(
        {"status": "not_yet_implemented", "note": "follow-up lifecycle lands in Phase 5"}
    )


async def web_research(db, args: dict[str, Any]) -> str:
    return json.dumps({"status": "not_yet_implemented", "note": "web_research lands in Phase 6+"})


TOOL_REGISTRY = {
    "save_note": save_note,
    "search_memory": search_memory,
    "open_followup": open_followup,
    "close_followup": close_followup,
    "web_research": web_research,
}


async def run_tool(db, name: str, args_json: str) -> str:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"bad tool args JSON: {e}"})
    return await fn(db, args)
