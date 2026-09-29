-- The first schema: posts, plan revisions, requests, attempts, the instance lock and the audit log.
-- Design sections 7 and 8. Tables for Slack events, extractions, matches, reviews, interactions,
-- API calls and aliases arrive with the features that fill them.
--
-- The idempotency keys are unique constraints. Key columns never change after insert, and rows that
-- carry a key are never deleted: triggers reject both, so a bug in the application cannot free a key.
-- REPLACE (and INSERT OR REPLACE) deletes a conflicting row without firing DELETE triggers, so every
-- protected table also refuses an insert that would collide with an existing row.

CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL CHECK (workspace_id <> ''),
    channel_id TEXT NOT NULL CHECK (channel_id <> ''),
    ts TEXT NOT NULL CHECK (ts <> ''),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, channel_id, ts)
) STRICT;

CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    state TEXT NOT NULL CHECK (
        state IN ('current', 'stale', 'expired', 'applied', 'partially_applied', 'undone', 'accepted')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (post_id, revision)
) STRICT;

CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans (id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    operation TEXT NOT NULL CHECK (operation IN ('transfer', 'remove', 'add', 'count')),
    state TEXT NOT NULL CHECK (
        state IN (
            'planned', 'approved', 'applying', 'failed', 'unknown', 'applied', 'verified', 'verify_failed',
            'accepted', 'undoing', 'undo_failed', 'undo_unknown', 'undone', 'rejected', 'cancelled', 'expired'
        )
    ),
    marker_id TEXT NOT NULL UNIQUE CHECK (
        length(marker_id) = 22 AND marker_id NOT GLOB '*[^0123456789abcdefghjkmnpqrstvwxyz]*'
    ),
    updated_at TEXT NOT NULL,
    UNIQUE (plan_id, sequence)
) STRICT;

CREATE TABLE request_attempts (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests (id),
    kind TEXT NOT NULL CHECK (kind IN ('a', 'u')),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 99),
    marker TEXT NOT NULL UNIQUE,
    snapshot TEXT NOT NULL CHECK (json_valid(snapshot)),
    request_body TEXT NOT NULL CHECK (json_valid(request_body)),
    outcome TEXT CHECK (outcome IN ('accepted', 'refused', 'unknown')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (request_id, kind, attempt)
) STRICT;

CREATE TABLE instance_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
) STRICT;

-- Ids and states only, never message text, so retention never has to touch it.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    post_id INTEGER,
    plan_id INTEGER,
    request_id INTEGER,
    attempt_id INTEGER,
    from_state TEXT,
    to_state TEXT
) STRICT;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER posts_keep_key BEFORE UPDATE OF id, workspace_id, channel_id, ts ON posts
BEGIN SELECT RAISE(ABORT, 'a post key never changes'); END;
CREATE TRIGGER posts_no_delete BEFORE DELETE ON posts
BEGIN SELECT RAISE(ABORT, 'posts are never deleted'); END;

CREATE TRIGGER plans_keep_key BEFORE UPDATE OF id, post_id, revision, input_hash ON plans
BEGIN SELECT RAISE(ABORT, 'a plan revision never changes its key or input'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON plans
BEGIN SELECT RAISE(ABORT, 'plans are never deleted'); END;

CREATE TRIGGER requests_keep_key BEFORE UPDATE OF id, plan_id, sequence, operation, marker_id ON requests
BEGIN SELECT RAISE(ABORT, 'a request never changes its key, operation or marker'); END;
CREATE TRIGGER requests_no_delete BEFORE DELETE ON requests
BEGIN SELECT RAISE(ABORT, 'requests are never deleted'); END;

-- An attempt records what was sent. Only its outcome may be filled in, once.
CREATE TRIGGER attempts_keep_intent
BEFORE UPDATE OF id, request_id, kind, attempt, marker, snapshot, request_body, started_at ON request_attempts
BEGIN SELECT RAISE(ABORT, 'an attempt never changes what it recorded before the write'); END;
CREATE TRIGGER attempts_outcome_once BEFORE UPDATE OF outcome, finished_at ON request_attempts
WHEN OLD.outcome IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an attempt outcome is recorded once'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON request_attempts
BEGIN SELECT RAISE(ABORT, 'attempts are never deleted'); END;

CREATE TRIGGER audit_log_no_replace BEFORE INSERT ON audit_log
WHEN EXISTS (SELECT 1 FROM audit_log WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER posts_no_replace BEFORE INSERT ON posts
WHEN EXISTS (
    SELECT 1 FROM posts
    WHERE id = NEW.id OR (workspace_id = NEW.workspace_id AND channel_id = NEW.channel_id AND ts = NEW.ts)
)
BEGIN SELECT RAISE(ABORT, 'a post key is taken'); END;
CREATE TRIGGER plans_no_replace BEFORE INSERT ON plans
WHEN EXISTS (SELECT 1 FROM plans WHERE id = NEW.id OR (post_id = NEW.post_id AND revision = NEW.revision))
BEGIN SELECT RAISE(ABORT, 'a plan revision key is taken'); END;
CREATE TRIGGER requests_no_replace BEFORE INSERT ON requests
WHEN EXISTS (
    SELECT 1 FROM requests
    WHERE id = NEW.id OR (plan_id = NEW.plan_id AND sequence = NEW.sequence) OR marker_id = NEW.marker_id
)
BEGIN SELECT RAISE(ABORT, 'a request key is taken'); END;
CREATE TRIGGER attempts_no_replace BEFORE INSERT ON request_attempts
WHEN EXISTS (
    SELECT 1 FROM request_attempts
    WHERE id = NEW.id OR (request_id = NEW.request_id AND kind = NEW.kind AND attempt = NEW.attempt)
        OR marker = NEW.marker
)
BEGIN SELECT RAISE(ABORT, 'an attempt key is taken'); END;

CREATE INDEX plans_by_post ON plans (post_id);
CREATE INDEX requests_by_state ON requests (state);
CREATE INDEX attempts_by_request ON request_attempts (request_id);
