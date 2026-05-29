"""Entity store + alias resolution.

After each save_note, an LLM extraction call at cron_default tier (cheap
DeepSeek) pulls named entities (people, orgs, projects, products, domains)
plus optional aliases. These get upserted into the entities + entity_aliases
tables. Conflict on `canonical` is a no-op so reruns don't break.

Query expansion: when search_memory runs, any query token that matches an
alias gets OR-ed with its canonical form into the FTS5 query. So a user
asking "what did OAI announce" finds notes mentioning "OpenAI".

The extraction runs as a fire-and-forget asyncio task — save_note returns
immediately, the LLM call happens in the background. Failures are logged
but never bubble up to the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from tars.db import Database

log = logging.getLogger("tars.memory.entities")

# Extraction prompt. Few-shot examples are critical for reliable JSON output
# from DeepSeek without verbose preamble.
EXTRACTION_SYSTEM = (
    "You extract named entities from notes and return STRICT JSON only. "
    "No prose, no markdown fences, no commentary. Just the JSON object."
)

EXTRACTION_USER_TEMPLATE = """From the note below, return JSON with this exact shape:
{{"entities":[{{"canonical":"...","kind":"person|org|project|product|domain","aliases":["...",...]}}]}}

Rules:
- canonical: the most natural full form ("OpenAI", not "OAI")
- kind: one of person, org, project, product, domain (lowercase exactly)
- aliases: short forms, abbreviations, nicknames. Empty list if none.
- Skip generic nouns ("coffee", "meeting"). Only named entities.
- If no entities, return {{"entities":[]}}

Examples:
Note: "Met Sarah from OpenAI about GPT-5 yesterday."
JSON: {{"entities":[{{"canonical":"Sarah","kind":"person","aliases":[]}},{{"canonical":"OpenAI","kind":"org","aliases":["OAI"]}},{{"canonical":"GPT-5","kind":"product","aliases":[]}}]}}

Note: "Building TARS on Hetzner CPX22 in Nuremberg."
JSON: {{"entities":[{{"canonical":"TARS","kind":"project","aliases":[]}},{{"canonical":"Hetzner","kind":"org","aliases":[]}}]}}

Note: "Bought milk."
JSON: {{"entities":[]}}

Note: <<<{body}>>>
JSON:"""


# ---------------------------------------------------------------------------
# Upsert + resolve
# ---------------------------------------------------------------------------


async def upsert_entity(
    db: Database, canonical: str, kind: str, aliases: list[str]
) -> int:
    """Idempotent insert. Returns entity id. Conflict on canonical = reuse."""
    canonical = canonical.strip()
    if not canonical:
        return 0
    row = await db.fetch_one(
        "SELECT id FROM entities WHERE canonical = ?", (canonical,)
    )
    if row is not None:
        entity_id = int(row["id"])
    else:
        cur = await db.execute(
            "INSERT INTO entities(canonical, kind, meta) VALUES (?, ?, ?)",
            (canonical, kind, "{}"),
        )
        entity_id = int(cur.lastrowid or 0)

    # The canonical itself becomes an alias (case-insensitive lookup later).
    aliases_to_add = {canonical.lower()}
    for a in aliases:
        a = (a or "").strip().lower()
        if a:
            aliases_to_add.add(a)

    for alias in aliases_to_add:
        await db.execute(
            "INSERT OR IGNORE INTO entity_aliases(alias, entity_id) VALUES (?, ?)",
            (alias, entity_id),
        )
    return entity_id


async def resolve_aliases(db: Database, query: str) -> set[str]:
    """For each token in query, look up entity_aliases.

    Returns the set of canonical forms NOT already present in the query
    (so the caller can OR them in without duplicating tokens)."""
    if not query.strip():
        return set()
    # Include hyphens in tokens so "tars-agent" stays one token, matching aliases
    # like "tars-agent". Standalone hyphens / leading-trailing get stripped.
    raw_tokens = re.findall(r"[\w-]+", query.lower())
    tokens = {t.strip("-") for t in raw_tokens if len(t.strip("-")) >= 2}
    if not tokens:
        return set()
    placeholders = ",".join("?" * len(tokens))
    rows = await db.fetch_all(
        f"SELECT ea.alias, e.canonical FROM entity_aliases ea "
        f"JOIN entities e ON e.id = ea.entity_id "
        f"WHERE ea.alias IN ({placeholders})",
        tuple(tokens),
    )
    expansions = set()
    for r in rows:
        canon = r["canonical"]
        if canon.lower() not in tokens:
            expansions.add(canon)
    return expansions


# ---------------------------------------------------------------------------
# LLM-driven extraction
# ---------------------------------------------------------------------------


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def extract_entities_for_note(
    db: Database, cfg, note_id: int, body: str
) -> int:
    """Call LLM at cron_default tier to extract entities and upsert them.

    Returns the number of entities upserted. Logs and swallows failures —
    extraction failure must never break the save_note path."""
    # Local import to avoid a circular dep at module load.
    from tars.router import CircuitOpen, call

    body = (body or "").strip()
    if not body:
        return 0

    prompt = EXTRACTION_USER_TEMPLATE.format(body=body[:2000])
    try:
        resp = await call(
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            tools=None,
            tier="cron_default",
            cfg=cfg,
            db=db,
            job_id=f"entity_extract:note:{note_id}",
        )
    except CircuitOpen as e:
        log.warning("entity extraction skipped for note %d: %s", note_id, e)
        return 0
    except Exception as e:  # noqa: BLE001
        log.warning("entity extraction LLM call failed for note %d: %s", note_id, e)
        return 0

    parsed = _safe_parse_json(resp.text)
    if not parsed:
        log.warning(
            "entity extraction: no parseable JSON for note %d. raw=%r",
            note_id, resp.text[:200],
        )
        return 0

    count = 0
    for ent in parsed.get("entities") or []:
        canonical = (ent.get("canonical") or "").strip()
        kind = (ent.get("kind") or "").strip().lower()
        aliases = ent.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        if kind not in ("person", "org", "project", "product", "domain"):
            continue
        if not canonical:
            continue
        try:
            await upsert_entity(db, canonical, kind, aliases)
            count += 1
        except Exception as e:  # noqa: BLE001
            log.warning("upsert_entity failed for %r: %s", canonical, e)

    log.info("entity extraction: note=%d upserted=%d", note_id, count)
    return count


def _safe_parse_json(text: str) -> dict | None:
    """Try direct parse, then extract the first {...} blob from the text."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _JSON_RE.search(s)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


# Background task helper. The bot/scheduled jobs use this to fire-and-forget
# extraction without awaiting in the hot path.
def schedule_extraction(db: Database, cfg, note_id: int, body: str) -> None:
    async def _runner() -> None:
        try:
            await extract_entities_for_note(db, cfg, note_id, body)
        except Exception:  # noqa: BLE001
            log.exception("entity extraction background task crashed")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No event loop — direct CLI use, just skip silently.
        log.debug("schedule_extraction skipped: no running event loop")
