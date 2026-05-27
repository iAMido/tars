"""Identity fallback for rerank failures.

If Voyage rerank rate-limits us, the Embedder must NOT raise. It returns
candidates in their input order with decaying scores. RRF ranking already
puts the most likely matches first, so identity fallback is a graceful
degradation, not a quality cliff.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tars.memory.embed import Embedder


@pytest.mark.asyncio
async def test_rerank_returns_identity_when_voyage_errors() -> None:
    embedder = Embedder(api_key="pa-fake-not-used")

    async def boom(*args, **kwargs):
        raise RuntimeError("rate limit 429")

    with patch.object(embedder._client, "rerank", side_effect=boom):
        out = await embedder.rerank("any query", ["a", "b", "c", "d"], top_k=3)

    # Identity: first 3 indices, decaying scores.
    assert [idx for idx, _ in out] == [0, 1, 2]
    scores = [s for _, s in out]
    # Scores must be strictly decreasing.
    assert scores == sorted(scores, reverse=True)
    # All positive.
    assert all(s > 0 for s in scores)


@pytest.mark.asyncio
async def test_rerank_empty_doc_list_returns_empty() -> None:
    embedder = Embedder(api_key="pa-fake-not-used")
    out = await embedder.rerank("any", [], top_k=8)
    assert out == []


@pytest.mark.asyncio
async def test_rerank_top_k_capped_by_doc_count() -> None:
    embedder = Embedder(api_key="pa-fake-not-used")

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    with patch.object(embedder._client, "rerank", side_effect=boom):
        out = await embedder.rerank("q", ["only one"], top_k=8)
    assert len(out) == 1
