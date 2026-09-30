-- The worker (design section 4, "Worker"): from a queued Slack event to a dry-run reply in the thread.
--
-- A grouped message keeps a link to the stored event it came from, so the worker reads its text from the
-- raw body; message text is still stored only there. A post gets its dry-run outcome once: when it was
-- handled, and the timestamp of the reply, or NULL when the post got no reply (declined, not a movement).
-- Posts from before this migration count as handled, so an upgrade never replies to old posts.

ALTER TABLE post_messages ADD COLUMN slack_event_id INTEGER REFERENCES slack_events (id);

ALTER TABLE posts ADD COLUMN dry_run_at TEXT;
ALTER TABLE posts ADD COLUMN reply_ts TEXT CHECK (reply_ts IS NULL OR reply_ts <> '');

UPDATE posts SET dry_run_at = created_at;

CREATE INDEX posts_waiting_for_dry_run ON posts (id) WHERE dry_run_at IS NULL;

CREATE TRIGGER posts_dry_run_once BEFORE UPDATE OF dry_run_at, reply_ts ON posts
WHEN OLD.dry_run_at IS NOT NULL OR NEW.dry_run_at IS NULL
BEGIN SELECT RAISE(ABORT, 'a post gets its dry-run outcome once'); END;
