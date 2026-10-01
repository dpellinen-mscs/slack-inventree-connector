-- Who may answer a question, and until when, as they were when it was asked (design section 4,
-- "Authorization"): the poster of the post as its dry run read it, and the Slack time its plan's expiry counts
-- from (the newest message for a first dry run, the post's own time for a revision). Read again at click time,
-- either could have moved: a thread root that arrives later changes the oldest message.
--
-- Questions asked before this migration have neither: only an approver may answer them, and their expiry
-- counts from the post's own time.

ALTER TABLE review_items ADD COLUMN poster_id TEXT CHECK (poster_id IS NULL OR poster_id <> '');
ALTER TABLE review_items ADD COLUMN expires_from TEXT CHECK (expires_from IS NULL OR expires_from <> '');

DROP TRIGGER review_items_keep_question;
CREATE TRIGGER review_items_keep_question BEFORE UPDATE ON review_items
WHEN NEW.id IS NOT OLD.id OR NEW.post_id IS NOT OLD.post_id OR NEW.revision IS NOT OLD.revision
    OR NEW.question IS NOT OLD.question OR NEW.approver_only IS NOT OLD.approver_only
    OR NEW.channel_id IS NOT OLD.channel_id OR NEW.message_ts IS NOT OLD.message_ts
    OR NEW.created_at IS NOT OLD.created_at OR NEW.poster_id IS NOT OLD.poster_id
    OR NEW.expires_from IS NOT OLD.expires_from
BEGIN SELECT RAISE(ABORT, 'a question never changes, only its answer'); END;
CREATE TRIGGER review_items_know_who_answers BEFORE INSERT ON review_items
WHEN NEW.poster_id IS NULL OR NEW.expires_from IS NULL
BEGIN SELECT RAISE(ABORT, 'a question knows its poster and when its plan expires'); END;
