"""The store of posts, plan revisions, requests and their attempts, with an append-only audit log.

Every change runs in one `BEGIN IMMEDIATE` transaction, so a writer holds the
database lock from its first read, and a state change is a compare-and-set:
two workers that race for the same request cannot both move it. Each state
change writes its audit row in the same transaction.

`synchronous = FULL` makes a committed transaction durable before the call
returns. The apply protocol relies on that: the intent of a write (snapshot,
request body, marker, state `applying`) is on disk before the write is sent.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from stockthread.core.grouping import Arrival, Placed, choose_post, seconds
from stockthread.core.marker import MAX_ATTEMPT, AttemptKind, Marker, new_marker_id
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import (
    UNSETTLED,
    Actor,
    RequestState,
    check_transition,
    may_approve_new_revision,
)

S = RequestState

MIN_SQLITE = (3, 37, 0)
"""STRICT tables."""

OUTCOMES: dict[RequestState, str] = {
    S.APPLIED: "accepted",
    S.FAILED: "refused",
    S.UNKNOWN: "unknown",
    S.UNDONE: "accepted",
    S.UNDO_FAILED: "refused",
    S.UNDO_UNKNOWN: "unknown",
}
"""The outcome of the open attempt, by the state a request leaves `applying` or `undoing` for."""

NOT_READ_BACK: frozenset[RequestState] = UNSETTLED | {S.APPLIED}
"""While a request of a post is in one of these, no other write of the post may start: its read-back
compares quantities with its snapshot, and another write would change them."""


class SchemaTooNew(Exception):
    """The database was migrated by a newer version of the connector."""


class StateConflict(Exception):
    """The request is not in the expected state: someone else moved it first. Nothing changed."""


class WriteBlocked(Exception):
    """The transition is allowed by the state machine, but the post or plan forbids it now."""


class InstanceLocked(Exception):
    """Another instance holds the lock and its heartbeat is fresh."""


class LockLost(Exception):
    """This instance no longer holds the lock."""


@dataclass(frozen=True)
class RequestRow:
    key: RequestKey
    id: int
    operation: Operation
    state: RequestState
    marker_id: str
    plan_state: str


@dataclass(frozen=True)
class Attempt:
    id: int
    marker: Marker


EventKind = Literal["event", "interactive"]


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    DROPPED = "dropped"
    FAILED = "failed"


@dataclass(frozen=True)
class QueuedEvent:
    id: int
    key: str
    """The event id for events; for clicks, a key the listener derives from the payload."""
    kind: EventKind
    body: str | None
    """The raw payload, or None once retention expired it."""
    attempts: int


_SLACK_TS = re.compile(r"\d{1,10}\.\d{6}")
"""A Slack message timestamp as Slack sends it."""


def new_lock_owner() -> str:
    """A random owner id for the instance lock, new for every process."""
    return secrets.token_hex(16)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _statements(script: str) -> Iterator[str]:
    """Split a migration into statements. Trigger bodies hold semicolons, so split on complete statements."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip() and not all(part.strip().startswith("--") or not part.strip() for part in buffer.splitlines()):
        raise RuntimeError("a migration ends with an incomplete statement")


