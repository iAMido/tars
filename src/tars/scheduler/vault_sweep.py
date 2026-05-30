"""Vault sweep — every 10 min.

Walks the vault directory for any markdown file we haven't indexed yet, OR
files whose mtime is newer than our last indexing. Imports them as new
notes (source='vault') so anything you type directly into Obsidian gets
picked up by search_memory.

This is the read-back side of the one-way vault mirror. Strictly
file-system → SQLite; the mirror module handles SQLite → file-system.

Conflict handling: notes we ourselves wrote (vault/notes/note-NNNNN.md)
are skipped — they're already in the DB and writing them back would
double-index. We only ingest user-authored files anywhere else under vault/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("tars.scheduler.vault_sweep")

# Files matching this pattern were written by TARS — skip them.
TARS_NOTE_PATTERN = re.compile(r"^note-\d{5}\.md$")
# Directories we don't ingest from (Syncthing internals, our own briefings, etc.)
SKIP_DIRS = {"notes", "briefings", ".stversions", ".stfolder", ".syncthing", ".obsidian"}


async def vault_sweep_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await vault_sweep(rt.db, rt.cfg)


def _body_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter block if present."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5 :]
    return text


async def vault_sweep(db, cfg) -> dict:
    t0 = time.time()
    vault_root = Path(cfg.paths.vault)
    if not vault_root.exists():
        return {"scanned": 0, "imported": 0, "elapsed_s": 0.0}

    # Build a set of paths we've already imported, indexed by their content hash
    # so we re-ingest if the file actually changed.
    seen_rows = await db.fetch_all(
        "SELECT source_ref, body_hash FROM doc_index WHERE source = 'vault'"
    )
    seen = {r["source_ref"]: r["body_hash"] for r in seen_rows}

    scanned = 0
    imported = 0
    skipped_tars = 0

    for md_path in vault_root.rglob("*.md"):
        # Filter directories we don't index from.
        rel_parts = md_path.relative_to(vault_root).parts
        if any(p in SKIP_DIRS for p in rel_parts[:-1]):
            continue
        if TARS_NOTE_PATTERN.match(md_path.name):
            skipped_tars += 1
            continue
        # Skip the rolling follow-ups file too.
        if md_path.name == "follow-ups.md":
            continue

        scanned += 1
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("vault_sweep: cannot read %s (%s)", md_path, e)
            continue

        body = _strip_frontmatter(text).strip()
        if not body:
            continue

        rel = str(md_path.relative_to(vault_root)).replace("\\", "/")
        h = _body_hash(body)
        if seen.get(rel) == h:
            continue  # already indexed at this version

        # Insert/update as a note with source='vault' so we don't confuse with
        # agent-saved notes. The existing index_single_doc handles the rest.
        cur = await db.execute(
            "INSERT INTO notes(created_at, source, body, tags, ext_path) "
            "VALUES (?, 'vault', ?, '[\"vault\"]', ?)",
            (int(time.time()), body[:5000], rel),
        )
        note_id = cur.lastrowid

        # Trigger inline indexing if voyage creds are available.
        try:
            from tars.memory.embed import Embedder
            from tars.memory.index import index_single_doc
            embedder = Embedder(api_key=cfg.voyage.api_key)
            await index_single_doc(
                db, embedder,
                source="vault",
                source_ref=rel,
                title=md_path.stem[:60],
                body=body,
                tags='["vault"]',
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vault_sweep: index failed for %s (%s); will retry next reindex", rel, e)

        imported += 1
        log.info("vault_sweep: imported %s as note #%s", rel, note_id)

    elapsed = time.time() - t0
    log.info(
        "vault_sweep: scanned=%d imported=%d (skipped %d tars-owned files) elapsed=%.2fs",
        scanned, imported, skipped_tars, elapsed,
    )
    return {
        "scanned": scanned,
        "imported": imported,
        "skipped_tars": skipped_tars,
        "elapsed_s": elapsed,
    }
