-- A row's id never changes, under any of its names.
--
-- "BEFORE UPDATE OF id" does not fire for "UPDATE ... SET rowid = ..." (or oid, _rowid_), although `id` is
-- the rowid of these tables. So a row that carries an idempotency key could be given another id, and the
-- rows that point to it (a plan's requests, a request's attempts, the audit log) would then point to
-- nothing, or to a row inserted later under the old id. A trigger on every update, with a condition,
-- fires whatever the column was called.

CREATE TRIGGER posts_keep_id BEFORE UPDATE ON posts
WHEN NEW.id IS NOT OLD.id
BEGIN SELECT RAISE(ABORT, 'a post key never changes'); END;

CREATE TRIGGER plans_keep_id BEFORE UPDATE ON plans
WHEN NEW.id IS NOT OLD.id
BEGIN SELECT RAISE(ABORT, 'a plan revision never changes its key or input'); END;

CREATE TRIGGER requests_keep_id BEFORE UPDATE ON requests
WHEN NEW.id IS NOT OLD.id
BEGIN SELECT RAISE(ABORT, 'a request never changes its key, operation or marker'); END;

CREATE TRIGGER attempts_keep_id BEFORE UPDATE ON request_attempts
WHEN NEW.id IS NOT OLD.id
BEGIN SELECT RAISE(ABORT, 'an attempt never changes what it recorded before the write'); END;

CREATE TRIGGER slack_events_keep_id BEFORE UPDATE ON slack_events
WHEN NEW.id IS NOT OLD.id
BEGIN SELECT RAISE(ABORT, 'a stored Slack event never changes its key'); END;
