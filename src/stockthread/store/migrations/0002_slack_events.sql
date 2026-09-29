-- The durable Slack event queue (design section 4, "Durable queue"; section 8).
--
-- The listener stores the raw payload and only then acknowledges it, so a restart loses nothing that
-- was acknowledged. The key is unique, so a Slack retry is stored once. A row moves
-- pending -> processing -> done, dropped or failed, and back to pending only when an unfinished
-- row is picked up again at startup. The raw body can only be expired (set to NULL) by retention, and
-- only once the row is finished. Delivery is at least once: after a lock takeover, an event that the old
-- instance was processing runs again, so everything downstream is idempotent (request keys, markers).

CREATE TABLE slack_events (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL CHECK (event_key <> '') UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('event', 'interactive')),
    received_at TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'done', 'dropped', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_by TEXT,
    outcome TEXT CHECK (outcome IS NULL OR (length(outcome) BETWEEN 1 AND 64 AND outcome NOT GLOB '*[^a-z0-9_]*'))
) STRICT;

CREATE INDEX slack_events_by_status ON slack_events (status, id);

ALTER TABLE audit_log ADD COLUMN slack_event_id INTEGER;

CREATE TRIGGER slack_events_keep_key BEFORE UPDATE OF id, event_key, kind, received_at ON slack_events
BEGIN SELECT RAISE(ABORT, 'a stored Slack event never changes its key'); END;
CREATE TRIGGER slack_events_body_only_expires BEFORE UPDATE OF body ON slack_events
WHEN NEW.body IS NOT NULL OR OLD.status IN ('pending', 'processing')
BEGIN SELECT RAISE(ABORT, 'a raw Slack event is never rewritten, only expired once finished'); END;
CREATE TRIGGER slack_events_attempts_only_grow BEFORE UPDATE OF attempts ON slack_events
WHEN NEW.attempts < OLD.attempts
BEGIN SELECT RAISE(ABORT, 'Slack event attempts never go down'); END;
CREATE TRIGGER slack_events_claim_moves_with_status BEFORE UPDATE OF claimed_by ON slack_events
WHEN NEW.claimed_by IS NOT OLD.claimed_by AND NOT (
    (OLD.status = 'pending' AND NEW.status = 'processing' AND NEW.claimed_by IS NOT NULL)
    OR (OLD.status = 'processing' AND NEW.status = 'pending' AND NEW.claimed_by IS NULL)
)
BEGIN SELECT RAISE(ABORT, 'a Slack event changes hands only when it is claimed or requeued'); END;
CREATE TRIGGER slack_events_finished_is_final BEFORE UPDATE ON slack_events
WHEN OLD.status IN ('done', 'dropped', 'failed') AND (
    NEW.outcome IS NOT OLD.outcome OR NEW.claimed_by IS NOT OLD.claimed_by OR NEW.attempts <> OLD.attempts
)
BEGIN SELECT RAISE(ABORT, 'a finished Slack event never changes'); END;
CREATE TRIGGER slack_events_status_moves BEFORE UPDATE OF status ON slack_events
WHEN NOT (
    (OLD.status = 'pending' AND NEW.status = 'processing')
    OR (OLD.status = 'processing' AND NEW.status IN ('pending', 'done', 'dropped', 'failed'))
    OR OLD.status = NEW.status
)
BEGIN SELECT RAISE(ABORT, 'illegal Slack event status change'); END;
CREATE TRIGGER slack_events_no_delete BEFORE DELETE ON slack_events
BEGIN SELECT RAISE(ABORT, 'Slack events are never deleted'); END;
CREATE TRIGGER slack_events_no_replace BEFORE INSERT ON slack_events
WHEN EXISTS (SELECT 1 FROM slack_events WHERE id = NEW.id OR event_key = NEW.event_key)
BEGIN SELECT RAISE(ABORT, 'a Slack event key is taken'); END;
