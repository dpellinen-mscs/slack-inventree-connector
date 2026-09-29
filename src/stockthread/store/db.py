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
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

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


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
            connection.execute("PRAGMA journal_mode = WAL")
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
        if self.schema_version > len(migrations):
            raise SchemaTooNew(
                f"the database has schema version {self.schema_version}; this version knows {len(migrations)}"
            )
        for version, script in migrations:
            if version <= self.schema_version:
                continue
            # executescript runs the whole file; the transaction and the version bump are in the script,
            # so a failed migration leaves the previous version in place.
            try:
                self._db.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat(timespec="microseconds")

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
        """Create the post, or return the id of the existing one. The key is fixed from here on."""
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

    def create_plan(self, post: PostKey, input_hash: str, operations: Sequence[Operation]) -> int:
        """Store a new plan revision with one planned request per operation, and return its revision.

        The earlier current revision becomes stale, and its planned requests are cancelled. Its
        approved requests stay approved but can no longer start an attempt, because only a current
        plan can be applied.
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
                for (request_id,) in self._db.execute(
                    "SELECT id FROM requests WHERE plan_id = ? AND state = 'planned'", (plan_id,)
                ).fetchall():
                    self._set_state(request_id, S.PLANNED, S.CANCELLED, Actor.PLANNER)
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

    def _post_states(self, post_id: int, *, exclude: int) -> list[tuple[int, RequestState]]:
        """(revision, state) of every other request of the post."""
        rows = self._db.execute(
            "SELECT p.revision, r.state FROM requests r JOIN plans p ON p.id = r.plan_id"
            " WHERE p.post_id = ? AND r.id <> ?",
            (post_id, exclude),
        ).fetchall()
        return [(int(revision), S(state)) for revision, state in rows]

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
                    state for revision, state in self._post_states(post_id, exclude=row.id) if revision < key.revision
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
    ) -> Attempt:
        """Store the intent of a write, and move the request to `applying` or `undoing`, in one transaction.

        Returns the marker to write into the notes. The caller sends the write only after this returns.
        Raises StateConflict if someone else moved the request first, and WriteBlocked if the post or
        plan forbids a write now: another write of the post may be in flight, a read-back found
        something unexpected (apply only), or the plan is no longer current (apply only).
        """
        target = S.APPLYING if kind is AttemptKind.APPLY else S.UNDOING
        check_transition(expected, target, actor)
        with self._transaction():
            row, _, post_id = self._request(key)
            if row.state != expected:
                raise StateConflict(f"request {key} is {row.state}, not {expected}")
            everything = self._post_states(post_id, exclude=row.id)
            others = [state for _, state in everything]
            if any(state in UNSETTLED for state in others):
                raise WriteBlocked("another write of this post may be in flight")
            if kind is AttemptKind.APPLY:
                if row.plan_state != "current":
                    raise WriteBlocked(f"plan revision {key.revision} is {row.plan_state}")
                if S.VERIFY_FAILED in others:
                    raise WriteBlocked("a read-back of this post found something unexpected")
                earlier = [state for revision, state in everything if revision < key.revision]
                if not may_approve_new_revision(earlier):
                    raise WriteBlocked("an earlier revision of this post left a stock change behind")
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

    def acquire_lock(self, owner: str, *, stale_after: timedelta) -> None:
        """Take the lock, or take it over when its heartbeat is older than `stale_after`."""
        with self._transaction():
            row = self._db.execute("SELECT owner, heartbeat_at FROM instance_lock WHERE id = 1").fetchone()
            if row is not None and row[0] != owner:
                age = self._clock() - datetime.fromisoformat(row[1])
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

    def release_lock(self, owner: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM instance_lock WHERE id = 1 AND owner = ?", (owner,))
