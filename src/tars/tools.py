"""Tool implementations called by the Agent's tool loop.

Status:
  - save_note: real (writes to notes table + fires entity extraction)
  - search_memory: real (FTS5 + vec0 hybrid + Voyage rerank + alias expansion)
  - open_followup / close_followup / list_followups: real (Phase 5)
  - get_current_time: real (Phase 5 — supports relative due dates)
  - web_research: stub (the gpt-5:online tier handles live RAG for /research)

Each tool function takes (db, args_dict) and returns a JSON-serializable result
(a string that gets fed back to the model as the tool's content).

The Embedder is constructed lazily and cached on the db handle so it survives
across calls but doesn't need to be wired through every signature.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tars.memory import entities as entities_mod
from tars.memory import follow_ups as fu_mod
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
    cfg = getattr(db, "_cfg", None)
    try:
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

    # Fire-and-forget entity extraction. Runs in background; never blocks the
    # save_note response. Failures are logged inside the helper.
    if cfg is not None and note_id is not None:
        entities_mod.schedule_extraction(db, cfg, int(note_id), body)

    # Mirror the note into the vault directory (markdown for Obsidian).
    # Failures here are also non-fatal — the note is already in SQLite.
    if cfg is not None and note_id is not None:
        try:
            from tars.integrations.vault import write_note as _vault_write
            _vault_write(
                cfg, note_id=int(note_id), body=body,
                tags=tags, source="agent", status="note",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vault mirror failed for note %s: %s", note_id, e)

    return json.dumps({"ok": True, "note_id": note_id})


async def get_note(db, args: dict[str, Any]) -> str:
    """Fetch a single note by its exact id."""
    try:
        nid = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "get_note requires integer note_id"})
    row = await db.fetch_one(
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       source, status, body, tags, closes_note_id, "
        "       datetime(closed_at,'unixepoch','localtime') AS closed_at "
        "FROM notes WHERE id = ?",
        (nid,),
    )
    if row is None:
        return json.dumps({"error": f"note #{nid} does not exist"})
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return json.dumps(
        {
            "id": int(row["id"]),
            "created": row["created"],
            "source": row["source"],
            "status": row["status"],
            "body": row["body"],
            "tags": tags,
            "closes_note_id": row["closes_note_id"],
            "closed_at": row["closed_at"],
        },
        ensure_ascii=False,
    )


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
    try:
        note_id = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "open_followup requires integer note_id"})
    due = args.get("due_at_iso")
    to = args.get("to")
    try:
        fu_id = await fu_mod.open_followup(
            db, note_id=note_id, due_at_iso=due, promised_to=to
        )
    except fu_mod.FollowUpError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"ok": True, "followup_id": fu_id})


async def close_followup(db, args: dict[str, Any]) -> str:
    try:
        fu_id = int(args.get("followup_id"))  # type: ignore[arg-type]
        resolving = int(args.get("resolving_note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps(
            {"error": "close_followup requires integer followup_id and resolving_note_id"}
        )
    try:
        await fu_mod.close_followup(db, fu_id, resolving)
    except fu_mod.FollowUpError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"ok": True, "followup_id": fu_id, "resolving_note_id": resolving})


async def list_followups(db, args: dict[str, Any]) -> str:
    limit = int(args.get("limit") or 20)
    rows = await fu_mod.list_open(db, limit=limit)
    return json.dumps({"open": rows})


async def get_current_time(db, args: dict[str, Any]) -> str:
    """Return current time so the model can compute relative due dates.
    Defaults to the configured TARS timezone (Asia/Jerusalem)."""
    cfg = getattr(db, "_cfg", None)
    tz_name = args.get("timezone") or (cfg.timezone if cfg else "Asia/Jerusalem")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Jerusalem")
    now = datetime.now(tz)
    return json.dumps(
        {
            "iso": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
            "human": now.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
            "unix": int(now.timestamp()),
            "timezone": str(tz),
        }
    )


async def web_research(db, args: dict[str, Any]) -> str:
    return json.dumps({"status": "not_yet_implemented", "note": "web_research lands in Phase 6+"})


TOOL_REGISTRY = {
    "save_note": save_note,
    "get_note": get_note,
    "search_memory": search_memory,
    "open_followup": open_followup,
    "close_followup": close_followup,
    "list_followups": list_followups,
    "get_current_time": get_current_time,
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
