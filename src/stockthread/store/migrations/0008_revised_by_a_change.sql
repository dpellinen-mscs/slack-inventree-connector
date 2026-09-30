-- An edit or a deletion alone makes an answered post due again (design section 4, "Plan revisions").
--
-- Every dry run keeps `changes_through`: the newest `revised_messages.id` that concerned its post when it
-- was decided, 0 when none did. A post with a change noted past that waits for a revision, as it does for a
-- message past `read_through`. Existing records take every change noted so far, so an upgrade never answers
-- an old edit. A revision reads no less than the one before, and reads further or knows of a newer change.

ALTER TABLE dry_runs ADD COLUMN changes_through INTEGER;
ALTER TABLE dry_run_revisions ADD COLUMN changes_through INTEGER;

UPDATE dry_runs SET changes_through = (
    SELECT coalesce(max(r.id), 0) FROM revised_messages r JOIN posts p ON p.id = dry_runs.post_id
    WHERE r.workspace_id = p.workspace_id AND r.channel_id = p.channel_id AND (
        r.ts = p.ts OR EXISTS (SELECT 1 FROM post_messages m WHERE m.post_id = p.id AND m.ts = r.ts)
    )
);
UPDATE dry_run_revisions SET changes_through = (
    SELECT changes_through FROM dry_runs WHERE dry_runs.post_id = dry_run_revisions.post_id
);

CREATE TRIGGER dry_runs_changes_through_kept BEFORE UPDATE OF changes_through ON dry_runs
BEGIN SELECT RAISE(ABORT, 'a dry-run record never changes'); END;
CREATE TRIGGER dry_runs_changes_through_given BEFORE INSERT ON dry_runs
WHEN NEW.changes_through IS NULL OR (
    NEW.changes_through <> 0 AND NOT EXISTS (SELECT 1 FROM revised_messages WHERE id = NEW.changes_through)
)
BEGIN SELECT RAISE(ABORT, 'a dry run knows which changes it was decided with'); END;

CREATE TRIGGER dry_run_revisions_changes_through_kept BEFORE UPDATE OF changes_through ON dry_run_revisions
BEGIN SELECT RAISE(ABORT, 'a dry-run revision never changes'); END;

DROP TRIGGER dry_run_revisions_in_order;
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
