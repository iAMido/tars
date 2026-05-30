"""RSS feed integration. feedparser is sync; we wrap in asyncio.to_thread
so the bot's event loop never blocks on a slow feed.

Adds new items to feed_items keyed on (feed_id, guid). last_seen_guid on the
feeds row points at the newest item we've stored; on the next fetch we walk
entries from the top and stop when we hit it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import feedparser

log = logging.getLogger("tars.integrations.news")

ITEM_SUMMARY_MAX = 800
FETCH_TIMEOUT_SECONDS = 20


def _entry_guid(entry: dict) -> str:
    """feedparser entries have id OR link; fall back to title hash if neither."""
    return (
        entry.get("id")
        or entry.get("link")
        or entry.get("title", "")[:200]
    )


def _entry_published_unix(entry: dict) -> int | None:
    """Try entry.published_parsed -> updated_parsed -> None."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t is None:
        return None
    try:
        return int(datetime(*t[:6], tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _entry_summary(entry: dict) -> str:
    raw = entry.get("summary") or entry.get("description") or ""
    # feedparser sometimes returns dict with .value
    if isinstance(raw, dict):
        raw = raw.get("value") or ""
    # Strip HTML tags crudely (full sanitization not needed for an indexable summary).
    import re as _re
    text = _re.sub(r"<[^>]+>", " ", raw)
    text = _re.sub(r"\s+", " ", text).strip()
    return text[:ITEM_SUMMARY_MAX]


def _parse_sync(feed_url: str) -> dict:
    # feedparser handles ETag/Last-Modified caching internally if we pass them;
    # for simplicity we don't.
    return feedparser.parse(feed_url, request_headers={"User-Agent": "TARS/1.0"})


async def fetch_feed(feed_url: str) -> dict:
    """Returns the raw feedparser result. Logs and re-raises on transport errors."""
    try:
        parsed = await asyncio.wait_for(
            asyncio.to_thread(_parse_sync, feed_url),
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("feed timeout: %s", feed_url)
        raise
    if parsed.bozo and parsed.entries == []:
        log.warning("feed parse warning for %s: %s", feed_url, getattr(parsed, "bozo_exception", "?"))
    return parsed


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def list_feeds(db, kind: str | None = None, enabled_only: bool = True) -> list[dict]:
    sql = "SELECT id, name, feed_url, kind, enabled, last_seen_guid, last_run_at, notes FROM feeds"
    args: tuple = ()
    where = []
    if kind is not None:
        where.append("kind = ?")
        args = args + (kind,)
    if enabled_only:
        where.append("enabled = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    rows = await db.fetch_all(sql, args)
    return [dict(r) for r in rows]


async def add_feed(db, name: str, feed_url: str, kind: str = "news",
                   notes: str | None = None) -> int:
    cur = await db.execute(
        "INSERT OR IGNORE INTO feeds(name, feed_url, kind, notes) VALUES (?, ?, ?, ?)",
        (name, feed_url, kind, notes),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = await db.fetch_one("SELECT id FROM feeds WHERE feed_url = ?", (feed_url,))
    return int(row["id"]) if row else 0


async def remove_feed(db, feed_id: int) -> bool:
    """Hard-delete a feed and all its items. Returns True if a row was removed."""
    row = await db.fetch_one("SELECT id FROM feeds WHERE id = ?", (int(feed_id),))
    if row is None:
        return False
    await db.execute("DELETE FROM feed_items WHERE feed_id = ?", (int(feed_id),))
    await db.execute("DELETE FROM feeds WHERE id = ?", (int(feed_id),))
    return True


async def set_feed_enabled(db, feed_id: int, enabled: bool) -> bool:
    row = await db.fetch_one("SELECT id FROM feeds WHERE id = ?", (int(feed_id),))
    if row is None:
        return False
    await db.execute(
        "UPDATE feeds SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, int(feed_id)),
    )
    return True


# ---------------------------------------------------------------------------
# Refresh a single feed
# ---------------------------------------------------------------------------


async def refresh_feed(db, feed: dict) -> list[dict]:
    """Fetch the feed; insert any entries newer than last_seen_guid; advance
    last_seen_guid; return the list of newly-stored items."""
    parsed = await fetch_feed(feed["feed_url"])
    entries = parsed.entries or []
    if not entries:
        return []

    last_seen = feed.get("last_seen_guid")
    new_items: list[dict] = []
    for entry in entries:
        guid = _entry_guid(entry)
        if not guid:
            continue
        if guid == last_seen:
            break  # caught up
        new_items.append(
            {
                "guid": guid,
                "title": (entry.get("title") or "").strip()[:300],
                "url": entry.get("link") or "",
                "summary": _entry_summary(entry),
                "published_at": _entry_published_unix(entry),
            }
        )

    now = int(time.time())
    for it in new_items:
        await db.execute(
            "INSERT OR IGNORE INTO feed_items("
            " feed_id, guid, title, url, summary, published_at, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                feed["id"], it["guid"], it["title"], it["url"],
                it["summary"], it["published_at"], now,
            ),
        )

    # Advance last_seen_guid even if we stored nothing (feed empty or all caught up).
    if entries:
        newest_guid = _entry_guid(entries[0])
        await db.execute(
            "UPDATE feeds SET last_seen_guid = ?, last_run_at = ? WHERE id = ?",
            (newest_guid, now, feed["id"]),
        )

    log.info("feed refreshed: id=%d name=%r new=%d", feed["id"], feed["name"], len(new_items))
    return new_items


async def refresh_all(db, kind: str | None = None) -> dict:
    """Refresh every enabled feed of the given kind. Returns aggregate counts."""
    feeds = await list_feeds(db, kind=kind, enabled_only=True)
    total_new = 0
    per_feed: list[dict] = []
    for f in feeds:
        try:
            new = await refresh_feed(db, f)
        except Exception as e:  # noqa: BLE001
            log.warning("feed %s failed: %s", f["name"], e)
            per_feed.append({"name": f["name"], "error": str(e)})
            continue
        per_feed.append({"name": f["name"], "new": len(new)})
        total_new += len(new)
    return {"feeds": len(feeds), "total_new": total_new, "per_feed": per_feed}
