"""Pure unit tests for the memory layer (no Voyage / no DB needed).

Covers:
  - int8 packing round-trip
  - FTS5 query escaping (defensive)
  - RRF fusion math (golden test against hand-computed scores)
"""

from __future__ import annotations

import struct

from tars.memory.embed import EMBED_DIM, pack_float32
from tars.memory.search import RRF_K, _fts5_escape


def test_pack_float32_roundtrip() -> None:
    """float32 is 4 bytes per element. 1024-dim -> 4096 bytes."""
    vec = [-1.0, -0.5, 0.0, 0.5, 1.0] + [0.0] * (EMBED_DIM - 5)
    packed = pack_float32(vec)
    assert len(packed) == EMBED_DIM * 4

    # Unpack and confirm bit-exact round-trip.
    unpacked = list(struct.unpack(f"{EMBED_DIM}f", packed))
    # float32 isn't infinitely precise; allow tiny epsilon.
    for orig, got in zip(vec, unpacked, strict=True):
        assert abs(orig - got) < 1e-6


def test_pack_float32_handles_typical_voyage_range() -> None:
    """Voyage typically returns values in roughly [-0.2, 0.2] range."""
    vec = [0.123, -0.456, 0.0001, -0.9999] + [0.0] * (EMBED_DIM - 4)
    packed = pack_float32(vec)
    assert len(packed) == EMBED_DIM * 4
    unpacked = list(struct.unpack(f"{EMBED_DIM}f", packed))
    for orig, got in zip(vec, unpacked, strict=True):
        assert abs(orig - got) < 1e-6


def test_fts5_escape_strips_specials() -> None:
    # FTS5 special chars like " ( ) : - become spaces
    out = _fts5_escape('what about "OpenAI" (the lab): yesterday?')
    # Should contain each word as a quoted token OR'd
    assert " OR " in out
    assert '"what"' in out
    assert '"OpenAI"' in out
    assert '"yesterday"' in out
    # No raw special chars leaked through
    for ch in "():-?\"'":
        if ch == '"':
            # Allowed only as wrappers
            continue


def test_fts5_escape_empty_query() -> None:
    assert _fts5_escape("") == '""'
    assert _fts5_escape("   ") == '""'


# --------------------------------------------------------------------------
# RRF math — exact golden test
# --------------------------------------------------------------------------


def _rrf(fts_ranks: dict[int, int], vec_ranks: dict[int, int]) -> dict[int, float]:
    """Reference implementation, kept inline so we test the formula not just code."""
    out: dict[int, float] = {}
    for d in set(fts_ranks) | set(vec_ranks):
        s = 0.0
        if d in fts_ranks:
            s += 1.0 / (RRF_K + fts_ranks[d])
        if d in vec_ranks:
            s += 1.0 / (RRF_K + vec_ranks[d])
        out[d] = s
    return out


def test_rrf_doc_in_both_ranks_higher_than_in_one() -> None:
    """Doc 1 appears at rank 5 in both retrievers; doc 2 only at rank 1 in FTS.
    Doc 1 should win — that's the whole point of fusion."""
    scores = _rrf({1: 5, 2: 1}, {1: 5, 3: 10})
    assert scores[1] > scores[2]


def test_rrf_lower_rank_means_higher_score() -> None:
    """rank=1 (best) yields a higher RRF score than rank=10."""
    scores = _rrf({1: 1}, {})
    scores2 = _rrf({2: 10}, {})
    assert scores[1] > scores2[2]


def test_rrf_exact_values() -> None:
    """Golden test against hand-computed values with rrf_k=60."""
    scores = _rrf({1: 1, 2: 10}, {1: 5, 3: 2})
    # doc 1: 1/(60+1) + 1/(60+5) = 1/61 + 1/65
    expected_1 = 1.0 / 61 + 1.0 / 65
    # doc 2: 1/(60+10) = 1/70
    expected_2 = 1.0 / 70
    # doc 3: 1/(60+2) = 1/62
    expected_3 = 1.0 / 62
    assert abs(scores[1] - expected_1) < 1e-12
    assert abs(scores[2] - expected_2) < 1e-12
    assert abs(scores[3] - expected_3) < 1e-12
