-- One plan expiry per post (design section 4, "Plan expiry"; issue #74): `plan_from` is the Slack time a post's
-- plan expires from, fixed by its first dry run (the newest message it read). Its revisions and every question
-- asked about it count from this one time, so a question cannot be answered once its post can no longer be
-- revised, and a post cannot be revised once its questions have expired.
--
-- Posts that had their dry run before this migration: a question asked in that first dry run already counts from
-- the newest message, and the post now does too. A post without one keeps counting from its own time, as before.
-- Questions asked in a revision before this migration count from the post's own time, which is never later: they
-- expire no later than the post, as before.

ALTER TABLE posts ADD COLUMN plan_from TEXT CHECK (plan_from IS NULL OR plan_from <> '');

UPDATE posts SET plan_from = coalesce(
    (SELECT i.expires_from FROM review_items i WHERE i.post_id = posts.id AND i.revision = 1
        AND i.expires_from IS NOT NULL ORDER BY i.id LIMIT 1),
    ts
) WHERE dry_run_at IS NOT NULL;

CREATE TRIGGER posts_plan_from_once BEFORE UPDATE OF plan_from ON posts
WHEN OLD.plan_from IS NOT NULL AND NEW.plan_from IS NOT OLD.plan_from
BEGIN SELECT RAISE(ABORT, 'a post''s plan expires from one time'); END;
CREATE TRIGGER posts_plan_from_with_dry_run BEFORE UPDATE OF dry_run_at ON posts
WHEN NEW.dry_run_at IS NOT NULL AND NEW.plan_from IS NULL
BEGIN SELECT RAISE(ABORT, 'a dry run fixes when the post''s plan expires'); END;
