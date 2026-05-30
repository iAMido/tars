"""Vault sweep — every 10 min. Two-way sync between vault/ and the DB.

Three things this job does, in order:

  1. **Notes round-trip (TARS-owned files)**:
     For every vault/notes/note-NNNNN.md, compare its body to what's stored
     in the DB:
       - file body_hash != DB body_hash → user edited in Obsidian → update DB,
         re-embed into brain_docs + vec_docs.
       - file missing for a note still 'note' or 'open' status → user deleted
         the file → mark note as 'deleted' and remove from brain/vec.

  2. **Follow-up closures via vault edits**:
     Parse vault/follow-ups.md, extract [followup:N] references. Any follow-up
     that's 'open' in the DB but NOT in the file is treated as closed-via-vault
     (we create a synthetic resolving note to satisfy the citation-gated close).

  3. **User-authored markdown ingestion** (existing behavior):
     Files outside notes/ and briefings/ (e.g. inbox/, daily/, etc.) get
     ingested as new notes with source='vault'.

The DB is still the source of truth — Obsidian is a first-class editor on top.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("tars.scheduler.vault_sweep")

# Files matching this pattern were written by TARS — they are TARS-owned notes
# (note-NNNNN.md). Synced bidirectionally per step 1 below, NOT re-ingested.
TARS_NOTE_PATTERN = re.compile(r"^note-(\d{5})\.md$")
# Directories vault_sweep doesn't ingest from (Syncthing internals, TARS's own
# managed folder, Obsidian config, templates).
SKIP_DIRS = {
    "_TARS",        # all TARS-managed files live here, handled separately
    "_Templates",   # Obsidian templates — not real notes
    ".stversions", ".stfolder", ".syncthing",  # Syncthing internals
    ".obsidian", ".trash",  # Obsidian internals
    # Legacy top-level dirs (before _TARS migration) — keep skipping in case
    # any orphan files linger after the move.
    "notes", "briefings",
}
# Loose follow-up id pattern within follow-ups.md
FOLLOWUP_REF_RE = re.compile(r"\[followup:(\d+)\]")

# PARA folder → tag mapping. Used by _ingest_user_authored_markdown so notes
# pulled from PARA folders carry their workflow tag automatically.
# Keys are folder names with their leading numeric prefix; matched case-insensitive.
PARA_FOLDER_TAGS: dict[str, list[str]] = {
    "00_inbox":     ["area/inbox"],
    "01_projects":  ["area/project"],
    "02_areas":     ["area/ongoing"],
    "03_resources": ["area/resource"],
    "04_archive":   ["area/archive"],
}


def _para_tags_for(rel_path: str) -> list[str]:
    """Given a vault-relative path like '01_Projects/TARS/notes.md', return
    a list of tags: ['area/project', 'project/TARS']. For files in non-PARA
    folders, returns ['vault']."""
    parts = rel_path.replace("\\", "/").split("/")
    if not parts:
        return ["vault"]
    top = parts[0].lower()
    base_tags = PARA_FOLDER_TAGS.get(top)
    if base_tags is None:
        return ["vault"]
    tags = list(base_tags)
    # Add a per-subfolder tag for Projects, Areas, Resources, Archive
    # (Inbox is flat — no sub-grouping convention).
    if len(parts) >= 3 and top != "00_inbox":
        sub = parts[1].strip()
        if sub and not sub.startswith("."):
            # e.g. area/project + project/TARS
            kind = top.split("_", 1)[1] if "_" in top else top
            # Singularize the kind for the sub-tag prefix.
            kind_singular = {"projects": "project", "areas": "area",
                             "resources": "resource"}.get(kind, kind)
            tags.append(f"{kind_singular}/{sub}")
    return tags


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


_NOTE_HEADER_RE = re.compile(r"^# Note \d+\s*$")


def _extract_note_body_from_tars_file(text: str) -> str:
    """For TARS-owned note files: strip frontmatter, strip the '# Note N' header
    line, return only the user-meaningful body. Inverse of vault.write_note."""
    text = _strip_frontmatter(text)
    lines = text.split("\n")
    # Drop leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    # Drop the "# Note N" header.
    if lines and _NOTE_HEADER_RE.match(lines[0]):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).rstrip()


async def vault_sweep(db, cfg) -> dict:
    t0 = time.time()
    vault_root = Path(cfg.paths.vault)
    if not vault_root.exists():
        return {"elapsed_s": 0.0, "note": "vault directory does not exist"}

    notes_synced = await _sync_tars_notes_from_vault(db, cfg, vault_root)
    fu_closed = await _sync_followups_from_vault(db, cfg, vault_root)
    user_ingested = await _ingest_user_authored_markdown(db, cfg, vault_root)

    summary = {
        **notes_synced,
        **fu_closed,
        **user_ingested,
        "elapsed_s": time.time() - t0,
    }
    log.info("vault_sweep: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# 1. TARS-owned notes round-trip
# ---------------------------------------------------------------------------


async def _sync_tars_notes_from_vault(db, cfg, vault_root: Path) -> dict:
    """For every vault/_TARS/notes/note-NNNNN.md, detect edits or deletions.

    - Body edited externally -> update notes.body, re-embed.
    - File deleted -> mark note status='deleted', drop from brain/vec.
    """
    notes_dir = vault_root / "_TARS" / "notes"
    edited = 0
    deleted = 0
    if not notes_dir.exists():
        return {"notes_edited": 0, "notes_deleted": 0}

    # Snapshot of all TARS notes from the DB (only those we haven't already
    # marked deleted — re-running the sweep on a deleted note should no-op).
    db_rows = await db.fetch_all(
        "SELECT id, body, status FROM notes "
        "WHERE source = 'agent' AND status != 'deleted'"
    )

    files_present: dict[int, Path] = {}
    for p in notes_dir.glob("note-*.md"):
        m = TARS_NOTE_PATTERN.match(p.name)
        if m:
            files_present[int(m.group(1))] = p

    for row in db_rows:
        nid = int(row["id"])
        path = files_present.get(nid)
        if path is None:
            # File deleted in Obsidian → soft-delete in DB.
            await db.execute(
                "UPDATE notes SET status = 'deleted' WHERE id = ?", (nid,)
            )
            # Remove from search indexes — best effort, ignore failures.
            try:
                doc_row = await db.fetch_one(
                    "SELECT doc_id FROM doc_index WHERE source='note' AND source_ref=?",
                    (str(nid),),
                )
                if doc_row:
                    doc_id = int(doc_row["doc_id"])
                    await db.execute("DELETE FROM brain_docs WHERE doc_id = ?", (doc_id,))
                    await db.execute("DELETE FROM vec_docs WHERE doc_id = ?", (doc_id,))
            except Exception as e:  # noqa: BLE001
                log.warning("vault_sweep: index cleanup failed for note %d (%s)", nid, e)
            deleted += 1
            log.info("vault_sweep: note #%d deleted (file removed)", nid)
            continue

        # File exists — compare body.
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("vault_sweep: cannot read %s (%s)", path, e)
            continue
        new_body = _extract_note_body_from_tars_file(text).strip()
        db_body = (row["body"] or "").strip()
        if not new_body or new_body == db_body:
            continue

        # Body changed — update DB.
        await db.execute(
            "UPDATE notes SET body = ? WHERE id = ?", (new_body[:5000], nid)
        )
        # Re-embed.
        try:
            from tars.memory.embed import Embedder
            from tars.memory.index import index_single_doc
            embedder = Embedder(api_key=cfg.voyage.api_key)
            await index_single_doc(
                db, embedder,
                source="note", source_ref=str(nid),
                title=new_body[:60], body=new_body,
                tags='["vault-edited"]',
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vault_sweep: re-index failed for note %d (%s)", nid, e)
        edited += 1
        log.info("vault_sweep: note #%d body updated from vault edit", nid)

    return {"notes_edited": edited, "notes_deleted": deleted}


# ---------------------------------------------------------------------------
# 2. Follow-up closures via vault edits
# ---------------------------------------------------------------------------


async def _sync_followups_from_vault(db, cfg, vault_root: Path) -> dict:
    """If user removed a [followup:N] line from follow-ups.md, close that
    follow-up in the DB. Synthetic resolving note is created so the
    citation-gated close stays valid."""
    fu_file = vault_root / "_TARS" / "follow-ups.md"
    if not fu_file.exists():
        return {"followups_closed_via_vault": 0}
    try:
        text = fu_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("vault_sweep: cannot read follow-ups.md (%s)", e)
        return {"followups_closed_via_vault": 0}

    ids_in_file: set[int] = set()
    for m in FOLLOWUP_REF_RE.finditer(text):
        try:
            ids_in_file.add(int(m.group(1)))
        except ValueError:
            continue

    open_rows = await db.fetch_all(
        "SELECT id FROM follow_ups WHERE status = 'open'"
    )
    open_ids = {int(r["id"]) for r in open_rows}
    missing = open_ids - ids_in_file
    if not missing:
        return {"followups_closed_via_vault": 0}

    # Safety: if the file parses to ZERO follow-up tokens AND we still have open
    # follow-ups in the DB, this is almost certainly a parsing failure or a
    # stale file — refuse to mass-close.
    if not ids_in_file and open_ids:
        log.warning(
            "vault_sweep: follow-ups.md has 0 [followup:N] tokens but %d open "
            "follow-ups in DB. Refusing to close. File may be stale or unparseable.",
            len(open_ids),
        )
        return {"followups_closed_via_vault": 0, "skipped_due_to_empty_parse": len(open_ids)}

    from tars.memory.follow_ups import FollowUpError, close_followup
    closed = 0
    now = int(time.time())
    for fu_id in missing:
        # Create a synthetic resolving note so the citation-gate is satisfied.
        cur = await db.execute(
            "INSERT INTO notes(created_at, source, body, tags) "
            "VALUES (?, 'vault', ?, ?)",
            (
                now, f"Follow-up #{fu_id} marked done via Obsidian vault edit.",
                json.dumps(["vault-closure"]),
            ),
        )
        resolving_id = int(cur.lastrowid or 0)
        try:
            await close_followup(db, fu_id, resolving_id)
            closed += 1
            log.info(
                "vault_sweep: closed follow-up #%d via vault edit (resolving note #%d)",
                fu_id, resolving_id,
            )
        except FollowUpError as e:
            log.warning("vault_sweep: close_followup %d failed (%s)", fu_id, e)

    return {"followups_closed_via_vault": closed}


# ---------------------------------------------------------------------------
# 3. User-authored markdown ingestion (existing behavior)
# ---------------------------------------------------------------------------


async def _ingest_user_authored_markdown(db, cfg, vault_root: Path) -> dict:
    seen_rows = await db.fetch_all(
        "SELECT source_ref, body_hash FROM doc_index WHERE source = 'vault'"
    )
    seen = {r["source_ref"]: r["body_hash"] for r in seen_rows}

    scanned = 0
    imported = 0
    for md_path in vault_root.rglob("*.md"):
        rel_parts = md_path.relative_to(vault_root).parts
        if any(p in SKIP_DIRS for p in rel_parts[:-1]):
            continue
        if TARS_NOTE_PATTERN.match(md_path.name):
            continue
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
            continue

        # PARA-aware tagging: detect which top-level folder the note came from
        # and add area/X (+ optional project/X) tags.
        tags_list = _para_tags_for(rel)
        tags_json = json.dumps(tags_list)

        cur = await db.execute(
            "INSERT INTO notes(created_at, source, body, tags, ext_path) "
            "VALUES (?, 'vault', ?, ?, ?)",
            (int(time.time()), body[:5000], tags_json, rel),
        )
        note_id = cur.lastrowid
        try:
            from tars.memory.embed import Embedder
            from tars.memory.index import index_single_doc
            embedder = Embedder(api_key=cfg.voyage.api_key)
            await index_single_doc(
                db, embedder,
                source="vault", source_ref=rel,
                title=md_path.stem[:60], body=body, tags=tags_json,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vault_sweep: index failed for %s (%s)", rel, e)
        imported += 1
        log.info(
            "vault_sweep: imported user-authored %s as note #%s tags=%s",
            rel, note_id, tags_list,
        )

    return {"user_scanned": scanned, "user_imported": imported}
