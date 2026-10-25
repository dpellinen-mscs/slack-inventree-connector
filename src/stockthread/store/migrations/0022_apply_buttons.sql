-- Apply buttons in Slack (design section 4, "Interactivity": buttons are removed once a decision is made; ADR 0019).
-- One row per dry-run reply posted with an Apply button: the post, the dry-run revision whose reply carries it, that
-- reply's ts, and `plans_before`, the newest plan revision of the post before the reply (0 for none): only a plan
-- revision after it is made from the reply's record (or made again by a later unchanged dry run). Like a question's
-- (migration 0012), `buttons` says where the button stands: `shown` (the reply has not been edited), `removed` (the
-- button was taken out of the reply), or `left` (the connector gave up: the reply could not be read or edited, or
-- carried no button it could find). `buttons_tries` counts failed attempts. Replies posted before this migration have
-- no row: their buttons stay up. A click is checked against the store whatever Slack shows (`Worker._handle_apply`),
-- so a button left up approves nothing it should not.

CREATE TABLE apply_buttons (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    message_ts TEXT NOT NULL CHECK (message_ts <> ''),
    plans_before INTEGER NOT NULL CHECK (plans_before >= 0),
    buttons TEXT NOT NULL DEFAULT 'shown' CHECK (buttons IN ('shown', 'removed', 'left')),
    buttons_tries INTEGER NOT NULL DEFAULT 0 CHECK (buttons_tries >= 0),
    UNIQUE (post_id, revision)
) STRICT;

CREATE TRIGGER apply_buttons_once BEFORE UPDATE OF buttons ON apply_buttons
WHEN OLD.buttons <> 'shown' OR NEW.buttons = 'shown'
BEGIN SELECT RAISE(ABORT, 'an Apply button is taken down once'); END;

CREATE TRIGGER apply_buttons_kept BEFORE UPDATE OF id, post_id, revision, message_ts, plans_before ON apply_buttons
BEGIN SELECT RAISE(ABORT, 'an Apply button stays on the reply it was posted with'); END;

CREATE TRIGGER apply_buttons_no_delete BEFORE DELETE ON apply_buttons
BEGIN SELECT RAISE(ABORT, 'Apply buttons are never deleted'); END;

-- As for every other table (0001): REPLACE would delete a row without a trace.
CREATE TRIGGER apply_buttons_no_replace BEFORE INSERT ON apply_buttons
WHEN EXISTS (SELECT 1 FROM apply_buttons WHERE id = NEW.id OR (post_id = NEW.post_id AND revision = NEW.revision))
BEGIN SELECT RAISE(ABORT, 'Apply buttons are changed by UPDATE only'); END;