def _set_wal(connection: sqlite3.Connection) -> None:
    """Switch to WAL. Two processes opening a new file at once can see SQLITE_BUSY here; retry briefly."""
    for _ in range(50):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) and "busy" not in str(error):
                raise
            time.sleep(0.1)
    connection.execute("PRAGMA journal_mode = WAL")


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files("stockthread.store").joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    found.sort()
    if [version for version, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migrations must be numbered 1, 2, 3, ... without gaps")
    return found


class Store:
    def __init__(self, connection: sqlite3.Connection, clock: Callable[[], datetime]) -> None:
        self._db = connection
        self._clock = clock

    @classmethod
    def open(cls, path: str | Path, *, clock: Callable[[], datetime] = _utcnow) -> Store:
        """Open the database, creating and migrating it as needed."""
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(f"SQLite {'.'.join(map(str, MIN_SQLITE))} or later is needed")
        connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            # REPLACE fires DELETE triggers only with this on. The schema also refuses REPLACE by itself.
            connection.execute("PRAGMA recursive_triggers = ON")
            _set_wal(connection)
            connection.execute("PRAGMA synchronous = FULL")
            store = cls(connection, clock)
            store._migrate()
        except BaseException:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self._db.close()

    @property
    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        migrations = _migrations()
        for version, script in migrations:
            # The version is read again inside the transaction, so two processes that open a new file at
            # once do not both run a migration. A failed migration rolls back with its version bump.
            with self._transaction():
                current = self.schema_version
                if current > len(migrations):
                    raise SchemaTooNew(
                        f"the database has schema version {current}; this version knows {len(migrations)}"
                    )
                if version <= current:
                    continue
                for statement in _statements(script):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {version:d}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
            self._db.execute("COMMIT")
        except BaseException:
            # SQLite may have rolled back by itself (disk full, I/O error). Do not hide the first error.
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _clock_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the store clock must return an aware datetime")
        return now

    def _now(self) -> str:
        return self._clock_now().astimezone(UTC).isoformat(timespec="microseconds")

    def _audit(self, actor: str, event: str, **ids: Any) -> None:
        columns = ["at", "actor", "event", *ids]
        placeholders = ", ".join("?" for _ in columns)
        # The column names come from this module, never from input.
        self._db.execute(
            f"INSERT INTO audit_log ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
            [self._now(), actor, event, *ids.values()],
        )

    # Posts and plans

    def _post_id(self, key: PostKey) -> int | None:
        row = self._db.execute(
            "SELECT id FROM posts WHERE workspace_id = ? AND channel_id = ? AND ts = ?",
            (key.workspace_id, key.channel_id, key.ts),
        ).fetchone()
        return None if row is None else int(row[0])

    def create_post(self, key: PostKey) -> int:
        """Create the post, or return the id of the existing one. The key is fixed from here on.

        This bypasses grouping and the lock: the pipeline uses `place_message`. For tests and tools only.
        """
        with self._transaction():
            existing = self._post_id(key)
            if existing is not None:
                return existing
            cursor = self._db.execute(
                "INSERT INTO posts (workspace_id, channel_id, ts, created_at) VALUES (?, ?, ?, ?)",
                (key.workspace_id, key.channel_id, key.ts, self._now()),
            )
            post_id = int(cursor.lastrowid or 0)
            self._audit(Actor.PLANNER, "post_created", post_id=post_id)
            return post_id

    def place_message(
        self,
        owner: str,
        message: PostKey,
        *,
        user_id: str,
        thread_ts: str | None,
        window_seconds: int,
        slack_event_id: int | None = None,
    ) -> tuple[PostKey, bool]:
        """Put a message in its post (design section 4, "Grouping"): the post key, and whether the post is new.

        `message` is the message's own key. A message is placed once: a redelivery gets the same post, with
        `False`. After a crash the first placement may have committed without a plan, so callers plan on
        every placement, not only when the post is new. `owner` must hold the instance lock, so two
        instances never group the same channel differently. Timestamps must be in Slack's exact form
        ("1790000000.000100"): keys are compared as strings. `slack_event_id` is the stored event the message
        came from: the worker reads the message text from its raw body.
        """
        if not user_id or window_seconds < 0:
            raise ValueError("a message needs a user, and the window cannot be negative")
        thread_ts = thread_ts or None
        for ts in (message.ts, thread_ts or message.ts):
            if not _SLACK_TS.fullmatch(ts):
                raise ValueError(f"not a Slack timestamp: {ts!r}")
        at = seconds(message.ts)
        root = thread_ts or message.ts
        workspace, channel = message.workspace_id, message.channel_id
        with self._transaction():
            self._check_lock(owner)
            # The window is found by a float range with a second of margin, then decided exactly in Decimal.
            rows = self._db.execute(
                "SELECT m.ts, m.user_id, m.thread_ts, p.ts, p.siblings_closed_at IS NULL"
                " FROM post_messages m JOIN posts p ON p.id = m.post_id"
                " WHERE m.workspace_id = ? AND m.channel_id = ?"
                " AND (m.ts IN (?, ?) OR (m.user_id = ? AND CAST(m.ts AS REAL) BETWEEN ? AND ?))",
                (
                    workspace,
                    channel,
                    message.ts,
                    root,
                    user_id,
                    float(at) - window_seconds - 1,
                    float(at) + window_seconds + 1,
                ),
            ).fetchall()
            placed = [Placed(row[0], row[1], row[2], row[3], bool(row[4])) for row in rows]
            keys = {
                row[0]
                for row in self._db.execute(
                    "SELECT ts FROM posts WHERE workspace_id = ? AND channel_id = ? AND ts IN (?, ?)",
                    (workspace, channel, message.ts, root),
                )
            }
            key = PostKey(
                workspace, channel, choose_post(Arrival(message.ts, user_id, thread_ts), placed, keys, window_seconds)
            )
            if any(row.ts == message.ts for row in placed):
                return key, False
            post_id = self._post_id(key)
            created = post_id is None
            if post_id is None:
                cursor = self._db.execute(
                    "INSERT INTO posts (workspace_id, channel_id, ts, created_at) VALUES (?, ?, ?, ?)",
                    (workspace, channel, key.ts, self._now()),
                )
                post_id = int(cursor.lastrowid or 0)
                self._audit(Actor.PLANNER, "post_created", post_id=post_id)
            self._db.execute(
                "INSERT INTO post_messages"
                " (post_id, workspace_id, channel_id, ts, user_id, thread_ts, added_at, slack_event_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, workspace, channel, message.ts, user_id, thread_ts, self._now(), slack_event_id),
            )
            self._audit(Actor.PLANNER, "message_grouped", post_id=post_id)
            return key, created

    def post_messages(self, key: PostKey) -> list[str]:
        """The timestamps of the messages in a post, oldest first."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute("SELECT ts FROM post_messages WHERE post_id = ?", (post_id,)).fetchall()
        return sorted((row[0] for row in rows), key=seconds)

    def close_siblings(self, owner: str, key: PostKey) -> None:
        """No more siblings join the post: its dry-run reply is posted. Thread replies still join. Idempotent."""
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(key)
            if post_id is None:
                raise KeyError("no such post")
            cursor = self._db.execute(
                "UPDATE posts SET siblings_closed_at = ? WHERE id = ? AND siblings_closed_at IS NULL",
                (self._now(), post_id),
            )
            if cursor.rowcount:
                self._audit(Actor.PLANNER, "siblings_closed", post_id=post_id)

    def posts_waiting_for_dry_run(self) -> list[tuple[PostKey, str]]:
        """Posts without a dry-run outcome yet, oldest first, each with the timestamp of its newest message."""
        rows = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts, m.ts FROM posts p JOIN post_messages m ON m.post_id = p.id"
            " WHERE p.dry_run_at IS NULL ORDER BY p.id"
        ).fetchall()
        newest: dict[PostKey, str] = {}
        for workspace, channel, post_ts, ts in rows:
            key = PostKey(workspace, channel, post_ts)
            if key not in newest or seconds(ts) > seconds(newest[key]):
                newest[key] = ts
        return list(newest.items())

    def post_bodies(self, key: PostKey) -> list[tuple[str, str | None]]:
        """Each message of a post, oldest first: its timestamp, and the raw body of the event it came from.

        The body is None when retention expired it, or when the message was grouped before migration 0004.
        """
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT m.ts, e.body FROM post_messages m LEFT JOIN slack_events e ON e.id = m.slack_event_id"
            " WHERE m.post_id = ?",
            (post_id,),
        ).fetchall()
        return sorted(((row[0], row[1]) for row in rows), key=lambda row: seconds(row[0]))

    def finish_dry_run(self, owner: str, key: PostKey, reply_ts: str | None) -> None:
        """Record the post's dry-run outcome: the reply's timestamp, or None when it got no reply. Once only."""
        if reply_ts is not None and not _SLACK_TS.fullmatch(reply_ts):
            raise ValueError(f"not a Slack timestamp: {reply_ts!r}")
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(key)
            if post_id is None:
                raise KeyError("no such post")
            cursor = self._db.execute(
                "UPDATE posts SET dry_run_at = ?, reply_ts = ? WHERE id = ? AND dry_run_at IS NULL",
                (self._now(), reply_ts, post_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict("the post already has its dry-run outcome")
            self._audit(Actor.PLANNER, "dry_run_replied" if reply_ts else "dry_run_silent", post_id=post_id)

    def create_plan(self, post: PostKey, input_hash: str, operations: Sequence[Operation]) -> int:
        """Store a new plan revision with one planned request per operation, and return its revision.

        The earlier current revision becomes stale, and its planned and approved requests are
        cancelled. Requests that already started a write keep their state.
        """
        if not operations:
            raise ValueError("a plan revision holds at least one request")
        with self._transaction():
            post_id = self._post_id(post)
            if post_id is None:
                raise KeyError(f"no post {post}")
            (last,) = self._db.execute(
                "SELECT coalesce(max(revision), 0) FROM plans WHERE post_id = ?", (post_id,)
            ).fetchone()
            for (plan_id,) in self._db.execute(
                "SELECT id FROM plans WHERE post_id = ? AND state = 'current'", (post_id,)
            ).fetchall():
                self._db.execute("UPDATE plans SET state = 'stale' WHERE id = ?", (plan_id,))
                self._audit(Actor.PLANNER, "plan_stale", post_id=post_id, plan_id=plan_id)
                for request_id, state in self._db.execute(
                    "SELECT id, state FROM requests WHERE plan_id = ? AND state IN ('planned', 'approved')",
                    (plan_id,),
                ).fetchall():
                    self._set_state(request_id, S(state), S.CANCELLED, Actor.PLANNER)
            revision = int(last) + 1
            cursor = self._db.execute(
                "INSERT INTO plans (post_id, revision, input_hash, state, created_at) VALUES (?, ?, ?, 'current', ?)",
                (post_id, revision, input_hash, self._now()),
            )
            plan_id = int(cursor.lastrowid or 0)
            self._audit(Actor.PLANNER, "plan_created", post_id=post_id, plan_id=plan_id)
            for sequence, operation in enumerate(operations, start=1):
                cursor = self._db.execute(
                    "INSERT INTO requests (plan_id, sequence, operation, state, marker_id, updated_at)"
                    " VALUES (?, ?, ?, 'planned', ?, ?)",
                    (plan_id, sequence, Operation(operation).value, new_marker_id(), self._now()),
                )
                self._audit(
                    Actor.PLANNER,
                    "request_created",
                    post_id=post_id,
                    plan_id=plan_id,
                    request_id=int(cursor.lastrowid or 0),
                    to_state=S.PLANNED.value,
                )
            return revision

    # Requests

    _REQUEST_SELECT = """
        SELECT r.id, r.operation, r.state, r.marker_id, p.state, p.id, p.post_id
        FROM requests r JOIN plans p ON p.id = r.plan_id JOIN posts o ON o.id = p.post_id
        WHERE o.workspace_id = ? AND o.channel_id = ? AND o.ts = ? AND p.revision = ? AND r.sequence = ?
    """

    def _request(self, key: RequestKey) -> tuple[RequestRow, int, int]:
        row = self._db.execute(
            self._REQUEST_SELECT,
            (key.post.workspace_id, key.post.channel_id, key.post.ts, key.revision, key.sequence),
        ).fetchone()
        if row is None:
            raise KeyError(f"no request {key}")
        request_id, operation, state, marker_id, plan_state, plan_id, post_id = row
        return (
            RequestRow(key, request_id, Operation(operation), S(state), marker_id, plan_state),
            plan_id,
            post_id,
        )

    def request(self, key: RequestKey) -> RequestRow:
        return self._request(key)[0]

    def _set_state(self, request_id: int, expected: RequestState, target: RequestState, actor: Actor) -> None:
        """The compare-and-set. Call inside a transaction, after every check."""
        check_transition(expected, target, actor)
        cursor = self._db.execute(
            "UPDATE requests SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (target.value, self._now(), request_id, expected.value),
        )
        if cursor.rowcount != 1:
            raise StateConflict(f"request {request_id} is no longer {expected}")
        plan_id, post_id = self._db.execute(
            "SELECT p.id, p.post_id FROM requests r JOIN plans p ON p.id = r.plan_id WHERE r.id = ?", (request_id,)
        ).fetchone()
        self._audit(
            actor,
            "request_state",
            post_id=post_id,
            plan_id=plan_id,
            request_id=request_id,
            from_state=expected.value,
            to_state=target.value,
        )

    def _post_states(self, post_id: int, *, exclude: int) -> list[tuple[int, int, RequestState]]:
        """(revision, sequence, state) of every other request of the post."""
        rows = self._db.execute(
            "SELECT p.revision, r.sequence, r.state FROM requests r JOIN plans p ON p.id = r.plan_id"
            " WHERE p.post_id = ? AND r.id <> ?",
            (post_id, exclude),
        ).fetchall()
        return [(int(revision), int(sequence), S(state)) for revision, sequence, state in rows]

    def transition(self, key: RequestKey, *, expected: RequestState, target: RequestState, actor: Actor) -> None:
        """Move a request from `expected` to `target`, or raise and change nothing.

        Raises IllegalTransition, StateConflict when the request is not in `expected`, and
        WriteBlocked when an approval is not allowed for the post now. Starting a write goes
        through `begin_attempt`, never through this method.
        """
        if target in (S.APPLYING, S.UNDOING):
            raise ValueError("a write starts with begin_attempt, which records its intent")
        check_transition(expected, target, actor)
        with self._transaction():
            row, _, post_id = self._request(key)
            if row.state != expected:
                raise StateConflict(f"request {key} is {row.state}, not {expected}")
            if target is S.APPROVED:
                if row.plan_state != "current":
                    raise WriteBlocked(f"plan revision {key.revision} is {row.plan_state}")
                earlier = [
                    state
                    for revision, _, state in self._post_states(post_id, exclude=row.id)
                    if revision < key.revision
                ]
                if not may_approve_new_revision(earlier):
                    raise WriteBlocked("an earlier revision of this post left a stock change behind")
            if row.state in (S.APPLYING, S.UNDOING):
                self._finish_attempt(row.id, target)
            self._set_state(row.id, expected, target, actor)

    def _finish_attempt(self, request_id: int, target: RequestState) -> None:
        outcome = OUTCOMES.get(target)
        if outcome is None:
            raise AssertionError(f"no attempt outcome for {target}")
        cursor = self._db.execute(
            "UPDATE request_attempts SET outcome = ?, finished_at = ? WHERE request_id = ? AND outcome IS NULL",
            (outcome, self._now(), request_id),
        )
        if cursor.rowcount != 1:
            raise AssertionError(f"request {request_id} has {cursor.rowcount} open attempts, expected 1")

    def begin_attempt(
        self,
        key: RequestKey,
        kind: AttemptKind,
        *,
        expected: RequestState,
        actor: Actor,
        snapshot: Mapping[str, Any],
        request_body: Mapping[str, Any],
        lock_owner: str,
    ) -> Attempt:
        """Store the intent of a write, and move the request to `applying` or `undoing`, in one transaction.

        Returns the marker to write into the notes. The caller sends the write only after this returns.
        Raises LockLost unless `lock_owner` holds the instance lock, StateConflict if someone else
        moved the request first, and WriteBlocked if the post or plan forbids a write now:

        - another write of the post is in flight or not yet read back (`applied`);
        - for an apply: the plan is not current, a read-back of the post found something unexpected,
          an earlier revision left a stock change behind, or an earlier request of the plan is not
          verified (requests run in order and stop at the first failure; rejected ones are skipped).
        """
        target = S.APPLYING if kind is AttemptKind.APPLY else S.UNDOING
        check_transition(expected, target, actor)
        with self._transaction():
            self._check_lock(lock_owner)
            row, _, post_id = self._request(key)
            if row.state != expected:
                raise StateConflict(f"request {key} is {row.state}, not {expected}")
            everything = self._post_states(post_id, exclude=row.id)
            others = [state for _, _, state in everything]
            if any(state in NOT_READ_BACK for state in others):
                raise WriteBlocked("another write of this post is in flight or not read back yet")
            if kind is AttemptKind.APPLY:
                if row.plan_state != "current":
                    raise WriteBlocked(f"plan revision {key.revision} is {row.plan_state}")
                if S.VERIFY_FAILED in others:
                    raise WriteBlocked("a read-back of this post found something unexpected")
                earlier = [state for revision, _, state in everything if revision < key.revision]
                if not may_approve_new_revision(earlier):
                    raise WriteBlocked("an earlier revision of this post left a stock change behind")
                before = [
                    state
                    for revision, sequence, state in everything
                    if revision == key.revision and sequence < key.sequence
                ]
                if any(state not in (S.VERIFIED, S.REJECTED) for state in before):
                    raise WriteBlocked("an earlier request of this plan is not verified")
            (count,) = self._db.execute(
                "SELECT count(*) FROM request_attempts WHERE request_id = ? AND kind = ?", (row.id, kind.value)
            ).fetchone()
            if count >= MAX_ATTEMPT:
                raise WriteBlocked(f"{MAX_ATTEMPT} attempts is the limit")
            marker = Marker(row.marker_id, kind, int(count) + 1)
            self._set_state(row.id, expected, target, actor)
            cursor = self._db.execute(
                "INSERT INTO request_attempts (request_id, kind, attempt, marker, snapshot, request_body, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.id,
                    kind.value,
                    marker.attempt,
                    str(marker),
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                    json.dumps(request_body, sort_keys=True, separators=(",", ":")),
                    self._now(),
                ),
            )
            attempt_id = int(cursor.lastrowid or 0)
            self._audit(actor, "attempt_started", post_id=post_id, request_id=row.id, attempt_id=attempt_id)
            return Attempt(attempt_id, marker)

    def markers(self, key: RequestKey) -> list[Marker]:
        """Every marker written or about to be written for the request, apply and undo, oldest first."""
        row = self.request(key)
        rows = self._db.execute(
            "SELECT marker FROM request_attempts WHERE request_id = ? ORDER BY id", (row.id,)
        ).fetchall()
        return [Marker.parse(marker) for (marker,) in rows]

    # The single-instance lock

    def _check_lock(self, owner: str) -> None:
        row = self._db.execute("SELECT owner FROM instance_lock WHERE id = 1").fetchone()
        if row is None or row[0] != owner:
            raise LockLost("this instance does not hold the lock")

    def acquire_lock(self, owner: str, *, stale_after: timedelta) -> None:
        """Take the lock, or take it over when its heartbeat is older than `stale_after`.

        `owner` must be unique per process: use `new_lock_owner()`. Two processes with the same owner
        would both hold the lock.
        """
        with self._transaction():
            row = self._db.execute("SELECT owner, heartbeat_at FROM instance_lock WHERE id = 1").fetchone()
            if row is not None and row[0] != owner:
                age = self._clock_now() - datetime.fromisoformat(row[1])
                if age < stale_after:
                    raise InstanceLocked("another instance is running on this database")
                self._audit("instance", "lock_taken_over")
            self._db.execute(
                "INSERT INTO instance_lock (id, owner, heartbeat_at) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET owner = excluded.owner, heartbeat_at = excluded.heartbeat_at",
                (owner, self._now()),
            )

    def heartbeat(self, owner: str) -> None:
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE instance_lock SET heartbeat_at = ? WHERE id = 1 AND owner = ?", (self._now(), owner)
            )
            if cursor.rowcount != 1:
                raise LockLost("another instance took over the lock")

    # The Slack event queue

    def enqueue_event(self, key: str, kind: EventKind, body: str) -> bool:
        """Store a raw payload before it is acknowledged. False when the key is already stored (a Slack retry)."""
        with self._transaction():
            if self._db.execute("SELECT 1 FROM slack_events WHERE event_key = ?", (key,)).fetchone():
                return False
            cursor = self._db.execute(
                "INSERT INTO slack_events (event_key, kind, received_at, body) VALUES (?, ?, ?, ?)",
                (key, kind, self._now(), body),
            )
            self._audit("ingest", "event_stored", slack_event_id=int(cursor.lastrowid or 0))
            return True

    def claim_events(self, owner: str, *, limit: int = 10) -> list[QueuedEvent]:
        """The oldest pending events, now processing by `owner`, which must hold the instance lock.

        Delivery is at least once: if another instance takes the lock over, it runs the event again, and
        this instance learns it only at `finish_event`. Processing must check the lock before any write.
        """
        if limit < 1:
            raise ValueError("claim at least one event")
        with self._transaction():
            self._check_lock(owner)
            rows = self._db.execute(
                "SELECT id, event_key, kind, body, attempts FROM slack_events WHERE status = 'pending'"
                " ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                self._db.execute(
                    "UPDATE slack_events SET status = 'processing', attempts = attempts + 1, claimed_by = ?"
                    " WHERE id = ?",
                    (owner, row[0]),
                )
            return [QueuedEvent(row[0], row[1], row[2], row[3], row[4] + 1) for row in rows]

    def finish_event(self, owner: str, event_id: int, status: EventStatus, outcome: str = "") -> None:
        """Record how processing ended. `outcome` is a drop reason or an error class, never message text."""
        if status not in (EventStatus.DONE, EventStatus.DROPPED, EventStatus.FAILED):
            raise ValueError("an event finishes as done, dropped or failed")
        if outcome and not re.fullmatch(r"[a-z0-9_]{1,64}", outcome):
            raise ValueError("an outcome is a short code such as a drop reason, never text")
        with self._transaction():
            self._check_lock(owner)
            cursor = self._db.execute(
                "UPDATE slack_events SET status = ?, outcome = ? WHERE id = ? AND status = 'processing'"
                " AND claimed_by = ?",
                (status.value, outcome or None, event_id, owner),
            )
            if cursor.rowcount != 1:
                raise StateConflict("the event is not being processed by this instance")
            self._audit("ingest", f"event_{status.value}", slack_event_id=event_id)

    def requeue_unfinished(self, owner: str, *, max_attempts: int = 3) -> int:
        """At startup, put events that an earlier instance left processing back in the queue.

        Events claimed by `owner` itself are left alone: they are still being processed. An event claimed
        `max_attempts` times fails, so one bad payload cannot crash the connector in a loop. Returns how many
        events went back to pending.
        """
        with self._transaction():
            self._check_lock(owner)
            rows = self._db.execute(
                "SELECT id, attempts FROM slack_events WHERE status = 'processing' AND claimed_by IS NOT ?", (owner,)
            ).fetchall()
            requeued = 0
            for event_id, attempts in rows:
                if attempts >= max_attempts:
                    self._db.execute(
                        "UPDATE slack_events SET status = 'failed', outcome = 'too_many_attempts' WHERE id = ?",
                        (event_id,),
                    )
                    self._audit("ingest", "event_failed", slack_event_id=event_id)
                else:
                    self._db.execute(
                        "UPDATE slack_events SET status = 'pending', claimed_by = NULL WHERE id = ?", (event_id,)
                    )
                    self._audit("ingest", "event_requeued", slack_event_id=event_id)
                    requeued += 1
            return requeued

    def event_status(self, event_id: int) -> tuple[EventStatus, int, str | None]:
        """Status, attempts and outcome of a stored event."""
        row = self._db.execute(
            "SELECT status, attempts, outcome FROM slack_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return EventStatus(row[0]), int(row[1]), row[2]

    def release_lock(self, owner: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM instance_lock WHERE id = 1 AND owner = ?", (owner,))
