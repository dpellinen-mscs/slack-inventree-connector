-- Revised dry runs (design section 4, "Plan revisions"): a message that joins a post after its dry run (a
-- thread reply, or a thread root that catch-up found later) makes the post read again.
--
-- Every dry run keeps `read_through`: the newest post_messages.id it read. A post whose newest message is
-- past that waits for a revision. Existing records read everything their post holds now, so an upgrade never
-- answers a post again. A post handled before migration 0004 has no record and is never revised.
-- The first dry run stays in `dry_runs` (revision 1). Later ones go to `dry_run_revisions`, numbered from 2,
-- each reading further than the one before. Like `dry_runs`, a revision never changes, and only its quoted
-- result can expire.

ALTER TABLE dry_runs ADD COLUMN read_through INTEGER;

UPDATE dry_runs SET read_through = (SELECT max(m.id) FROM post_messages m WHERE m.post_id = dry_runs.post_id);

CREATE TRIGGER dry_runs_read_through_kept BEFORE UPDATE OF read_through ON dry_runs
BEGIN SELECT RAISE(ABORT, 'a dry-run record never changes'); END;
-- From here on every record says what it read: without it, a post could never be revised.
CREATE TRIGGER dry_runs_read_through_given BEFORE INSERT ON dry_runs
WHEN NEW.read_through IS NULL OR NOT EXISTS (
    SELECT 1 FROM post_messages WHERE id = NEW.read_through AND post_id = NEW.post_id
)
BEGIN SELECT RAISE(ABORT, 'a dry run reads messages of its own post'); END;

CREATE TABLE dry_run_revisions (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES dry_runs (post_id),
    revision INTEGER NOT NULL CHECK (revision >= 2),
    read_through INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('replied', 'unchanged', 'retracted', 'silent', 'withheld', 'failed', 'gave_up')
    ),
    extractor TEXT CHECK (extractor IS NULL OR extractor <> ''),
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    reply_ts TEXT CHECK (reply_ts IS NULL OR (reply_ts <> '' AND outcome IN ('replied', 'retracted'))),
    created_at TEXT NOT NULL,
    UNIQUE (post_id, revision)
) STRICT;

CREATE TRIGGER dry_run_revisions_keep_record BEFORE UPDATE ON dry_run_revisions
WHEN NEW.id IS NOT OLD.id OR NEW.post_id IS NOT OLD.post_id OR NEW.revision IS NOT OLD.revision
    OR NEW.read_through IS NOT OLD.read_through OR NEW.outcome IS NOT OLD.outcome
    OR NEW.extractor IS NOT OLD.extractor OR NEW.reply_ts IS NOT OLD.reply_ts OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a dry-run revision never changes'); END;
CREATE TRIGGER dry_run_revisions_result_only_expires BEFORE UPDATE OF result ON dry_run_revisions
WHEN NEW.result IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a dry-run result is never rewritten, only expired'); END;
CREATE TRIGGER dry_run_revisions_no_delete BEFORE DELETE ON dry_run_revisions
BEGIN SELECT RAISE(ABORT, 'dry-run records are never deleted'); END;
CREATE TRIGGER dry_run_revisions_in_order BEFORE INSERT ON dry_run_revisions
WHEN EXISTS (SELECT 1 FROM dry_run_revisions WHERE id = NEW.id)
    OR NEW.revision IS NOT 2 + (SELECT count(*) FROM dry_run_revisions WHERE post_id = NEW.post_id)
    OR NEW.read_through <= coalesce(
        (SELECT max(read_through) FROM dry_run_revisions WHERE post_id = NEW.post_id),
        (SELECT read_through FROM dry_runs WHERE post_id = NEW.post_id),
        NEW.read_through
    )
    OR NOT EXISTS (SELECT 1 FROM post_messages WHERE id = NEW.read_through AND post_id = NEW.post_id)
BEGIN SELECT RAISE(ABORT, 'a revision follows the one before it and reads further'); END;
