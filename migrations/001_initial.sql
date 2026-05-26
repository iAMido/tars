-- TARS schema v1. Core tables: conversations, messages, notes, entities,
-- entity_aliases, follow_ups, briefings, jobs, cost_ledger.
-- See PLAN.md §6.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    thread_key TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    meta TEXT  -- JSON blob; sqlite stores as TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    thread_key TEXT NOT NULL REFERENCES conversations(thread_key),
    ts INTEGER NOT NULL,
    role TEXT NOT NULL,        -- system|user|assistant|tool
    content TEXT NOT NULL,
    tool_calls TEXT,           -- JSON
    cost_usd REAL DEFAULT 0,
    model TEXT,
    tier TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_ts ON messages(thread_key, ts);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    source TEXT NOT NULL,           -- 'telegram'|'voice'|'briefing'|'manual'
    body TEXT NOT NULL,
    tags TEXT DEFAULT '[]',         -- JSON array
    entities TEXT DEFAULT '[]',     -- JSON array
    status TEXT DEFAULT 'note',     -- 'note'|'open'|'closed'
    closes_note_id INTEGER REFERENCES notes(id),
    closed_at INTEGER,
    ext_path TEXT                   -- mirror path in vault, optional
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    canonical TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,             -- 'person'|'org'|'project'|'product'|'domain'
    meta TEXT                       -- JSON
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias TEXT PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id)
);

CREATE TABLE IF NOT EXISTS follow_ups (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    promised_to TEXT,
    due_at INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    reopened_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY,
    date TEXT UNIQUE NOT NULL,      -- yyyy-mm-dd
    summary TEXT NOT NULL,
    payload TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    last_run INTEGER,
    last_status TEXT,
    last_duration_ms INTEGER,
    next_run INTEGER
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT,
    job_id TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_ledger(ts);
