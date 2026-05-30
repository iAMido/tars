-- RSS feed sources + cached items.
-- Used by news_sources_refresh (kind='news', hourly) and
-- competitive_intel_scan (kind='competitive', 09/13/17 daily).
--
-- A "feed" is just a URL we'll fetch via feedparser. `kind` differentiates
-- general news from things you actively track (competitors, project repos,
-- specific people's blogs).

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'news',   -- 'news' | 'competitive'
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_guid TEXT,                 -- newest entry id we've already stored
    last_run_at INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_feeds_kind ON feeds(kind, enabled);

-- Items fetched from feeds. Indexed into brain_docs by the next reindex.
CREATE TABLE IF NOT EXISTS feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
    guid TEXT NOT NULL,                  -- entry.id or entry.link
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    published_at INTEGER,                -- unix ts if entry.published parses
    fetched_at INTEGER NOT NULL,
    UNIQUE(feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_feed_items_fetched ON feed_items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_items_published ON feed_items(published_at DESC);
