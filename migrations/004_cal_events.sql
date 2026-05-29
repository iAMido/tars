-- Calendar events cache. Populated by the calendar_pull job (Phase 6).
-- Keyed on iCalUID so updates to the same event upsert cleanly.

CREATE TABLE IF NOT EXISTS cal_events (
    ical_uid TEXT PRIMARY KEY,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    title TEXT NOT NULL,
    attendees TEXT,      -- JSON array
    location TEXT,
    payload TEXT,        -- raw event JSON for ad-hoc reads
    fetched_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_start ON cal_events(start_ts);
