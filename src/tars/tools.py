"""Tool implementations called by the Agent's tool loop.

Status:
  - save_note: real (writes to notes table + fires entity extraction)
  - search_memory: real (FTS5 + vec0 hybrid + Voyage rerank + alias expansion)
  - open_followup / close_followup / list_followups: real (Phase 5)
  - get_current_time: real (Phase 5 — supports relative due dates)
  - web_research: stub (the gpt-5:online tier handles live RAG for /research)

Each tool function takes (db, args_dict) and returns a JSON-serializable result
(a string that gets fed back to the model as the tool's content).

The Embedder is constructed lazily and cached on the db handle so it survives
across calls but doesn't need to be wired through every signature.
"""

from __future__ import annotations

import json
import logging
import os as _os
import re as _re
import tempfile as _tempfile
import time
from datetime import datetime
from pathlib import Path as _Path
from typing import Any
from zoneinfo import ZoneInfo

from tars.memory import entities as entities_mod
from tars.memory import follow_ups as fu_mod
from tars.memory.embed import Embedder
from tars.memory.search import hybrid_search

log = logging.getLogger("tars.tools")


def _get_embedder(db, cfg) -> Embedder:
    """Lazily attach an Embedder to the db handle (idempotent)."""
    cached = getattr(db, "_embedder", None)
    if cached is not None:
        return cached
    e = Embedder(api_key=cfg.voyage.api_key)
    db._embedder = e
    return e


async def save_note(db, args: dict[str, Any]) -> str:
    body = (args.get("body") or "").strip()
    if not body:
        return json.dumps({"error": "empty note body"})
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags_json = json.dumps(tags)
    cur = await db.execute(
        "INSERT INTO notes(created_at, source, body, tags) VALUES (?, ?, ?, ?)",
        (int(time.time()), "agent", body, tags_json),
    )
    note_id = cur.lastrowid

    # Index the new note immediately so search_memory can find it on the very
    # next call. Failures are logged but never block the save.
    cfg = getattr(db, "_cfg", None)
    try:
        if cfg is not None:
            from tars.memory.index import index_single_doc  # local import: avoid cycle on load
            embedder = _get_embedder(db, cfg)
            await index_single_doc(
                db, embedder,
                source="note",
                source_ref=str(note_id),
                title=body[:60],
                body=body,
                tags=tags_json,
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "save_note: live index failed (%s); note saved as #%s, next reindex picks it up",
            e, note_id,
        )

    # Fire-and-forget entity extraction. Runs in background; never blocks the
    # save_note response. Failures are logged inside the helper.
    if cfg is not None and note_id is not None:
        entities_mod.schedule_extraction(db, cfg, int(note_id), body)

    # Mirror the note into the vault directory (markdown for Obsidian).
    # Failures here are also non-fatal — the note is already in SQLite.
    if cfg is not None and note_id is not None:
        try:
            from tars.integrations.vault import write_note as _vault_write
            _vault_write(
                cfg, note_id=int(note_id), body=body,
                tags=tags, source="agent", status="note",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vault mirror failed for note %s: %s", note_id, e)

    return json.dumps({"ok": True, "note_id": note_id})


async def get_note(db, args: dict[str, Any]) -> str:
    """Fetch a single note by its exact id."""
    try:
        nid = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "get_note requires integer note_id"})
    row = await db.fetch_one(
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       source, status, body, tags, closes_note_id, "
        "       datetime(closed_at,'unixepoch','localtime') AS closed_at "
        "FROM notes WHERE id = ?",
        (nid,),
    )
    if row is None:
        return json.dumps({"error": f"note #{nid} does not exist"})
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return json.dumps(
        {
            "id": int(row["id"]),
            "created": row["created"],
            "source": row["source"],
            "status": row["status"],
            "body": row["body"],
            "tags": tags,
            "closes_note_id": row["closes_note_id"],
            "closed_at": row["closed_at"],
        },
        ensure_ascii=False,
    )


