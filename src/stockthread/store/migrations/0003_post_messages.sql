-- Grouping (design section 4, "Grouping"): which post each Slack message belongs to.
--
-- A message belongs to exactly one post: the message key is unique, and a row never changes or goes
-- away. Only ids and timestamps are stored, never message text. A post stops taking siblings once its
-- dry-run reply is posted (siblings_closed_at, set once); thread replies still join it after that.

CREATE TABLE post_messages (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    workspace_id TEXT NOT NULL CHECK (workspace_id <> ''),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    ts TEXT NOT NULL CHECK (ts <> ''),
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    thread_ts TEXT CHECK (thread_ts IS NULL OR thread_ts <> ''),
    added_at TEXT NOT NULL,
    UNIQUE (workspace_id, channel_id, ts)
) STRICT;

CREATE INDEX post_messages_by_user ON post_messages (workspace_id, channel_id, user_id);
CREATE INDEX post_messages_by_post ON post_messages (post_id);

ALTER TABLE posts ADD COLUMN siblings_closed_at TEXT;

CREATE TRIGGER post_messages_no_update BEFORE UPDATE ON post_messages
BEGIN SELECT RAISE(ABORT, 'a message never moves to another post'); END;
CREATE TRIGGER post_messages_no_delete BEFORE DELETE ON post_messages
BEGIN SELECT RAISE(ABORT, 'a message never leaves its post'); END;
CREATE TRIGGER post_messages_no_replace BEFORE INSERT ON post_messages
WHEN EXISTS (
    SELECT 1 FROM post_messages
    WHERE id = NEW.id OR (workspace_id = NEW.workspace_id AND channel_id = NEW.channel_id AND ts = NEW.ts)
)
BEGIN SELECT RAISE(ABORT, 'a message belongs to exactly one post'); END;
CREATE TRIGGER post_messages_same_channel BEFORE INSERT ON post_messages
WHEN NOT EXISTS (
    SELECT 1 FROM posts
    WHERE id = NEW.post_id AND workspace_id = NEW.workspace_id AND channel_id = NEW.channel_id
)
BEGIN SELECT RAISE(ABORT, 'a message joins a post in its own channel'); END;
CREATE TRIGGER posts_siblings_close_once BEFORE UPDATE OF siblings_closed_at ON posts
WHEN OLD.siblings_closed_at IS NOT NULL OR NEW.siblings_closed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'a post closes to siblings once, for good'); END;
