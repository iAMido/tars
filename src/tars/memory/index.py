"""Reindex notes, messages, and briefings into brain_docs (FTS5) + vec_docs (vec0).

Phase 4: full reindex on demand (CLI subcommand + once at bot startup). Each
run iterates all source documents, embeds via Voyage, and DELETE+INSERTs them
into both virtual tables, keyed by a stable doc_id from the doc_index table.

Phase 6 will optimize this:
  - Diff mode using body_hash to skip unchanged docs
  - Tombstoning deleted source docs
  - Scheduled job at IntervalTrigger(minutes=15)
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Iterable

from tars.db import Database
from tars.memory.embed import EMBED_MAX_BATCH, Embedder, pack_int8

log = logging.getLogger("tars.memory.index")

# Only assistant turns above this length earn an index entry — anything
# shorter is small talk and pollutes retrieval more than it helps.
MIN_MESSAGE_CHARS = 200


# ---------------------------------------------------------------------------
# doc_id allocation
# ---------------------------------------------------------------------------


async def _get_or_create_doc_id(db: Database, source: str, source_ref: str) -> int:
    row = await db.fetch_one(
        "SELECT doc_id FROM doc_index WHERE source = ? AND source_ref = ?",
        (source, source_ref),
    )
    if row is not None:
        return int(row["doc_id"])
    await db.execute(
        "INSERT INTO doc_index(source, source_ref, indexed_at) VALUES (?, ?, ?)",
        (source, source_ref, int(time.time())),
    )
    row = await db.fetch_one(
        "SELECT doc_id FROM doc_index WHERE source = ? AND source_ref = ?",
        (source, source_ref),
    )
    assert row is not None
    return int(row["doc_id"])


# ---------------------------------------------------------------------------
# Source iteration
# ---------------------------------------------------------------------------


async def _collect_documents(db: Database) -> list[dict]:
    """Pull all index-eligible documents from the source tables.

    Returns dicts with keys: source, source_ref, title, body, tags.
    """
    docs: list[dict] = []

    # --- notes ---
    rows = await db.fetch_all(
        "SELECT id, body, tags FROM notes ORDER BY id"
    )
    for r in rows:
        body = (r["body"] or "").strip()
        if not body:
            continue
        docs.append(
            {
                "source": "note",
                "source_ref": str(r["id"]),
                "title": body[:60],
                "body": body,
                "tags": r["tags"] or "[]",
            }
        )

    # --- briefings ---
    rows = await db.fetch_all(
        "SELECT id, date, summary FROM briefings ORDER BY id"
    )
    for r in rows:
        body = (r["summary"] or "").strip()
        if not body:
            continue
        docs.append(
            {
                "source": "briefing",
                "source_ref": r["date"],
                "title": f"Briefing {r['date']}",
                "body": body,
                "tags": "[]",
            }
        )

    # --- assistant messages (final turns, no tool_calls) ---
    rows = await db.fetch_all(
        "SELECT id, content, thread_key FROM messages "
        "WHERE role = 'assistant' AND tool_calls IS NULL "
        "AND length(content) >= ? "
        "ORDER BY id",
        (MIN_MESSAGE_CHARS,),
    )
    for r in rows:
        body = (r["content"] or "").strip()
        if not body:
            continue
        docs.append(
            {
                "source": "message",
                "source_ref": str(r["id"]),
                "title": f"{r['thread_key']} #{r['id']}",
                "body": body,
                "tags": "[]",
            }
        )

    # --- feed items (news + competitive intel) ---
    # Wrapped in try so a missing migration doesn't break the indexer.
    try:
        rows = await db.fetch_all(
            "SELECT fi.id, fi.title, fi.summary, fi.url, f.name AS feed_name, f.kind "
            "FROM feed_items fi JOIN feeds f ON f.id = fi.feed_id "
            "ORDER BY fi.id"
        )
        for r in rows:
            title = (r["title"] or "").strip()
            summary = (r["summary"] or "").strip()
            url = (r["url"] or "").strip()
            if not title and not summary:
                continue
            body = f"{title}\n\n{summary}".strip()
            if url:
                body = f"{body}\n\n{url}"
            docs.append(
                {
                    "source": "feed",
                    "source_ref": str(r["id"]),
                    "title": f"[{r['kind']}] {r['feed_name']}: {title[:60]}",
                    "body": body,
                    "tags": f'["{r["kind"]}", "{r["feed_name"]}"]',
                }
            )
    except Exception:  # noqa: BLE001
        # feeds table may not yet exist on a fresh DB before migration 007.
        pass

    return docs


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def index_single_doc(
    db: Database,
    embedder: Embedder,
    source: str,
    source_ref: str,
    title: str,
    body: str,
    tags: str = "[]",
) -> int | None:
    """Embed + upsert a single document immediately.

    Called from save_note so a newly-saved note is searchable on the very next
    search_memory call, without waiting for the bot to restart or a scheduled
    reindex. Returns the allocated doc_id (or None if body was empty)."""
    body = (body or "").strip()
    if not body:
        return None
    doc_id = await _get_or_create_doc_id(db, source, source_ref)

    vecs = await embedder.embed([body], input_type="document")

    await db.execute("DELETE FROM brain_docs WHERE doc_id = ?", (doc_id,))
    await db.execute(
        "INSERT INTO brain_docs(doc_id, source, title, body, tags) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, source, title, body, tags),
    )
    await db.execute("DELETE FROM vec_docs WHERE doc_id = ?", (doc_id,))
    await db.execute(
        "INSERT INTO vec_docs(doc_id, embedding) VALUES (?, ?)",
        (doc_id, pack_int8(vecs[0])),
    )
    await db.execute(
        "UPDATE doc_index SET indexed_at = ?, body_hash = ? WHERE doc_id = ?",
        (int(time.time()), _body_hash(body), doc_id),
    )
    return doc_id


async def reindex_brain_docs(db: Database, embedder: Embedder, *, full: bool = False) -> dict:
    """Embed and upsert source docs into brain_docs + vec_docs.

    Default: diff mode — skip docs whose body_hash matches what's already
    recorded in doc_index. `full=True` re-embeds everything (use for the
    one-shot startup reindex or after a schema change).

    Returns a small summary dict for logging."""
    t0 = time.time()
    docs = await _collect_documents(db)
    if not docs:
        log.info("reindex: nothing to index")
        return {"indexed": 0, "skipped": 0, "elapsed_s": 0.0}

    # Allocate stable doc_ids + read existing body_hash for each.
    for d in docs:
        d["doc_id"] = await _get_or_create_doc_id(db, d["source"], d["source_ref"])
        d["body_hash"] = _body_hash(d["body"])

    if full:
        to_index = docs
        skipped = 0
    else:
        # Diff: skip docs whose hash matches what's already on disk.
        existing = await db.fetch_all(
            "SELECT doc_id, body_hash FROM doc_index WHERE body_hash IS NOT NULL"
        )
        existing_map = {int(r["doc_id"]): r["body_hash"] for r in existing}
        to_index = [d for d in docs if existing_map.get(d["doc_id"]) != d["body_hash"]]
        skipped = len(docs) - len(to_index)

    if not to_index:
        elapsed = time.time() - t0
        log.info("reindex: 0 changed, %d unchanged (elapsed=%.2fs)", skipped, elapsed)
        return {"indexed": 0, "skipped": skipped, "elapsed_s": elapsed}

    # Embed in batches (only the changed ones).
    bodies = [d["body"] for d in to_index]
    vectors: list[list[int]] = []
    for i in range(0, len(bodies), EMBED_MAX_BATCH):
        batch = bodies[i : i + EMBED_MAX_BATCH]
        vectors.extend(await embedder.embed(batch, input_type="document"))

    now = int(time.time())
    for d, vec in zip(to_index, vectors, strict=True):
        doc_id = d["doc_id"]
        await db.execute("DELETE FROM brain_docs WHERE doc_id = ?", (doc_id,))
        await db.execute(
            "INSERT INTO brain_docs(doc_id, source, title, body, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, d["source"], d["title"], d["body"], d["tags"]),
        )
        await db.execute("DELETE FROM vec_docs WHERE doc_id = ?", (doc_id,))
        await db.execute(
            "INSERT INTO vec_docs(doc_id, embedding) VALUES (?, ?)",
            (doc_id, pack_int8(vec)),
        )
        await db.execute(
            "UPDATE doc_index SET indexed_at = ?, body_hash = ? WHERE doc_id = ?",
            (now, d["body_hash"], doc_id),
        )

    elapsed = time.time() - t0
    by_source: dict[str, int] = {}
    for d in to_index:
        by_source[d["source"]] = by_source.get(d["source"], 0) + 1
    log.info(
        "reindex done: %d changed, %d unchanged in %.2fs by_source=%s",
        len(to_index), skipped, elapsed, by_source,
    )
    return {
        "indexed": len(to_index),
        "skipped": skipped,
        "elapsed_s": elapsed,
        "by_source": by_source,
    }
