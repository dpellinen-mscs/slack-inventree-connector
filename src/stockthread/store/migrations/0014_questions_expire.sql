-- Plan expiry for questions (design section 4, "Taken down once closed"): a question still open when its plan
-- expires is marked `expired_at` (once, while open) and then taken out of the reply it was asked in, like an
-- answered or stale one, ending with a line that says it expired. An expired question takes no answer, also if
-- `plan_expiry_hours` is raised later: its buttons are gone from Slack.
--
-- Questions open at this migration are marked by the connector's next take-down step, like any other.

ALTER TABLE review_items ADD COLUMN expired_at TEXT CHECK (expired_at IS NULL OR expired_at <> '');

CREATE TRIGGER review_items_expire_once BEFORE UPDATE OF expired_at ON review_items
WHEN OLD.expired_at IS NOT NULL OR OLD.status <> 'open'
BEGIN SELECT RAISE(ABORT, 'a question expires once, while it is open'); END;
CREATE TRIGGER review_items_expired_unanswered BEFORE UPDATE OF status ON review_items
WHEN OLD.expired_at IS NOT NULL AND NEW.status = 'answered'
BEGIN SELECT RAISE(ABORT, 'an expired question takes no answer'); END;
CREATE TRIGGER review_items_born_unexpired BEFORE INSERT ON review_items
WHEN NEW.expired_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a question is asked unexpired'); END;

DROP TRIGGER review_items_buttons_once;
CREATE TRIGGER review_items_buttons_once BEFORE UPDATE OF buttons ON review_items
WHEN OLD.buttons <> 'shown' OR NEW.buttons = 'shown' OR (NEW.status = 'open' AND NEW.expired_at IS NULL)
    OR NEW.message_ts IS NULL
BEGIN SELECT RAISE(ABORT, 'buttons are taken down once, after their question closed or expired'); END;
