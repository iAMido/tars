-- Track when a follow-up's due-time nudge was last sent to Telegram.
-- Used by followup_due_scan (runs every 2 min) to find open follow-ups whose
-- due_at has passed and haven't been pinged yet (or have been re-snoozed
-- since the last ping, in which case last_nudged_at < due_at again).
--
-- Query shape:
--   SELECT ... FROM follow_ups
--   WHERE status = 'open' AND due_at IS NOT NULL AND due_at <= :now
--     AND (last_nudged_at IS NULL OR last_nudged_at < due_at);
--
-- After firing, the scanner sets last_nudged_at = :now so the same row
-- doesn't re-fire on the next 2-min tick. On user-driven snooze, due_at
-- is moved forward; last_nudged_at stays where it is (now < new_due) so
-- the row becomes "not yet nudged again" automatically.

ALTER TABLE follow_ups ADD COLUMN last_nudged_at INTEGER;
CREATE INDEX IF NOT EXISTS idx_followups_due_scan
    ON follow_ups(status, due_at, last_nudged_at);
