-- Catch-up after downtime (design section 4, "Catch-up after downtime"): how far each channel's history
-- has been read.
--
-- One row per channel, made by the first sweep of that channel. `since_ts` is where that sweep stood:
-- nothing older is ever read from the history, so a new installation (or a channel new to the allowlist)
-- does not answer the posts that were there before it. `swept_to` is the newest point up to which a whole
-- sweep has ended; it only moves forward. Both are Slack timestamps ("1790000000.000000"), kept as text.

CREATE TABLE catch_up (
    workspace_id TEXT NOT NULL CHECK (workspace_id <> ''),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    since_ts TEXT NOT NULL CHECK (since_ts GLOB '[0-9]*.[0-9][0-9][0-9][0-9][0-9][0-9]'),
    swept_to TEXT NOT NULL CHECK (swept_to GLOB '[0-9]*.[0-9][0-9][0-9][0-9][0-9][0-9]'),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, channel_id),
    CHECK (since_ts NOT GLOB '*[^0-9.]*' AND swept_to NOT GLOB '*[^0-9.]*'),
    CHECK (CAST(swept_to AS REAL) >= CAST(since_ts AS REAL))
) STRICT, WITHOUT ROWID;

CREATE TRIGGER catch_up_keeps_its_start BEFORE UPDATE ON catch_up
WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.channel_id IS NOT OLD.channel_id
    OR NEW.since_ts IS NOT OLD.since_ts
BEGIN SELECT RAISE(ABORT, 'a channel keeps the point its catch-up started from'); END;
CREATE TRIGGER catch_up_only_moves_forward BEFORE UPDATE ON catch_up
WHEN CAST(NEW.swept_to AS REAL) < CAST(OLD.swept_to AS REAL)
BEGIN SELECT RAISE(ABORT, 'catch-up never moves back'); END;
CREATE TRIGGER catch_up_no_delete BEFORE DELETE ON catch_up
BEGIN SELECT RAISE(ABORT, 'a catch-up mark is never deleted'); END;
CREATE TRIGGER catch_up_no_replace BEFORE INSERT ON catch_up
WHEN EXISTS (SELECT 1 FROM catch_up WHERE workspace_id = NEW.workspace_id AND channel_id = NEW.channel_id)
BEGIN SELECT RAISE(ABORT, 'a channel has one catch-up mark'); END;
