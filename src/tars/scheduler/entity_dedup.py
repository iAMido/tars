"""Entity dedup — nightly at 02:00 (local time).

Merges obvious duplicate entities deterministically. No LLM call — uses
two heuristics:

  1. **Case duplicates.** Two entities with the same lowercased canonical
     (e.g. 'OpenAI' and 'openai') are the same entity; merge into the one
     with more aliases (more "evidence" of canonical truth).

  2. **Alias collisions.** Entity A's canonical equals (lowercased) one of
     entity B's aliases → A is just an alias of B; merge A into B.

Merge operation:
  - Move all of the loser's aliases under the winner's id (INSERT OR IGNORE)
  - DELETE the loser's row from entities
  - The aliases table CASCADEs… wait, it doesn't. We migrate the aliases
    explicitly. The doc_index table doesn't reference entities so nothing
    else to fix up.

LLM-assisted fuzzy dedup ("Sam" vs "Sam Altman", "Cloudflare" vs "CF, Inc")
is deferred — too easy to false-positive. The heuristic version cleans up
the obvious cases without any wrong-merge risk.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("tars.scheduler.entity_dedup")


async def entity_dedup_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await entity_dedup(rt.db, rt.cfg)


async def _merge(db, winner_id: int, loser_id: int, loser_canonical: str) -> None:
    """Move loser's aliases to winner; delete loser."""
    # First, ensure the loser's canonical is preserved as an alias of the winner.
    await db.execute(
        "INSERT OR IGNORE INTO entity_aliases(alias, entity_id) VALUES (?, ?)",
        (loser_canonical.lower(), winner_id),
    )
    # Move all of loser's aliases to winner.
    await db.execute(
        "UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?",
        (winner_id, loser_id),
    )
    # Drop any aliases that couldn't move (already exist on winner).
    await db.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (loser_id,))
    # Drop the loser.
    await db.execute("DELETE FROM entities WHERE id = ?", (loser_id,))
    log.info("merged entity loser=%d -> winner=%d", loser_id, winner_id)


async def entity_dedup(db, cfg) -> dict:
    t0 = time.time()

    # Fetch entities with their alias count (a rough "evidence" metric).
    rows = await db.fetch_all(
        "SELECT e.id, e.canonical, e.kind, "
        "       (SELECT COUNT(*) FROM entity_aliases ea WHERE ea.entity_id = e.id) AS alias_count "
        "FROM entities e ORDER BY e.id"
    )
    entities = [dict(r) for r in rows]
    if len(entities) < 2:
        log.info("entity_dedup: %d entities, nothing to dedup", len(entities))
        return {"entities": len(entities), "merged": 0, "elapsed_s": time.time() - t0}

    merged = 0

    # --- Heuristic 1: case duplicates ---
    by_lower: dict[str, list[dict]] = {}
    for e in entities:
        by_lower.setdefault(e["canonical"].lower(), []).append(e)
    for canon_lower, group in by_lower.items():
        if len(group) < 2:
            continue
        # Winner: most aliases; tiebreak on lowest id.
        group.sort(key=lambda x: (-x["alias_count"], x["id"]))
        winner = group[0]
        for loser in group[1:]:
            await _merge(db, winner["id"], loser["id"], loser["canonical"])
            merged += 1

    # --- Heuristic 2: alias collisions ---
    # Re-fetch since heuristic 1 deleted some rows.
    rows = await db.fetch_all("SELECT id, canonical FROM entities")
    entities2 = [dict(r) for r in rows]
    alias_rows = await db.fetch_all("SELECT alias, entity_id FROM entity_aliases")
    alias_owner: dict[str, int] = {r["alias"]: int(r["entity_id"]) for r in alias_rows}

    for e in entities2:
        canon_lower = e["canonical"].lower()
        owner = alias_owner.get(canon_lower)
        if owner is None or owner == e["id"]:
            continue
        # Another entity claims this canonical as an alias → fold this entity in.
        await _merge(db, winner_id=owner, loser_id=e["id"], loser_canonical=e["canonical"])
        merged += 1

    elapsed = time.time() - t0
    final = await db.fetch_one("SELECT COUNT(*) AS n FROM entities")
    log.info(
        "entity_dedup: started=%d ended=%d merged=%d elapsed=%.2fs",
        len(entities), final["n"], merged, elapsed,
    )
    return {
        "entities_before": len(entities),
        "entities_after": int(final["n"]),
        "merged": merged,
        "elapsed_s": elapsed,
    }
