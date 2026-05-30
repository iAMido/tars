-- Track inline-keyboard prompts that are waiting for the user's text reply.
-- Used by the "⏰ Custom" button: bot sends a "When?" message, user replies
-- with a time string, bot then creates the follow-up via the Agent.

ALTER TABLE pending_actions ADD COLUMN prompt_message_id INTEGER;
ALTER TABLE pending_actions ADD COLUMN awaiting_kind TEXT;
