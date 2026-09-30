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

-- What the dry run decided, so an operator can see why long after the catalog, the extractor or the Slack
-- thread changed: the outcome, the extractor's name and version, and the result as JSON (the candidate
-- movements, every match with its rule and ids, and the review reasons). The result quotes words of the
-- post, so retention expires it like a raw body: it can only become NULL.
CREATE TABLE dry_runs (
    post_id INTEGER PRIMARY KEY REFERENCES posts (id),
    outcome TEXT NOT NULL CHECK (outcome IN ('replied', 'silent', 'withheld', 'failed', 'gave_up')),
    extractor TEXT CHECK (extractor IS NULL OR extractor <> ''),
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER dry_runs_keep_record BEFORE UPDATE OF post_id, outcome, extractor, created_at ON dry_runs
BEGIN SELECT RAISE(ABORT, 'a dry-run record never changes'); END;
CREATE TRIGGER dry_runs_result_only_expires BEFORE UPDATE OF result ON dry_runs
WHEN NEW.result IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a dry-run result is never rewritten, only expired'); END;
CREATE TRIGGER dry_runs_no_delete BEFORE DELETE ON dry_runs
BEGIN SELECT RAISE(ABORT, 'dry-run records are never deleted'); END;
CREATE TRIGGER dry_runs_no_replace BEFORE INSERT ON dry_runs
WHEN EXISTS (SELECT 1 FROM dry_runs WHERE post_id = NEW.post_id)
BEGIN SELECT RAISE(ABORT, 'a post has one dry-run record'); END;

-- Messages that were edited or deleted, by message key: ids only. Slack does not keep event order, so an
-- edit can arrive before the message it changes. The worker notes every edit and deletion here and never
-- reads a post that holds a noted message from its original text.
CREATE TABLE revised_messages (
    id INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL CHECK (workspace_id <> ''),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    ts TEXT NOT NULL CHECK (ts <> ''),
    slack_event_id INTEGER REFERENCES slack_events (id),
    noted_at TEXT NOT NULL,
    UNIQUE (workspace_id, channel_id, ts)
) STRICT;

CREATE TRIGGER revised_messages_no_update BEFORE UPDATE ON revised_messages
BEGIN SELECT RAISE(ABORT, 'a noted revision never changes'); END;
CREATE TRIGGER revised_messages_no_delete BEFORE DELETE ON revised_messages
BEGIN SELECT RAISE(ABORT, 'noted revisions are never deleted'); END;
CREATE TRIGGER revised_messages_no_replace BEFORE INSERT ON revised_messages
WHEN EXISTS (
    SELECT 1 FROM revised_messages
    WHERE id = NEW.id OR (workspace_id = NEW.workspace_id AND channel_id = NEW.channel_id AND ts = NEW.ts)
)
BEGIN SELECT RAISE(ABORT, 'a revision is noted once per message'); END;
