-- Per-suggestion action queue. A briefing message with N inline buttons
-- creates N rows. When the user taps a button, we look up the suggestion
-- text from this table and execute the chosen action (save_note,
-- open_followup, or dismiss).
--
-- callback_data is limited to 64 bytes by Telegram, so we encode just the
-- short row id + a single-char action verb. Suggestion text and metadata
-- live here.

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- 'briefing_suggestion' | (future kinds)
    text TEXT NOT NULL,                -- the suggestion body
    extra TEXT,                        -- JSON: tags, hashtags, source briefing date
    created_at INTEGER NOT NULL,
    consumed_at INTEGER,               -- nullable; set when acted on
    consumed_action TEXT,              -- e.g. 'save', 'remind_1d', 'dismiss'
    consumed_result TEXT               -- JSON: {note_id, followup_id, ...}
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_chat ON pending_actions(chat_id, consumed_at);
