"""Voyage AI client wrapper.

  - embed(texts, input_type) -> list[list[int]]      (int8, 1024-dim)
  - rerank(query, docs, top_k) -> list[(idx, score)] (with identity fallback)

Identity fallback on rerank failures is critical: rerank-2.5 has aggressive
rate limits on the free tier. When it errors we degrade gracefully — RRF order
is already pretty good, and Voyage's reranker is a +5-10% MRR bump on top, not
a fundamental requirement.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Sequence

import voyageai

log = logging.getLogger("tars.memory.embed")

EMBED_MODEL = "voyage-3-large"
RERANK_MODEL = "rerank-2.5"
EMBED_DIM = 1024
EMBED_DTYPE = "int8"  # signed int8 in [-128, 127]; 1024 bytes per vector.

# Batch caps recommended by Voyage to keep request bodies under 1MB.
EMBED_MAX_BATCH = 32


def pack_int8(vec: Sequence[int]) -> bytes:
    """Pack a sequence of int8 ints into the byte layout sqlite-vec expects
    for a vec0 column declared as int8[N]."""
    return struct.pack(f"{len(vec)}b", *vec)


class Embedder:
    """Thin async wrapper around voyageai.AsyncClient."""

    def __init__(self, api_key: str) -> None:
        self._client = voyageai.AsyncClient(api_key=api_key)
        self._lock = asyncio.Lock()

    async def embed(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[int]]:
        """Embed a list of texts. Returns one int8 vector per input."""
        if not texts:
            return []
        out: list[list[int]] = []
        for i in range(0, len(texts), EMBED_MAX_BATCH):
            batch = texts[i : i + EMBED_MAX_BATCH]
            r = await self._client.embed(
                texts=batch,
                model=EMBED_MODEL,
                output_dimension=EMBED_DIM,
                output_dtype=EMBED_DTYPE,
                input_type=input_type,
            )
            out.extend(r.embeddings)
        log.info(
            "embed: %d docs in=%s model=%s dim=%d",
            len(texts),
            input_type,
            EMBED_MODEL,
            EMBED_DIM,
        )
        return out

    async def rerank(
        self, query: str, docs: list[str], top_k: int = 8
    ) -> list[tuple[int, float]]:
        """Return [(orig_index, relevance_score), ...] sorted by score desc.

        On any failure (rate limit, transport, validation): falls back to
        identity order — the first top_k docs in the input list, with scores
        decaying as 1/(rank+1). Logs the failure but does not raise.
        """
        if not docs:
            return []
        try:
            r = await self._client.rerank(
                query=query,
                documents=docs,
                model=RERANK_MODEL,
                top_k=min(top_k, len(docs)),
            )
            return [(item.index, float(item.relevance_score)) for item in r.results]
        except Exception as e:  # noqa: BLE001
            log.warning("rerank failed (%s), using identity fallback", e)
            limit = min(len(docs), top_k)
            return [(i, 1.0 / (i + 1)) for i in range(limit)]
