-- Every edit and deletion is noted, not only the first of each message (design section 4, "Plan revisions"):
-- a revision reads a message as its newest edit shows it, so a second edit must start a revision too.
--
-- `revised_messages` loses its one-row-per-message key and gets one row per Slack event instead. Ids are
-- kept, so the `changes_through` of every dry run still names the same change, and they only grow: a row is
-- never deleted. A note without an event (none are made from events today) is still kept once per message;
-- the store checks that. SQLite cannot drop a table constraint, so the table is rebuilt, and the two
-- dry-run triggers that read it are dropped first and made again unchanged.
--
-- Before this migration a second edit of a message was not noted, so the note of a message from before it
-- may not be its newest edit. `revised_messages_before` keeps the newest id noted then: the worker never
-- reads a message with such a note, and withholds its post as changed instead.

DROP TRIGGER dry_runs_changes_through_given;
DROP TRIGGER dry_run_revisions_in_order;

CREATE TABLE revised_messages_new (
    id INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL CHECK (workspace_id <> ''),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    ts TEXT NOT NULL CHECK (ts <> ''),
    slack_event_id INTEGER REFERENCES slack_events (id),
    noted_at TEXT NOT NULL
) STRICT;

INSERT INTO revised_messages_new (id, workspace_id, channel_id, ts, slack_event_id, noted_at)
SELECT id, workspace_id, channel_id, ts, slack_event_id, noted_at FROM revised_messages;

CREATE TABLE revised_messages_before (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    through INTEGER NOT NULL CHECK (through >= 0)
) STRICT;
INSERT INTO revised_messages_before (id, through) SELECT 1, coalesce(max(id), 0) FROM revised_messages;
CREATE TRIGGER revised_messages_before_kept BEFORE UPDATE ON revised_messages_before
BEGIN SELECT RAISE(ABORT, 'the mark of notes from before migration 0009 never changes'); END;
CREATE TRIGGER revised_messages_before_no_delete BEFORE DELETE ON revised_messages_before
BEGIN SELECT RAISE(ABORT, 'the mark of notes from before migration 0009 never changes'); END;

DROP TABLE revised_messages;
ALTER TABLE revised_messages_new RENAME TO revised_messages;

CREATE INDEX revised_messages_by_message ON revised_messages (workspace_id, channel_id, ts);
CREATE UNIQUE INDEX revised_messages_by_event ON revised_messages (slack_event_id) WHERE slack_event_id IS NOT NULL;

CREATE TRIGGER revised_messages_no_update BEFORE UPDATE ON revised_messages
BEGIN SELECT RAISE(ABORT, 'a noted revision never changes'); END;
CREATE TRIGGER revised_messages_no_delete BEFORE DELETE ON revised_messages
BEGIN SELECT RAISE(ABORT, 'noted revisions are never deleted'); END;
CREATE TRIGGER revised_messages_no_replace BEFORE INSERT ON revised_messages
WHEN EXISTS (SELECT 1 FROM revised_messages WHERE id = NEW.id OR slack_event_id = NEW.slack_event_id)
BEGIN SELECT RAISE(ABORT, 'a revision is noted once per event'); END;

CREATE TRIGGER dry_runs_changes_through_given BEFORE INSERT ON dry_runs
WHEN NEW.changes_through IS NULL OR (
    NEW.changes_through <> 0 AND NOT EXISTS (SELECT 1 FROM revised_messages WHERE id = NEW.changes_through)
)
BEGIN SELECT RAISE(ABORT, 'a dry run knows which changes it was decided with'); END;

CREATE TRIGGER dry_run_revisions_in_order BEFORE INSERT ON dry_run_revisions
WHEN EXISTS (SELECT 1 FROM dry_run_revisions WHERE id = NEW.id)
    OR NEW.revision IS NOT 2 + (SELECT count(*) FROM dry_run_revisions WHERE post_id = NEW.post_id)
    OR NOT EXISTS (SELECT 1 FROM post_messages WHERE id = NEW.read_through AND post_id = NEW.post_id)
    OR NEW.changes_through IS NULL
    OR (NEW.changes_through <> 0 AND NOT EXISTS (SELECT 1 FROM revised_messages WHERE id = NEW.changes_through))
    OR NOT EXISTS (
        SELECT 1 FROM (
            SELECT coalesce(max(r.read_through), d.read_through) AS read,
                coalesce(max(r.changes_through), d.changes_through) AS changes
            FROM dry_runs d LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id WHERE d.post_id = NEW.post_id
        ) AS last
        WHERE NEW.read_through >= last.read AND NEW.changes_through >= last.changes
            AND (NEW.read_through > last.read OR NEW.changes_through > last.changes)
    )
BEGIN SELECT RAISE(ABORT, 'a revision follows the one before it and reads further or knows of a newer change'); END;
