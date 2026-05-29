-- Generic scheduler key/value state. Used by:
--   - email_summary: last_seen_ts (when we last checked Gmail)
--   - email_summary: last_summary_ts (avoid spamming if user reads slowly)
--   - email_summary: suppress_until_ts (if user said "stop")
-- More keys added by other jobs as needed. No structure — just a dictionary.

CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
