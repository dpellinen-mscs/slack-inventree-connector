-- A transient failure puts an event back in the queue to be tried again later (weekly audit #105): `not_before` is
-- the earliest time it may be claimed again, NULL for any time. It is set only as the event goes back from
-- processing to pending, and cleared as it is claimed, so a finished event has none.

ALTER TABLE slack_events ADD COLUMN not_before TEXT;

CREATE TRIGGER slack_events_retry_later_on_requeue BEFORE UPDATE OF not_before ON slack_events
WHEN NEW.not_before IS NOT NULL AND NOT (OLD.status = 'processing' AND NEW.status = 'pending')
BEGIN SELECT RAISE(ABORT, 'an event is put off only as it goes back to the queue'); END;
