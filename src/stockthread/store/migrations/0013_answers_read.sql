-- Acting on an answer (design section 4, "Acting on an answer"): an answered question makes its post due for a
-- revision at once, and the revision applies the answer. `answers_through` is the newest answered question
-- (`review_items.id`) a dry run knew of, as `changes_through` is for edits: an answer given after it makes the
-- post due again. Only one question of a post is open at a time, so answers come in id order.
--
-- Answers given before this migration were recorded and never acted on. They stay that way: every record
-- counts them as known, so an upgrade posts no revision for an old click.

ALTER TABLE dry_runs ADD COLUMN answers_through INTEGER NOT NULL DEFAULT 0 CHECK (answers_through >= 0);
ALTER TABLE dry_run_revisions ADD COLUMN answers_through INTEGER NOT NULL DEFAULT 0 CHECK (answers_through >= 0);

UPDATE dry_runs SET answers_through = coalesce(
    (SELECT max(i.id) FROM review_items i WHERE i.post_id = dry_runs.post_id AND i.status = 'answered'), 0
);
UPDATE dry_run_revisions SET answers_through = (
    SELECT answers_through FROM dry_runs WHERE dry_runs.post_id = dry_run_revisions.post_id
);

CREATE TRIGGER dry_runs_answers_through_kept BEFORE UPDATE OF answers_through ON dry_runs
BEGIN SELECT RAISE(ABORT, 'a dry-run record never changes'); END;
CREATE TRIGGER dry_run_revisions_answers_through_kept BEFORE UPDATE OF answers_through ON dry_run_revisions
BEGIN SELECT RAISE(ABORT, 'a dry-run revision never changes'); END;
CREATE TRIGGER dry_runs_answers_through_given BEFORE INSERT ON dry_runs
WHEN NEW.answers_through <> 0
BEGIN SELECT RAISE(ABORT, 'a first dry run knows of no answer'); END;

DROP TRIGGER dry_run_revisions_in_order;
CREATE TRIGGER dry_run_revisions_in_order BEFORE INSERT ON dry_run_revisions
WHEN EXISTS (SELECT 1 FROM dry_run_revisions WHERE id = NEW.id)
    OR NEW.revision IS NOT 2 + (SELECT count(*) FROM dry_run_revisions WHERE post_id = NEW.post_id)
    OR NOT EXISTS (SELECT 1 FROM post_messages WHERE id = NEW.read_through AND post_id = NEW.post_id)
    OR NEW.changes_through IS NULL
    OR (NEW.changes_through <> 0 AND NOT EXISTS (SELECT 1 FROM revised_messages WHERE id = NEW.changes_through))
    OR (NEW.answers_through <> 0 AND NOT EXISTS (
        SELECT 1 FROM review_items WHERE id = NEW.answers_through AND post_id = NEW.post_id AND status = 'answered'
    ))
    OR NOT EXISTS (
        SELECT 1 FROM (
            SELECT coalesce(max(r.read_through), d.read_through) AS read,
                coalesce(max(r.changes_through), d.changes_through) AS changes,
                coalesce(max(r.answers_through), d.answers_through) AS answers
            FROM dry_runs d LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id WHERE d.post_id = NEW.post_id
        ) AS last
        WHERE NEW.read_through >= last.read AND NEW.changes_through >= last.changes
            AND NEW.answers_through >= last.answers
            AND (NEW.read_through > last.read OR NEW.changes_through > last.changes
                OR NEW.answers_through > last.answers)
    )
BEGIN SELECT RAISE(ABORT, 'a revision follows the one before it and reads further, or knows of a newer change or answer'); END;
