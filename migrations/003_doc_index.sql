-- doc_index: stable integer doc_id for the FTS5 brain_docs and vec0 vec_docs tables.
--
-- Both virtual tables key on an INTEGER PRIMARY KEY. We need a mapping from
-- (source, source_ref) -> doc_id so reindexing notes/messages/briefings reuses
-- the same id across reruns (so DELETE+INSERT updates work, and KNN/FTS
-- results can be joined back to their source).
--
-- source       : 'note' | 'message' | 'briefing' | 'vault'
-- source_ref   : opaque per-source identifier ('123' for note id 123, etc.)
-- indexed_at   : unix ts of last reindex; useful for incremental work later
-- body_hash    : sha256 of the indexed body so we can skip re-embedding
--                unchanged docs (Phase 6 optimization; ignored in Phase 4)

CREATE TABLE IF NOT EXISTS doc_index (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    indexed_at INTEGER,
    body_hash TEXT,
    UNIQUE(source, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_doc_index_source ON doc_index(source);
