-- FTS5 virtual table for full-text search over notes, messages, briefings, vault.
-- The companion vec0 virtual table (vec_docs) is created at runtime in db.py
-- because it requires the sqlite-vec extension to be loaded first.

CREATE VIRTUAL TABLE IF NOT EXISTS brain_docs USING fts5(
    doc_id UNINDEXED,
    source UNINDEXED,           -- 'note'|'message'|'briefing'|'vault'
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);