async def list_notes(db, args: dict[str, Any]) -> str:
    """List the user's notes by recency. Use for 'show me my notes',
    'what did I note today/this week', 'last N notes', etc. — anything that
    asks for a LIST rather than a semantic SEARCH.

    Args:
      limit:      max notes to return (default 20, max 100)
      since_days: only notes from the last N days (omit = no time filter)
      tag:        only notes whose tags JSON contains this string (optional)
      include_closed: include status='closed' rows (default false)
    """
    try:
        limit = min(int(args.get("limit") or 20), 100)
    except (TypeError, ValueError):
        limit = 20
    since_days = args.get("since_days")
    tag = (args.get("tag") or "").strip()
    include_closed = bool(args.get("include_closed"))

    where = ["status != 'deleted'"]
    params: list[Any] = []
    if not include_closed:
        where.append("status != 'closed'")
    if since_days:
        try:
            cutoff = int(time.time()) - int(since_days) * 86400
            where.append("created_at >= ?")
            params.append(cutoff)
        except (TypeError, ValueError):
            pass
    if tag:
        where.append("tags LIKE ?")
        params.append(f"%{tag}%")
    params.append(limit)
    sql = (
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       source, status, body, tags "
        "FROM notes WHERE " + " AND ".join(where) + " "
        "ORDER BY created_at DESC LIMIT ?"
    )
    rows = await db.fetch_all(sql, tuple(params))
    out = []
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        out.append({
            "id": int(r["id"]),
            "note_id": int(r["id"]),  # alias so the guardrail regex picks it up too
            "created": r["created"],
            "source": r["source"],
            "status": r["status"],
            "preview": (r["body"] or "").split("\n")[0][:200],
            "tags": tags,
        })
    return json.dumps({"notes": out, "count": len(out)}, ensure_ascii=False)


async def delete_note(db, args: dict[str, Any]) -> str:
    """Soft-delete a TARS note by id. Marks status='deleted' in the DB
    (the row stays for audit) and removes the corresponding vault file at
    _TARS/notes/note-NNNNN.md. Searches and lists filter deleted rows out.

    Use when the user explicitly says \"delete note N\", \"remove note N\",
    \"erase note N\". The action is essentially equivalent to deleting the
    file in Obsidian — same end state, faster path.
    """
    try:
        nid = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "delete_note requires integer note_id"})
    row = await db.fetch_one(
        "SELECT id, status FROM notes WHERE id = ?", (nid,),
    )
    if row is None:
        return json.dumps({"error": f"note #{nid} does not exist"})
    if row["status"] == "deleted":
        return json.dumps({"ok": True, "note_id": nid, "already_deleted": True})

    # Refuse to delete notes that are the SOURCE of an open follow-up — that
    # would silently orphan the follow-up. Force the user to close it first.
    fu_row = await db.fetch_one(
        "SELECT id FROM follow_ups WHERE note_id = ? AND status = 'open' LIMIT 1",
        (nid,),
    )
    if fu_row is not None:
        return json.dumps({
            "error": (
                f"note #{nid} is the source of open follow-up "
                f"#{int(fu_row['id'])}. Close the follow-up first "
                f"(close_followup) or it will orphan."
            ),
        })

    await db.execute(
        "UPDATE notes SET status = 'deleted' WHERE id = ?", (nid,),
    )

    # Remove the vault file if present (best-effort — DB is source of truth).
    file_removed = False
    cfg = getattr(db, "_cfg", None)
    if cfg is not None:
        try:
            file_path = _Path(cfg.paths.vault) / "_TARS" / "notes" / f"note-{nid:05d}.md"
            if file_path.exists():
                file_path.unlink()
                file_removed = True
        except Exception as e:  # noqa: BLE001
            log.warning("delete_note: file unlink failed for %d (%s)", nid, e)

    log.info("delete_note: note=%d file_removed=%s", nid, file_removed)
    return json.dumps({
        "ok": True,
        "note_id": nid,
        "file_removed": file_removed,
    })


_TODO_OPEN_RE = _re.compile(r"^(\s*)-\s*\[ \]\s+(.+?)\s*$", _re.MULTILINE)
_NOTE_BACKLINK_RE = _re.compile(r"\[\[note-(\d{1,8})\]\]")


def _vault_para_md_files(cfg) -> list[_Path]:
    """Walk PARA folders under the vault root, yield .md files. Excludes
    dotfolders, _TARS, _Templates."""
    out: list[_Path] = []
    try:
        vault = _Path(cfg.paths.vault)
    except Exception:  # noqa: BLE001
        return out
    for folder in sorted(_PARA_FOLDERS):
        root = vault / folder
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            # skip dot-prefixed parts
            if any(part.startswith(".") for part in p.parts):
                continue
            out.append(p)
    return out


