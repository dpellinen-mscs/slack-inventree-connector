-- Questions (design section 4, "Questions" and "Interactivity"): a dry-run reply with review reasons asks the
-- one question its post needs, with buttons in the reply itself.
--
-- `review_items` holds one question per dry run (first or revision) that asked one: the question as JSON
-- (ids, codes and primary keys, never words of the post), whether only an approver may answer it, and the
-- message it was asked in. NULL `message_ts`: Slack gave no usable timestamp, so no click can fit it. A
-- question is `open` until it is answered, or until a newer dry run of its post stands in the thread (a
-- revised reply, or a notice that the reply no longer stands) and makes it `stale`. Both are final. Open does
-- not mean current: a click is checked against plan expiry too. `approver_only` is the mark a click trusts.
--
-- `interactions` holds one row per button: a random id of at least 128 bits (the button's value, all a click
-- carries) and the answer it stands for. A button row never changes.

CREATE TABLE review_items (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    question TEXT NOT NULL CHECK (json_valid(question)),
    approver_only INTEGER NOT NULL CHECK (approver_only IN (0, 1)),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    message_ts TEXT CHECK (message_ts IS NULL OR message_ts <> ''),
    status TEXT NOT NULL CHECK (status IN ('open', 'answered', 'stale')),
    answer TEXT CHECK (answer IS NULL OR json_valid(answer)),
    answered_by TEXT CHECK (answered_by IS NULL OR answered_by <> ''),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK ((status = 'answered') = (answer IS NOT NULL)),
    CHECK ((answer IS NULL) = (answered_by IS NULL)),
    CHECK ((status = 'open') = (closed_at IS NULL)),
    UNIQUE (post_id, revision)
) STRICT;

CREATE TRIGGER review_items_keep_question BEFORE UPDATE ON review_items
WHEN NEW.id IS NOT OLD.id OR NEW.post_id IS NOT OLD.post_id OR NEW.revision IS NOT OLD.revision
    OR NEW.question IS NOT OLD.question OR NEW.approver_only IS NOT OLD.approver_only
    OR NEW.channel_id IS NOT OLD.channel_id OR NEW.message_ts IS NOT OLD.message_ts
    OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a question never changes, only its answer'); END;
CREATE TRIGGER review_items_close_once BEFORE UPDATE OF status, answer, answered_by, closed_at ON review_items
WHEN OLD.status <> 'open' OR NEW.status = 'open'
BEGIN SELECT RAISE(ABORT, 'a question is answered or goes stale once'); END;
CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items
BEGIN SELECT RAISE(ABORT, 'questions are never deleted'); END;
CREATE TRIGGER review_items_asked_open BEFORE INSERT ON review_items
WHEN EXISTS (SELECT 1 FROM review_items WHERE id = NEW.id OR (post_id = NEW.post_id AND revision = NEW.revision))
    OR NEW.status <> 'open'
    OR EXISTS (SELECT 1 FROM review_items WHERE post_id = NEW.post_id AND status = 'open')
BEGIN SELECT RAISE(ABORT, 'a post has one open question, asked once per dry run'); END;
CREATE TRIGGER review_items_same_channel BEFORE INSERT ON review_items
WHEN NEW.channel_id IS NOT (SELECT channel_id FROM posts WHERE id = NEW.post_id)
BEGIN SELECT RAISE(ABORT, 'a question is asked in the channel of its post'); END;

CREATE TABLE interactions (
    id TEXT PRIMARY KEY CHECK (length(id) >= 22),
    review_item_id INTEGER NOT NULL REFERENCES review_items (id),
    position INTEGER NOT NULL CHECK (position >= 0),
    choice TEXT NOT NULL CHECK (json_valid(choice)),
    UNIQUE (review_item_id, position)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER interactions_no_update BEFORE UPDATE ON interactions
BEGIN SELECT RAISE(ABORT, 'a button never changes'); END;
CREATE TRIGGER interactions_no_delete BEFORE DELETE ON interactions
BEGIN SELECT RAISE(ABORT, 'buttons are never deleted'); END;
CREATE TRIGGER interactions_no_replace BEFORE INSERT ON interactions
WHEN EXISTS (SELECT 1 FROM interactions WHERE id = NEW.id OR (review_item_id = NEW.review_item_id
    AND position = NEW.position))
BEGIN SELECT RAISE(ABORT, 'a button is stored once'); END;
CREATE TRIGGER interactions_of_an_open_question BEFORE INSERT ON interactions
WHEN NOT EXISTS (SELECT 1 FROM review_items WHERE id = NEW.review_item_id AND status = 'open')
BEGIN SELECT RAISE(ABORT, 'a button belongs to an open question'); END;
