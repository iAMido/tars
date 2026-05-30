-- Daily cost rollup table. Populated by cost_rollup_daily at midnight.
-- Faster than running aggregate queries over cost_ledger every dashboard load.

CREATE TABLE IF NOT EXISTS cost_rollups (
    date TEXT PRIMARY KEY,           -- yyyy-mm-dd local time
    total_usd REAL NOT NULL,
    by_tier TEXT,                    -- JSON {tier: {cost, calls, prompt, completion, cached}}
    by_model TEXT,                   -- JSON {model: cost}
    calls INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    generated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_rollups_date ON cost_rollups(date DESC);