async def list_open_todos(db, args: dict[str, Any]) -> str:
    """Scan PARA markdown files for open checkbox items (`- [ ] X`).
    Excludes completed items (`- [x]`). Use when the user asks
    \"what's on my todo list?\", \"open todos?\", \"what work is pending?\".

    Args:
      paths:         optional list of EXPLICIT vault-relative .md paths to
                     scan (e.g. ['01_Projects/Work/work_to_dos.md']). When
                     present, only these files are read. Highest priority.
      folder:        optional vault-relative folder filter (e.g. '01_Projects/Work').
                     Ignored if `paths` is given.
      max_per_file:  per-file cap on items returned (default 10)
      max_total:     overall cap (default 50)
    """
    cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "vault unavailable: cfg not bound"})
    paths_arg = args.get("paths") or None
    folder_filter = (args.get("folder") or "").strip().replace("\\", "/")
    try:
        max_per = max(1, min(int(args.get("max_per_file") or 10), 50))
    except (TypeError, ValueError):
        max_per = 10
    try:
        max_total = max(1, min(int(args.get("max_total") or 50), 200))
    except (TypeError, ValueError):
        max_total = 50

    vault_root = _Path(cfg.paths.vault).resolve()

    # File source: explicit paths beat folder filter beats full vault walk.
    files: list[_Path] = []
    if isinstance(paths_arg, list) and paths_arg:
        for raw in paths_arg:
            if not isinstance(raw, str):
                continue
            abs_path, err = _resolve_vault_path(cfg, raw)
            if err or not abs_path or not abs_path.exists():
                continue
            files.append(abs_path)
    else:
        files = _vault_para_md_files(cfg)

    results: list[dict] = []
    total = 0
    for p in files:
        try:
            rel = str(p.resolve().relative_to(vault_root)).replace("\\", "/")
        except ValueError:
            continue
        if not paths_arg and folder_filter and not rel.startswith(folder_filter):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        matches = _TODO_OPEN_RE.findall(content)
        if not matches:
            continue
        items = [m[1].strip() for m in matches[:max_per]]
        results.append({
            "path": rel,
            "open_count": len(matches),
            "items_shown": len(items),
            "items": items,
        })
        total += len(items)
        if total >= max_total:
            break
    # Most-todo files first.
    results.sort(key=lambda r: r["open_count"], reverse=True)
    grand_total = sum(r["open_count"] for r in results)
    return json.dumps({
        "total_open": grand_total,
        "files_with_todos": len(results),
        "files": results,
    }, ensure_ascii=False)


