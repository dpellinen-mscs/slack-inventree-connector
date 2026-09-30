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
from decimal import Decimal
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


class DryRunOutcome(StrEnum):
    """How a post's dry run ended. Only `replied` has a reply in the thread."""

    REPLIED = "replied"
    SILENT = "silent"
    """Declined, or not a movement: no reply by design."""
    WITHHELD = "withheld"
    """A message cannot be read again, was edited or deleted first, or is too old to answer."""
    FAILED = "failed"
    """The pipeline raised on this post."""
    GAVE_UP = "gave_up"
    """Posting the reply kept failing."""
    UNCHANGED = "unchanged"
    """A revision only: the post reads as the reply that stands, so nothing was posted."""
    RETRACTED = "retracted"
    """A revision only: the reply that stood no longer does, and a notice in the thread says so."""


@dataclass(frozen=True)
class WaitingPost:
    key: PostKey
    newest_ts: str
    """The timestamp of the post's newest message."""
    has_key_message: bool
    """Whether the message the post is keyed on (the thread root) is in it yet."""
    revision: int = 1
    """1 for the first dry run. More: the post has had its dry run and a message joined it since."""


@dataclass(frozen=True)
class DryRunRecord:
    """What a post's dry run decided. `result` quotes words of the post: never log it."""

    outcome: DryRunOutcome
    extractor: str | None
    result: Mapping[str, Any] | None
    """None when the dry run stored none, or once retention expired it."""
    revision: int = 1
    reply_ts: str | None = None
    """The timestamp of what was posted in the thread: the reply, or a retraction notice."""


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

    def posts_waiting_for_dry_run(self, revisable_since: Decimal | None = None) -> list[WaitingPost]:
        """Posts without a dry-run outcome yet, then posts that a message joined after their last dry run (a
        revision), each oldest first.

        `revisable_since`: only posts keyed on a message from this Slack time on are revised. An older post's
        reply has expired with its plan: a message that joins it is kept, and starts no revision. It also keeps
        the step from reading every post ever answered.
        """
        since = "-1" if revisable_since is None else format(revisable_since, "f")
        first = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts, m.ts, 1 FROM posts p JOIN post_messages m ON m.post_id = p.id"
            " WHERE p.dry_run_at IS NULL ORDER BY p.id"
        ).fetchall()
        revised = self._db.execute(
            "WITH recent AS ("
            " SELECT id FROM posts WHERE dry_run_at IS NOT NULL AND CAST(ts AS REAL) >= CAST(? AS REAL)"
            "),"
            " last AS ("
            " SELECT d.post_id, coalesce(max(r.read_through), d.read_through) AS through, count(r.id) + 2 AS next"
            " FROM recent JOIN dry_runs d ON d.post_id = recent.id"
            " LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id GROUP BY d.post_id"
            ") SELECT p.workspace_id, p.channel_id, p.ts, m.ts, last.next FROM last"
            " JOIN posts p ON p.id = last.post_id JOIN post_messages m ON m.post_id = p.id"
            " WHERE EXISTS (SELECT 1 FROM post_messages n WHERE n.post_id = p.id AND n.id > last.through)"
            " ORDER BY p.id",
            (since,),
        ).fetchall()
        found: dict[PostKey, tuple[int, list[str]]] = {}
        for workspace, channel, post_ts, ts, revision in [*first, *revised]:
            found.setdefault(PostKey(workspace, channel, post_ts), (int(revision), []))[1].append(ts)
        return [WaitingPost(key, max(ts, key=seconds), key.ts in ts, revision) for key, (revision, ts) in found.items()]

    def post_of(self, message: PostKey) -> PostKey | None:
        """The post that holds this message, whether or not it has had its dry run."""
        row = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts FROM post_messages m JOIN posts p ON p.id = m.post_id"
            " WHERE m.workspace_id = ? AND m.channel_id = ? AND m.ts = ?",
            (message.workspace_id, message.channel_id, message.ts),
        ).fetchone()
        return None if row is None else PostKey(row[0], row[1], row[2])

    def waiting_post_of(self, message: PostKey) -> PostKey | None:
        """The post that holds this message, if that post has no dry-run outcome yet."""
        row = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts FROM post_messages m JOIN posts p ON p.id = m.post_id"
            " WHERE m.workspace_id = ? AND m.channel_id = ? AND m.ts = ? AND p.dry_run_at IS NULL",
            (message.workspace_id, message.channel_id, message.ts),
        ).fetchone()
        return None if row is None else PostKey(row[0], row[1], row[2])

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

    def note_revision(self, owner: str, message: PostKey, slack_event_id: int | None = None) -> None:
        """Remember that a message was edited or deleted, whether or not the message itself has arrived. Idempotent."""
        with self._transaction():
            self._check_lock(owner)
            key = (message.workspace_id, message.channel_id, message.ts)
            if self._db.execute(
                "SELECT 1 FROM revised_messages WHERE workspace_id = ? AND channel_id = ? AND ts = ?", key
            ).fetchone():
                return
            self._db.execute(
                "INSERT INTO revised_messages (workspace_id, channel_id, ts, slack_event_id, noted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (*key, slack_event_id, self._now()),
            )
            self._audit("ingest", "revision_noted", slack_event_id=slack_event_id)

    def post_has_revision(self, key: PostKey) -> bool:
        """Whether any message of the post, or the message it is keyed on, was edited or deleted, in whatever
        order the events arrived."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        row = self._db.execute(
            "SELECT 1 FROM revised_messages r WHERE EXISTS ("
            " SELECT 1 FROM post_messages m WHERE m.post_id = ?1"
            " AND m.workspace_id = r.workspace_id AND m.channel_id = r.channel_id AND m.ts = r.ts"
            ") OR EXISTS ("  # the thread root the post is keyed on, even when it never arrived
            " SELECT 1 FROM posts p WHERE p.id = ?1"
            " AND p.workspace_id = r.workspace_id AND p.channel_id = r.channel_id AND p.ts = r.ts"
            ") LIMIT 1",
            (post_id,),
        ).fetchone()
        return row is not None

    def finish_dry_run(
        self,
        owner: str,
        key: PostKey,
        outcome: DryRunOutcome,
        reply_ts: str | None = None,
        *,
        extractor: str | None = None,
        result: Mapping[str, Any] | None = None,
        revision: int = 1,
        read: Sequence[str] | None = None,
    ) -> None:
        """Record the outcome of the post's dry run `revision`, and the timestamp of what was posted when Slack
        gave one. Each revision once, in order.

        `extractor` and `result` are what was decided and why (the candidates, matches and reasons): they are
        stored in the same transaction, so a finished post always has its record. `read` is the timestamps of
        the messages that were read; None means every message the post holds now (a post withheld unread). A
        message that joins the post later makes it wait for the next revision.
        """
        posted = (DryRunOutcome.REPLIED, DryRunOutcome.RETRACTED)
        if reply_ts is not None and (outcome not in posted or not _SLACK_TS.fullmatch(reply_ts)):
            raise ValueError("only a reply has a timestamp, in Slack's form")
        if revision < 1 or (revision == 1 and outcome in (DryRunOutcome.UNCHANGED, DryRunOutcome.RETRACTED)):
            raise ValueError("only a revision is unchanged or retracted")
        stored = None if result is None else json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(key)
            if post_id is None:
                raise KeyError("no such post")
            through = self._read_through(post_id, read)
            now = self._now()
            if revision > 1:
                last = self._db.execute(
                    "SELECT coalesce(max(r.read_through), d.read_through), count(r.id) + 1 FROM dry_runs d"
                    " LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id WHERE d.post_id = ?",
                    (post_id,),
                ).fetchone()
                if last is None or last[0] is None or revision != last[1] + 1 or through <= last[0]:
                    raise StateConflict(f"the post has no revision {revision} waiting")
                self._db.execute(
                    "INSERT INTO dry_run_revisions"
                    " (post_id, revision, read_through, outcome, extractor, result, reply_ts, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (post_id, revision, through, outcome.value, extractor or None, stored, reply_ts, now),
                )
                self._audit(Actor.PLANNER, f"revision_{outcome.value}", post_id=post_id)
                return
            cursor = self._db.execute(
                "UPDATE posts SET dry_run_at = ?, reply_ts = ? WHERE id = ? AND dry_run_at IS NULL",
                (now, reply_ts, post_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict("the post already has its dry-run outcome")
            self._db.execute(
                "INSERT INTO dry_runs (post_id, outcome, extractor, result, read_through, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (post_id, outcome.value, extractor or None, stored, through, now),
            )
            self._audit(Actor.PLANNER, f"dry_run_{outcome.value}", post_id=post_id)

    def _read_through(self, post_id: int, read: Sequence[str] | None) -> int:
        """The newest post_messages.id of those read (all of the post's messages when None)."""
        if read is None:
            row = self._db.execute("SELECT max(id), count(*) FROM post_messages WHERE post_id = ?", (post_id,))
        else:
            wanted = sorted(set(read))
            marks = ", ".join("?" for _ in wanted)
            row = self._db.execute(
                f"SELECT max(id), count(*) FROM post_messages WHERE post_id = ? AND ts IN ({marks})",  # noqa: S608
                (post_id, *wanted),
            )
        through, count = row.fetchone()
        if through is None or (read is not None and count != len(set(read))):
            raise ValueError("a dry run reads messages of its own post")
        return int(through)

    def dry_run(self, key: PostKey) -> DryRunRecord | None:
        """The record of a post's latest dry run, or None when it has not had one (or had it before migration
        0004)."""
        history = self.dry_run_history(key)
        return history[-1] if history else None

    def dry_run_history(self, key: PostKey) -> list[DryRunRecord]:
        """The records of the post's first dry run and of each revision since, in order."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT d.outcome, d.extractor, d.result, 1, p.reply_ts FROM dry_runs d JOIN posts p ON p.id = d.post_id"
            " WHERE d.post_id = ?1 UNION ALL"
            " SELECT outcome, extractor, result, revision, reply_ts FROM dry_run_revisions WHERE post_id = ?1"
            " ORDER BY 4",
            (post_id,),
        ).fetchall()
        return [
            DryRunRecord(DryRunOutcome(outcome), extractor, None if result is None else json.loads(result), rev, ts)
            for outcome, extractor, result, rev, ts in rows
        ]

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
            return self._enqueue(key, kind, body)

    def _enqueue(self, key: str, kind: EventKind, body: str) -> bool:
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

    # Catch-up after downtime

    def message_placed(self, message: PostKey) -> bool:
        """Whether the message is in a post already."""
        row = self._db.execute(
            "SELECT 1 FROM post_messages WHERE workspace_id = ? AND channel_id = ? AND ts = ?",
            (message.workspace_id, message.channel_id, message.ts),
        ).fetchone()
        return row is not None

    def catch_up_mark(self, workspace_id: str, channel_id: str) -> tuple[str, str] | None:
        """Where the channel's catch-up started, and up to where its history has been read: two Slack
        timestamps. None before the channel's first sweep."""
        row = self._db.execute(
            "SELECT since_ts, swept_to FROM catch_up WHERE workspace_id = ? AND channel_id = ?",
            (workspace_id, channel_id),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def mark_caught_up(self, workspace_id: str, channel_id: str, swept_to: str) -> None:
        """A whole sweep of the channel has ended at `swept_to`. The first mark of a channel is also where
        its catch-up starts from, for good. A mark never moves back: an older one changes nothing.

        Needs no instance lock: like `enqueue_event`, it only records what was read from Slack.
        """
        self.store_sweep(workspace_id, channel_id, swept_to, ())

    def store_sweep(
        self, workspace_id: str, channel_id: str, swept_to: str | None, events: Sequence[tuple[str, str]]
    ) -> int:
        """Store what a sweep of the channel found, as (event key, body), and mark the channel as swept to
        `swept_to`, all at once. None leaves the mark alone: part of the channel could not be read. Returns
        how many events were new.

        One transaction, so the worker sees either none of a sweep's events or all of them. It answers no
        post while it sees events pending, so a post the sweep found is not answered before the siblings and
        replies the same sweep found. Needs no instance lock.
        """
        if swept_to is not None and not _SLACK_TS.fullmatch(swept_to):
            raise ValueError(f"not a Slack timestamp: {swept_to!r}")
        with self._transaction():
            stored = sum(self._enqueue(key, "event", body) for key, body in events)
            if swept_to is None:
                return stored
            mark = self.catch_up_mark(workspace_id, channel_id)
            if mark is None:
                self._db.execute(
                    "INSERT INTO catch_up (workspace_id, channel_id, since_ts, swept_to, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (workspace_id, channel_id, swept_to, swept_to, self._now()),
                )
            elif seconds(swept_to) > seconds(mark[1]):
                self._db.execute(
                    "UPDATE catch_up SET swept_to = ?, updated_at = ? WHERE workspace_id = ? AND channel_id = ?",
                    (swept_to, self._now(), workspace_id, channel_id),
                )
            return stored

    def release_lock(self, owner: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM instance_lock WHERE id = 1 AND owner = ?", (owner,))
