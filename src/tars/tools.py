"""Tool implementations called by the Agent's tool loop.

Status:
  - save_note: real (writes to notes table)
  - search_memory: real (Phase 4 — FTS5 + vec0 hybrid + Voyage rerank)
  - open_followup / close_followup: stubs (real impl in Phase 5)
  - web_research: stub (Phase 6+; the gpt-5:online tier handles live RAG for /research)

Each tool function takes (db, args_dict) and returns a JSON-serializable result
(a string that gets fed back to the model as the tool's content).

The Embedder is constructed lazily and cached on the db handle so it survives
across calls but doesn't need to be wired through every signature.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from tars.memory.embed import Embedder
from tars.memory.search import hybrid_search

log = logging.getLogger("tars.tools")


def _get_embedder(db, cfg) -> Embedder:
    """Lazily attach an Embedder to the db handle (idempotent)."""
    cached = getattr(db, "_embedder", None)
    if cached is not None:
        return cached
    e = Embedder(api_key=cfg.voyage.api_key)
    db._embedder = e
    return e


async def save_note(db, args: dict[str, Any]) -> str:
    body = (args.get("body") or "").strip()
    if not body:
        return json.dumps({"error": "empty note body"})
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags_json = json.dumps(tags)
    cur = await db.execute(
        "INSERT INTO notes(created_at, source, body, tags) VALUES (?, ?, ?, ?)",
        (int(time.time()), "agent", body, tags_json),
    )
    note_id = cur.lastrowid

    # Index the new note immediately so search_memory can find it on the very
    # next call. Failures are logged but never block the save.
    try:
        cfg = getattr(db, "_cfg", None)
        if cfg is not None:
            from tars.memory.index import index_single_doc  # local import: avoid cycle on load
            embedder = _get_embedder(db, cfg)
            await index_single_doc(
                db, embedder,
                source="note",
                source_ref=str(note_id),
                title=body[:60],
                body=body,
                tags=tags_json,
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "save_note: live index failed (%s); note saved as #%s, next reindex picks it up",
            e, note_id,
        )

    return json.dumps({"ok": True, "note_id": note_id})


async def search_memory(db, args: dict[str, Any], *, cfg=None) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "empty query"})
    k = int(args.get("k") or 8)

    if cfg is None:
        cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "embedder cfg unavailable"})

    embedder = _get_embedder(db, cfg)
    try:
        results = await hybrid_search(db, embedder, query=query, k=k)
    except Exception as e:  # noqa: BLE001
        log.exception("search_memory failed")
        return json.dumps({"error": f"search failed: {e}", "results": []})

    # Strip body in the LLM payload to avoid huge context; keep title + score.
    # The model can ask for a specific doc_id again if it needs the full body.
    summary = [
        {
            "doc_id": r["doc_id"],
            "source": r["source"],
            "title": r["title"],
            "preview": (r["body"] or "")[:300],
            "score": round(r["score"], 4),
        }
        for r in results
    ]
    return json.dumps({"results": summary})


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
