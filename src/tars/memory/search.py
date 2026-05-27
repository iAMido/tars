"""Hybrid search: FTS5 (BM25) + vec0 (cosine on int8 embeddings),
merged with Reciprocal Rank Fusion, then Voyage rerank-2.5 final pass.

The canonical RRF pattern (Alex Garcia's "Hybrid full-text search and vector
search with SQLite"):
    score = SUM(1 / (rrf_k + rank_i))    for each retriever i

PLAN.md §5 Phase 4: candidate pool ~25, final k=8.
"""

from __future__ import annotations

import logging

from tars.db import Database
from tars.memory.embed import Embedder, pack_int8

log = logging.getLogger("tars.memory.search")

RRF_K = 60
CANDIDATE_POOL = 25


# ---------------------------------------------------------------------------
# FTS5 query escaping
# ---------------------------------------------------------------------------


def _fts5_escape(query: str) -> str:
    """FTS5 has its own query language (quotes, AND/OR/NOT, NEAR, prefix).
    For free-text queries from an LLM/user we just OR the tokens together
    after stripping FTS5 syntax characters."""
    # Strip characters with special FTS5 meaning. Then wrap each token in
    # double-quotes to disable column-filter parsing.
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in query)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


async def hybrid_search(
    db: Database,
    embedder: Embedder,
    query: str,
    k: int = 8,
    candidate_pool: int = CANDIDATE_POOL,
) -> list[dict]:
    """Return up to k matching docs with reranked relevance scores.

    Each result dict has: doc_id, source, title, body, tags, score.
    """
    if not query.strip():
        return []

    # --- 1. embed the query ---
    qvecs = await embedder.embed([query], input_type="query")
    qvec_bytes = pack_int8(qvecs[0])

    # --- 2. FTS5 BM25 candidates ---
    fts_query = _fts5_escape(query)
    try:
        fts_rows = await db.fetch_all(
            "SELECT doc_id, rank FROM brain_docs "
            "WHERE brain_docs MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, candidate_pool),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("FTS5 query failed (%s); proceeding vector-only", e)
        fts_rows = []
    fts_ranks: dict[int, int] = {
        int(r["doc_id"]): i + 1 for i, r in enumerate(fts_rows)
    }

    # --- 3. vec0 cosine KNN candidates ---
    try:
        vec_rows = await db.fetch_all(
            "SELECT doc_id, distance FROM vec_docs "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (qvec_bytes, candidate_pool),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("vec0 query failed (%s); proceeding FTS-only", e)
        vec_rows = []
    vec_ranks: dict[int, int] = {
        int(r["doc_id"]): i + 1 for i, r in enumerate(vec_rows)
    }

    # --- 4. RRF fusion ---
    all_ids = set(fts_ranks) | set(vec_ranks)
    if not all_ids:
        return []
    rrf_scores: dict[int, float] = {}
    for doc_id in all_ids:
        s = 0.0
        if doc_id in fts_ranks:
            s += 1.0 / (RRF_K + fts_ranks[doc_id])
        if doc_id in vec_ranks:
            s += 1.0 / (RRF_K + vec_ranks[doc_id])
        rrf_scores[doc_id] = s
    top_ids = sorted(rrf_scores, key=lambda d: rrf_scores[d], reverse=True)[:candidate_pool]

    # --- 5. fetch full bodies ---
    placeholders = ",".join("?" * len(top_ids))
    rows = await db.fetch_all(
        f"SELECT doc_id, source, title, body, tags "
        f"FROM brain_docs WHERE doc_id IN ({placeholders})",
        tuple(top_ids),
    )
    by_id = {int(r["doc_id"]): r for r in rows}
    candidates = [by_id[d] for d in top_ids if d in by_id]

    # --- 6. rerank (with identity fallback inside) ---
    bodies = [c["body"] for c in candidates]
    reranked = await embedder.rerank(query, bodies, top_k=k)

    return [
        {
            "doc_id": int(candidates[idx]["doc_id"]),
            "source": candidates[idx]["source"],
            "title": candidates[idx]["title"],
            "body": candidates[idx]["body"],
            "tags": candidates[idx]["tags"],
            "score": float(score),
        }
        for idx, score in reranked
        if idx < len(candidates)
    ]
