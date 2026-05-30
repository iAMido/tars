"""Vault writer — exports notes / briefings / follow-ups as markdown files
under cfg.paths.vault.

Designed so Obsidian (or any markdown editor) can open the directory directly.
File layout:

    vault/
    ├── notes/
    │   └── note-{id:05d}.md       # one file per note, frontmatter + body
    ├── briefings/
    │   └── {YYYY-MM-DD}.md        # one file per daily briefing
    └── follow-ups.md              # single regenerated file with all open + closed

All writes are atomic (write-to-temp + os.replace) so Syncthing never propagates
a partial file to other devices.

This is the WRITE side. A future vault_sweep job (Phase 6.1 / Tier 4) will read
back any user edits and reindex into brain_docs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger("tars.integrations.vault")


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically: temp file in same dir + os.replace.
    fsync the temp file so we know it's on disk before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_tag(t: str) -> str:
    """Obsidian frontmatter tags should NOT include `#` and should use `/`
    for hierarchy (not `-`). Strip the `#`, normalize whitespace, lowercase
    the alphabetic parts (dates stay numeric)."""
    s = (t or "").strip().lstrip("#").strip()
    return s


def _frontmatter(fields: dict) -> str:
    """Minimal YAML frontmatter compatible with Obsidian Properties.

    Quirks Obsidian cares about:
      - `tags:` must be a YAML list. NO `#` prefix in frontmatter — Obsidian
        renders tags from frontmatter without it; including `#` shows literal `#`.
      - `aliases:` is a list of alternative note names — useful for [[wikilinks]].
      - Other keys become Properties in Obsidian's new Properties pane.
    """
    def _emit_scalar(v):
        if isinstance(v, (int, float)):
            return str(v)
        return json.dumps(str(v), ensure_ascii=False)

    def _emit_list(items):
        if not items:
            return "[]"
        # Block-style list is friendlier for Obsidian's Properties UI than inline.
        return "\n" + "\n".join(f"  - {json.dumps(x, ensure_ascii=False)}" for x in items)

    lines: list[str] = []
    for k, v in fields.items():
        if k == "tags" and isinstance(v, list):
            # Dedup while preserving order so repeated tags don't appear twice.
            seen: set[str] = set()
            cleaned: list[str] = []
            for t in v:
                c = _clean_tag(t)
                if c and c not in seen:
                    seen.add(c)
                    cleaned.append(c)
            lines.append(f"tags:{_emit_list(cleaned)}")
        elif k == "aliases" and isinstance(v, list):
            lines.append(f"aliases:{_emit_list(v)}")
        elif isinstance(v, list):
            inner = ", ".join(json.dumps(x, ensure_ascii=False) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {_emit_scalar(v)}")
    body = "\n".join(lines)
    return f"---\n{body}\n---\n"


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def _vault_root(cfg) -> Path:
    return Path(cfg.paths.vault)


def write_note(cfg, note_id: int, body: str, tags: Sequence[str] | None = None,
               source: str = "agent", created_at: int | None = None,
               status: str = "note") -> Path:
    """Write a single note file. Idempotent — overwrites if it already exists."""
    tz = ZoneInfo(getattr(cfg, "timezone", "UTC"))
    created = datetime.fromtimestamp(created_at or int(time.time()), tz=tz)
    notes_dir = _vault_root(cfg) / "notes"
    path = notes_dir / f"note-{int(note_id):05d}.md"

    front = _frontmatter({
        "id": int(note_id),
        "created": created.isoformat(timespec="seconds"),
        "source": source,
        "status": status,
        "tags": list(tags or []),
    })
    content = (
        f"{front}\n"
        f"# Note {note_id}\n\n"
        f"{body.strip()}\n"
    )
    _atomic_write(path, content)
    log.debug("vault: wrote %s", path)
    return path


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------


def write_briefing(cfg, date_iso: str, summary: str) -> Path:
    """Write/overwrite a briefing file: vault/briefings/YYYY-MM-DD.md"""
    path = _vault_root(cfg) / "briefings" / f"{date_iso}.md"
    front = _frontmatter({"date": date_iso, "kind": "morning_briefing"})
    content = f"{front}\n# Morning briefing — {date_iso}\n\n{summary.strip()}\n"
    _atomic_write(path, content)
    log.debug("vault: wrote %s", path)
    return path


# ---------------------------------------------------------------------------
# Follow-ups (one rolling file)
# ---------------------------------------------------------------------------


def write_followups(cfg, open_followups: list[dict]) -> Path:
    """Regenerate vault/follow-ups.md with current open follow-ups."""
    tz = ZoneInfo(getattr(cfg, "timezone", "UTC"))
    now = datetime.now(tz)
    path = _vault_root(cfg) / "follow-ups.md"
    front = _frontmatter({
        "kind": "follow_ups_index",
        "generated_at": now.isoformat(timespec="seconds"),
        "open_count": len(open_followups),
    })
    lines = [f"# Open follow-ups ({len(open_followups)})", ""]
    if not open_followups:
        lines.append("_None._")
    for fu in open_followups:
        due_ts = fu.get("due_at")
        if due_ts:
            due = datetime.fromtimestamp(due_ts, tz=tz)
            due_str = due.strftime("%Y-%m-%d %H:%M")
        else:
            due_str = "no due"
        body = (fu.get("body") or "").strip().split("\n")[0][:120]
        reopens = fu.get("reopened_count") or 0
        reopen_str = f" · reopens={reopens}" if reopens else ""
        promised = f" · to {fu['promised_to']}" if fu.get("promised_to") else ""
        lines.append(
            f"- [[notes/note-{fu['note_id']:05d}|#{fu['note_id']}]] "
            f"{body} (due {due_str}{promised}{reopen_str})"
        )
    _atomic_write(path, front + "\n" + "\n".join(lines) + "\n")
    log.debug("vault: wrote %s", path)
    return path


# ---------------------------------------------------------------------------
# Bulk backfill
# ---------------------------------------------------------------------------


async def backfill_from_db(db, cfg) -> dict:
    """Walk the DB and emit every note + briefing + open follow-up.
    Used once to populate the vault for an existing TARS install."""
    note_rows = await db.fetch_all(
        "SELECT id, created_at, source, body, tags, status FROM notes ORDER BY id"
    )
    for r in note_rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        write_note(
            cfg,
            note_id=int(r["id"]),
            body=r["body"] or "",
            tags=tags,
            source=r["source"] or "agent",
            created_at=int(r["created_at"]) if r["created_at"] else None,
            status=r["status"] or "note",
        )

    briefing_rows = await db.fetch_all(
        "SELECT date, summary FROM briefings ORDER BY date"
    )
    for r in briefing_rows:
        write_briefing(cfg, r["date"], r["summary"] or "")

    from tars.memory.follow_ups import list_open
    fus = await list_open(db, limit=200)
    write_followups(cfg, fus)

    return {
        "notes": len(note_rows),
        "briefings": len(briefing_rows),
        "open_followups": len(fus),
    }