async def list_un_promoted_notes(db, args: dict[str, Any]) -> str:
    """List TARS-captured notes (last N days) that don't yet have a
    `[[note-NNNNN]]` backlink in any PARA file. The user's \"to triage\"
    inbox view — notes they haven't yet decided to file as projects.

    Excludes auto-generated reminder-close notes (tagged source/reminder-ping).
    Use when the user asks \"what hasn't been filed?\", \"what's un-triaged?\",
    \"what still needs to be promoted?\".

    Args:
      since_days:  only consider notes from the last N days (default 14)
      limit:       max notes to return (default 20, cap 100)
    """
    cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "vault unavailable: cfg not bound"})
    try:
        since_days = max(1, int(args.get("since_days") or 14))
    except (TypeError, ValueError):
        since_days = 14
    try:
        limit = max(1, min(int(args.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20

    cutoff = int(time.time()) - since_days * 86400

    # Collect candidate notes from DB.
    rows = await db.fetch_all(
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       body, tags "
        "FROM notes "
        "WHERE created_at >= ? "
        "  AND status NOT IN ('deleted', 'closed') "
        "  AND source = 'agent' "
        "  AND (tags IS NULL OR tags NOT LIKE '%source/reminder-ping%') "
        "ORDER BY id DESC LIMIT ?",
        (cutoff, limit * 3),  # over-fetch since we'll filter
    )
    if not rows:
        return json.dumps({"total_un_promoted": 0, "notes": []})

    # Build the set of note-ids referenced by PARA files.
    files = _vault_para_md_files(cfg)
    referenced: set[int] = set()
    for p in files:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in _NOTE_BACKLINK_RE.finditer(content):
            try:
                referenced.add(int(m.group(1)))
            except ValueError:
                pass

    un_promoted: list[dict] = []
    for r in rows:
        nid = int(r["id"])
        if nid in referenced:
            continue
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        un_promoted.append({
            "id": nid,
            "note_id": nid,
            "created": r["created"],
            "preview": (r["body"] or "").strip().split("\n")[0][:200],
            "tags": tags,
        })
        if len(un_promoted) >= limit:
            break

    return json.dumps({
        "total_un_promoted": len(un_promoted),
        "since_days": since_days,
        "notes": un_promoted,
    }, ensure_ascii=False)


_ACTION_VERBS = (
    "call", "send", "email", "reply", "schedule", "book", "buy", "order",
    "review", "read", "watch", "research", "investigate", "build", "fix",
    "refactor", "deploy", "test", "check", "verify", "ask", "follow up",
    "draft", "write", "edit", "update", "upgrade", "renew", "cancel",
    "pay", "transfer", "remind", "prepare", "plan", "design",
)


def _promotability_score(body: str, tags: list) -> int:
    """Heuristic 0-10 score on how worth promoting a TARS note is. Higher
    means more likely to be project-shaped (rather than a one-line
    capture)."""
    if not body:
        return 0
    s = 0
    blen = len(body)
    if blen >= 80:
        s += 1
    if blen >= 200:
        s += 2
    if blen >= 600:
        s += 1
    # Multi-line content
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) >= 3:
        s += 1
    if len(lines) >= 6:
        s += 1
    # Action verb presence
    lower = body.lower()
    if any(v in lower for v in _ACTION_VERBS):
        s += 2
    # Mentions a date or money
    if _re.search(r"\b\d{4}-\d{2}-\d{2}\b", body) or _re.search(
        r"[\$£€₪]\s?\d", body,
    ):
        s += 1
    # Has actual checklist or bullets
    if _re.search(r"^\s*[-*]\s", body, _re.MULTILINE):
        s += 1
    # Tags hint at intent
    if tags:
        s += 1
    return min(s, 10)


async def suggest_promotions(db, args: dict[str, Any]) -> str:
    """Score un-promoted TARS notes (last N days) and return the top K
    most-promotable ones. Use when the user asks 'what should I file?',
    'what's worth promoting?', or the briefing's *Triage* section.

    Builds on list_un_promoted_notes — same backlink-detection logic —
    but adds a promotability score and ranks. Excludes anything scoring
    below `min_score` (default 4).

    Args:
      since_days: only consider notes from last N days (default 14)
      limit:      max suggestions to return (default 3, cap 10)
      min_score:  exclude notes scoring below this (default 4 / 10)
    """
    cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "vault unavailable: cfg not bound"})
    try:
        since_days = max(1, int(args.get("since_days") or 14))
    except (TypeError, ValueError):
        since_days = 14
    try:
        limit = max(1, min(int(args.get("limit") or 3), 10))
    except (TypeError, ValueError):
        limit = 3
    try:
        min_score = max(0, min(int(args.get("min_score") or 4), 10))
    except (TypeError, ValueError):
        min_score = 4

    cutoff = int(time.time()) - since_days * 86400

    rows = await db.fetch_all(
        "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
        "       body, tags "
        "FROM notes "
        "WHERE created_at >= ? "
        "  AND status NOT IN ('deleted', 'closed') "
        "  AND source = 'agent' "
        "  AND (tags IS NULL OR tags NOT LIKE '%source/reminder-ping%') "
        "ORDER BY id DESC LIMIT ?",
        (cutoff, limit * 5),
    )
    if not rows:
        return json.dumps({"suggestions": [], "total": 0})

    files = _vault_para_md_files(cfg)
    referenced: set[int] = set()
    for p in files:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in _NOTE_BACKLINK_RE.finditer(content):
            try:
                referenced.add(int(m.group(1)))
            except ValueError:
                pass

    scored: list[dict] = []
    for r in rows:
        nid = int(r["id"])
        if nid in referenced:
            continue
        body = r["body"] or ""
        try:
            tags = json.loads(r["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        score = _promotability_score(body, tags)
        if score < min_score:
            continue
        scored.append({
            "id": nid,
            "note_id": nid,
            "created": r["created"],
            "preview": body.strip().split("\n")[0][:160],
            "tags": tags,
            "score": score,
        })

    scored.sort(key=lambda x: (-x["score"], -x["id"]))
    suggestions = scored[:limit]
    return json.dumps({
        "since_days": since_days,
        "total": len(suggestions),
        "suggestions": suggestions,
    }, ensure_ascii=False)


async def search_memory(db, args: dict[str, Any], *, cfg=None) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "empty query"})
    k = int(args.get("k") or 8)

    if cfg is None:
        cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "embedder cfg unavailable"})

    embedder = _get_embedder(db, cfg)
    try:
        results = await hybrid_search(db, embedder, query=query, k=k)
    except Exception as e:  # noqa: BLE001
        log.exception("search_memory failed")
        return json.dumps({"error": f"search failed: {e}", "results": []})

    # Strip body in the LLM payload to avoid huge context; keep title + score.
    # The model can ask for a specific doc_id again if it needs the full body.
    summary = [
        {
            "doc_id": r["doc_id"],
            "source": r["source"],
            "title": r["title"],
            "preview": (r["body"] or "")[:300],
            "score": round(r["score"], 4),
        }
        for r in results
    ]
    return json.dumps({"results": summary})


