# 0009: The audit store: stdlib sqlite3, SQL migrations, guards in the database

- Status: Accepted
- Date: 2026-09-29
- Decided by: agent (FYI to Dan)

## Context

Design section 8 puts the audit trail and the request state machine in one
SQLite file. The store is the local half of the idempotency guard: a
request is applied at most once across retries, restarts and races, and a
post has at most one applied plan revision at a time. A bug in application
code must not be able to free a key or rewrite what was sent.

## Decision

- The standard library `sqlite3` module, no ORM and no migration library.
  SQL is short, reviewable and runs the same way in tests and production.
- Migrations are numbered `.sql` files in `stockthread/store/migrations/`,
  forward only. `PRAGMA user_version` holds the applied number. Each file
  runs in one transaction with its version bump. A database with a newer
  version than the code knows is refused, never downgraded.
- Connections use autocommit mode with explicit `BEGIN IMMEDIATE`, so every
  change takes the write lock before its first read. A state change is a
  compare-and-set, and its audit row is written in the same transaction.
- `journal_mode = WAL` and `synchronous = FULL`: a committed intent is on
  disk before the write it describes is sent to InvenTree.
- The database, not only the code, enforces the invariants that protect
  stock: unique keys for posts, plan revisions, requests, markers and
  attempts; STRICT tables with CHECK constraints on states; triggers that
  reject changes to key columns, to the recorded intent of an attempt, a
  second outcome of an attempt, and any update or delete of `audit_log`
  and of rows that carry a key.
- Triggers also refuse an insert that collides with an existing row, because
  `REPLACE` deletes the old row without firing delete triggers. Connections
  also turn on `recursive_triggers`.
- `begin_attempt` is the only way to start a write. In one transaction it
  checks that this process holds the instance lock, that no other request
  of the post is in flight or not yet read back, and, for an apply, that
  the plan is current, earlier revisions left no change and earlier
  requests of the plan are verified (or rejected). Then it stores the
  snapshot, the request body and the marker, and moves the request.
- The first migration holds only what the apply protocol needs: posts,
  plans, requests, request attempts, the instance lock and the audit log.
  The other tables of section 8 arrive with the features that fill them.
- SQLite 3.37 or later (STRICT tables). Every supported Python on current
  Linux and macOS ships a newer one.

## Consequences

- Data changes need a new migration file, and a migration can never be
  edited once released.
- The triggers make rows permanent. Retention of message text (a later
  migration) will blank text columns, which the triggers allow, and keep
  the rows.
- One process writes at a time. That fits the single-instance design; the
  instance lock with a heartbeat refuses a second instance on the same file.
- If a second backend (PostgreSQL) is ever needed, the SQL and triggers
  must be ported. Nothing in v1.0 needs one.
