-- Confirmed aliases (design section 10, ADR 0012): a text that names one part or one stock location. One alias per
-- kind, words of the text and user: a later confirmation of the same text replaces it (`Store.learn_alias`).
-- `words` is the text as `matching.words` splits it, joined by spaces. `user_id` is NULL for an alias everyone's
-- posts use, and the Slack user id for one with a word like "my" (per-user aliases, M4). `rivals` is the JSON list of
-- the records the whole-word and fuzzy rules fitted when it was confirmed; a record that fits later and is not in it
-- makes the alias ambiguous. NULL for an alias an admin typed, which is never rechecked this way. `request_id` is the
-- verified request the confirmation moved stock in, so an undo can offer to forget what it taught. AUTOINCREMENT: an
-- admin removes aliases by id (`stockthread aliases remove`), and the audit log names them by id, so none is reused.

CREATE TABLE aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('part', 'location')),
    text TEXT NOT NULL CHECK (text <> ''),
    words TEXT NOT NULL CHECK (words <> ''),
    record INTEGER NOT NULL CHECK (record > 0),
    user_id TEXT CHECK (user_id IS NULL OR user_id <> ''),
    rivals TEXT CHECK (rivals IS NULL OR json_type(rivals) = 'array'),
    request_id INTEGER REFERENCES requests (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX aliases_one_per_text ON aliases (kind, words, ifnull(user_id, ''));

ALTER TABLE audit_log ADD COLUMN alias_id INTEGER;

-- As for every other table (0001): REPLACE would delete a row without a trace.
CREATE TRIGGER aliases_no_replace BEFORE INSERT ON aliases
WHEN EXISTS (
    SELECT 1 FROM aliases
    WHERE id = NEW.id OR (kind = NEW.kind AND words = NEW.words AND ifnull(user_id, '') = ifnull(NEW.user_id, ''))
)
BEGIN SELECT RAISE(ABORT, 'aliases are replaced by UPDATE only'); END;

-- `rivals` holds record ids: positive integers only (a CHECK cannot read json_each).
CREATE TRIGGER aliases_rivals_insert BEFORE INSERT ON aliases
WHEN NEW.rivals IS NOT NULL AND EXISTS (
    SELECT 1 FROM json_each(NEW.rivals) WHERE type <> 'integer' OR value <= 0
)
BEGIN SELECT RAISE(ABORT, 'rivals must be record ids'); END;
CREATE TRIGGER aliases_rivals_update BEFORE UPDATE OF rivals ON aliases
WHEN NEW.rivals IS NOT NULL AND EXISTS (
    SELECT 1 FROM json_each(NEW.rivals) WHERE type <> 'integer' OR value <= 0
)
BEGIN SELECT RAISE(ABORT, 'rivals must be record ids'); END;