async def open_followup(db, args: dict[str, Any]) -> str:
    try:
        note_id = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "open_followup requires integer note_id"})
    due = args.get("due_at_iso")
    to = args.get("to")
    try:
        fu_id = await fu_mod.open_followup(
            db, note_id=note_id, due_at_iso=due, promised_to=to
        )
    except fu_mod.FollowUpError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"ok": True, "followup_id": fu_id})


async def close_followup(db, args: dict[str, Any]) -> str:
    try:
        fu_id = int(args.get("followup_id"))  # type: ignore[arg-type]
        resolving = int(args.get("resolving_note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps(
            {"error": "close_followup requires integer followup_id and resolving_note_id"}
        )
    try:
        await fu_mod.close_followup(db, fu_id, resolving)
    except fu_mod.FollowUpError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"ok": True, "followup_id": fu_id, "resolving_note_id": resolving})


async def list_followups(db, args: dict[str, Any]) -> str:
    limit = int(args.get("limit") or 20)
    rows = await fu_mod.list_open(db, limit=limit)
    return json.dumps({"open": rows})


async def get_current_time(db, args: dict[str, Any]) -> str:
    """Return current time so the model can compute relative due dates.
    Defaults to the configured TARS timezone (Asia/Jerusalem)."""
    cfg = getattr(db, "_cfg", None)
    tz_name = args.get("timezone") or (cfg.timezone if cfg else "Asia/Jerusalem")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Asia/Jerusalem")
    now = datetime.now(tz)
    return json.dumps(
        {
            "iso": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
            "human": now.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
            "unix": int(now.timestamp()),
            "timezone": str(tz),
        }
    )


async def web_research(db, args: dict[str, Any]) -> str:
    return json.dumps({"status": "not_yet_implemented", "note": "web_research lands in Phase 6+"})


# ---------------------------------------------------------------------------
# Vault tools — promote_note + update_vault_file
# ---------------------------------------------------------------------------
#
# Both tools restrict writes to PARA folders (00_Inbox / 01_Projects /
# 02_Areas / 03_Resources / 04_Archive). _TARS/ is OFF LIMITS — that's the
# TARS-managed area and writes there race with vault_sweep.
#
# vault_sweep ingests new files within 10 min, so anything written here will
# show up as a new note (Path B in the data model) with auto-tagging based
# on its PARA location.

_PARA_FOLDERS = {"00_Inbox", "01_Projects", "02_Areas", "03_Resources", "04_Archive"}


def _slugify(s: str, maxlen: int = 40) -> str:
    """Make a filesystem-friendly slug. ASCII letters/digits + dashes."""
    s = (s or "").strip().lower()
    # Replace non-alphanum with dashes, collapse repeats.
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen] or "untitled"


