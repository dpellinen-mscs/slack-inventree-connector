-- Buttons in Slack (design section 4, "Interactivity": buttons are removed once a decision is made). A question
-- that is answered or stale still shows its buttons in the reply it was asked in until the connector edits that
-- reply. `buttons` says where that stands: `shown` (the reply has not been edited), `removed` (the question was
-- taken out of the reply), or `left` (the connector gave up: the reply could not be read or edited, carried no
-- question it could find, or the question closed before this migration). `buttons_tries` counts failed attempts.
-- A question still open at this migration was asked in a reply without the marked divider: once it closes, its
-- reply is read once and left.

ALTER TABLE review_items ADD COLUMN buttons TEXT NOT NULL DEFAULT 'shown'
    CHECK (buttons IN ('shown', 'removed', 'left'));
ALTER TABLE review_items ADD COLUMN buttons_tries INTEGER NOT NULL DEFAULT 0 CHECK (buttons_tries >= 0);

UPDATE review_items SET buttons = 'left' WHERE status <> 'open';

CREATE TRIGGER review_items_buttons_once BEFORE UPDATE OF buttons ON review_items
WHEN OLD.buttons <> 'shown' OR NEW.buttons = 'shown' OR NEW.status = 'open' OR NEW.message_ts IS NULL
BEGIN SELECT RAISE(ABORT, 'buttons are taken down once, after their question closed'); END;