def _resolve_vault_path(cfg, rel: str) -> tuple[_Path | None, str | None]:
    """Resolve a vault-relative path. Returns (abs_path, error_or_None).

    Hard guards: absolute paths rejected, `..` traversal rejected, result
    must live inside cfg.paths.vault, and the FIRST path segment must be
    one of the PARA folders (not `_TARS`, not `_Templates`, not a dotfile).
    """
    if not rel or not isinstance(rel, str):
        return None, "path is required"
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if ".." in _Path(rel).parts:
        return None, "path traversal forbidden"
    vault_root = _Path(cfg.paths.vault).resolve()
    target = (vault_root / rel).resolve()
    try:
        target.relative_to(vault_root)
    except ValueError:
        return None, "path must be inside the vault"
    parts = _Path(rel).parts
    if not parts:
        return None, "empty path"
    top = parts[0]
    if top not in _PARA_FOLDERS:
        return None, (
            f"top-level folder {top!r} not writable. Use one of: "
            f"{sorted(_PARA_FOLDERS)}"
        )
    return target, None


def _atomic_write_text(path: _Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except Exception:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


async def promote_note(db, args: dict[str, Any]) -> str:
    """Create a PARA file that references an existing TARS note.

    Use for: "turn note 45 into a project file under 01_Projects/Caltrack",
    "promote my brain-dump into 02_Areas/Health".

    Does NOT move or delete the original — that would break follow-up
    linkage. Creates a NEW .md file in the target folder with the body
    plus a `Source: [[note-NNNNN]]` backlink. vault_sweep auto-ingests
    the new file with PARA tags within 10 min.

    Args:
      note_id:      integer id of the TARS note to promote
      dest_folder:  vault-relative, must start with a PARA folder name
                    (e.g. "01_Projects/Caltrack")
      title:        optional override for filename slug. Defaults to first
                    line of the note body.
    """
    cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "vault unavailable: cfg not bound"})
    try:
        nid = int(args.get("note_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return json.dumps({"error": "promote_note requires integer note_id"})
    dest_folder = (args.get("dest_folder") or "").strip()
    if not dest_folder:
        return json.dumps({"error": "dest_folder required (e.g. '01_Projects/Work')"})
    title_arg = (args.get("title") or "").strip()

    row = await db.fetch_one(
        "SELECT id, body, tags FROM notes WHERE id = ? AND status != 'deleted'",
        (nid,),
    )
    if row is None:
        return json.dumps({"error": f"note #{nid} not found"})
    body = (row["body"] or "").strip()

    # Filename: note-NNNNN-<slug>.md. Slug from explicit title or first
    # non-empty line.
    raw_title = title_arg or body.split("\n", 1)[0]
    slug = _slugify(raw_title)
    filename = f"note-{nid:05d}-{slug}.md"

    rel = f"{dest_folder.rstrip('/')}/{filename}"
    abs_path, err = _resolve_vault_path(cfg, rel)
    if err:
        return json.dumps({"error": err})
    if abs_path.exists():
        return json.dumps({
            "error": f"file already exists: {rel}. Pick a different title "
                     f"or update the existing file with update_vault_file."
        })

    content = (
        f"# {raw_title}\n\n"
        f"Source: [[note-{nid:05d}]] (TARS capture)\n\n"
        f"---\n\n"
        f"{body}\n"
    )
    try:
        _atomic_write_text(abs_path, content)
    except Exception as e:  # noqa: BLE001
        log.warning("promote_note write failed: %s", e)
        return json.dumps({"error": f"write failed: {e}"})
    log.info("promote_note: note=%d -> %s", nid, rel)
    return json.dumps({
        "ok": True,
        "note_id": nid,
        "path": rel,
        "byte_size": len(content.encode("utf-8")),
    })


async def update_vault_file(db, args: dict[str, Any]) -> str:
    """Edit a markdown file inside the vault's PARA folders.

    Use for: "add a bullet to projects/Caltrack/issues.md", "mark this
    item done in my plan file", "replace the Plan section with X".

    Restricted to PARA folders (00_Inbox / 01_Projects / 02_Areas /
    03_Resources / 04_Archive). Can NOT touch _TARS/ — that's TARS-managed
    and edits race with vault_sweep.

    Args:
      path:     vault-relative path (e.g. "01_Projects/Caltrack/issues.md").
                Must end in .md.
      mode:     one of: append | prepend | overwrite | replace_section
                  append          — adds <content> at end with a leading blank line
                  prepend         — adds <content> at top (after any frontmatter)
                  overwrite       — replaces entire body (DESTRUCTIVE — use sparingly)
                  replace_section — replaces text under a markdown header
                                    (must specify section)
      content:  the text to write
      section:  required for replace_section. Match against `## <section>`
                or `### <section>` (case-insensitive). Replaces everything
                from that header up to the next same-or-higher level header.
    """
    cfg = getattr(db, "_cfg", None)
    if cfg is None:
        return json.dumps({"error": "vault unavailable: cfg not bound"})
    path = (args.get("path") or "").strip()
    mode = (args.get("mode") or "").strip().lower()
    content = args.get("content") or ""
    if not isinstance(content, str):
        return json.dumps({"error": "content must be a string"})
    if mode not in ("append", "prepend", "overwrite", "replace_section"):
        return json.dumps({
            "error": "mode must be one of: append, prepend, overwrite, replace_section"
        })
    if not path.endswith(".md"):
        return json.dumps({"error": "path must end in .md"})
    abs_path, err = _resolve_vault_path(cfg, path)
    if err:
        return json.dumps({"error": err})

    # Read existing (empty string if file doesn't yet exist — overwrite + create OK).
    existing = ""
    file_exists = abs_path.exists()
    if file_exists:
        try:
            existing = abs_path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"read failed: {e}"})

    new_content: str

    if mode == "append":
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        new_content = existing + sep + content.rstrip() + "\n"
    elif mode == "prepend":
        # Preserve YAML frontmatter at the top if present.
        if existing.startswith("---\n"):
            end = existing.find("\n---\n", 4)
            if end != -1:
                head = existing[: end + 5]
                body = existing[end + 5 :]
                new_content = head + content.rstrip() + "\n\n" + body
            else:
                new_content = content.rstrip() + "\n\n" + existing
        else:
            new_content = content.rstrip() + "\n\n" + existing
    elif mode == "overwrite":
        new_content = content if content.endswith("\n") else content + "\n"
    else:  # replace_section
        section = (args.get("section") or "").strip()
        if not section:
            return json.dumps({"error": "section required for replace_section"})
        if not file_exists:
            return json.dumps({
                "error": f"can't replace_section on a file that doesn't exist: {path}"
            })
        # Find a markdown header matching <section>. Accept ##, ###, ####.
        header_re = _re.compile(
            rf"(?im)^(#{{2,4}})\s+{_re.escape(section)}\s*$"
        )
        m = header_re.search(existing)
        if not m:
            return json.dumps({
                "error": f"section header '## {section}' not found in {path}"
            })
        header_level = len(m.group(1))
        after = existing[m.end():]
        # Find next header at same or higher level (fewer or equal #'s).
        next_pat = _re.compile(
            r"(?im)^(#{1," + str(header_level) + r"})\s+\S"
        )
        nxt = next_pat.search(after)
        if nxt:
            tail = after[nxt.start():]
        else:
            tail = ""
        head = existing[: m.end()]
        body = content.strip() + "\n\n"
        new_content = head + "\n\n" + body + tail
        if not new_content.endswith("\n"):
            new_content += "\n"

    try:
        _atomic_write_text(abs_path, new_content)
    except Exception as e:  # noqa: BLE001
        log.warning("update_vault_file write failed: %s", e)
        return json.dumps({"error": f"write failed: {e}"})
    log.info(
        "update_vault_file: mode=%s path=%s before=%d after=%d",
        mode, path, len(existing), len(new_content),
    )
    return json.dumps({
        "ok": True,
        "path": path,
        "mode": mode,
        "created": not file_exists,
        "byte_size": len(new_content.encode("utf-8")),
    })


TOOL_REGISTRY = {
    "save_note": save_note,
    "get_note": get_note,
    "list_notes": list_notes,
    "delete_note": delete_note,
    "list_open_todos": list_open_todos,
    "list_un_promoted_notes": list_un_promoted_notes,
    "suggest_promotions": suggest_promotions,
    "search_memory": search_memory,
    "open_followup": open_followup,
    "close_followup": close_followup,
    "list_followups": list_followups,
    "get_current_time": get_current_time,
    "web_research": web_research,
    "promote_note": promote_note,
    "update_vault_file": update_vault_file,
}


async def run_tool(db, name: str, args_json: str) -> str:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"bad tool args JSON: {e}"})
    return await fn(db, args)
