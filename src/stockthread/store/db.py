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

import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from stockthread.core.grouping import Arrival, Placed, choose_post, seconds
from stockthread.core.marker import MAX_ATTEMPT, AttemptKind, Marker, marked, new_marker_id
from stockthread.core.marker_check import Outcome, RecordedAttempt, RecordedRequest
from stockthread.core.matching import Alias, same_words, words
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import (
    NEW_TRY,
    RECEIPT_STATES,
    UNDOABLE,
    UNSETTLED,
    WORKING,
    Actor,
    RequestState,
    check_transition,
    may_approve_new_revision,
)

S = RequestState

MIN_SQLITE = (3, 37, 0)
"""STRICT tables."""

MAX_EVENT_ATTEMPTS = 4
"""Claims of one Slack event, by retries after a transient failure and by restarts alike: the last one fails."""
MAX_EVENT_RETRY_DELAY = timedelta(minutes=5)
"""The longest an event may be put off. A later `not_before` can only come from the clock stepping back: such an
event is claimable at once, so it neither waits for the clock nor keeps the queue from being empty."""

OUTCOMES: dict[RequestState, str] = {
    S.APPLIED: "accepted",
    S.FAILED: "refused",
    S.UNKNOWN: "unknown",
    S.UNDONE: "accepted",
    S.UNDO_FAILED: "refused",
    S.UNDO_UNKNOWN: "unknown",
    S.VERIFY_FAILED: "accepted",  # only from undoing: an accepted undo whose read-back disagrees (ADR 0017)
}
"""The outcome of the open attempt, by the state a request leaves `applying` or `undoing` for."""

NOT_READ_BACK: frozenset[RequestState] = UNSETTLED | {S.APPLIED}
"""While a request of a post is in one of these, no other write of the post may start: its read-back
compares quantities with its snapshot, and another write would change them."""

RECOVERED_FOUND: dict[tuple[RequestState, RequestState], bool] = {
    (S.UNKNOWN, S.APPLIED): True,
    (S.UNKNOWN, S.FAILED): False,
    (S.UNDO_UNKNOWN, S.UNDONE): True,
    (S.UNDO_UNKNOWN, S.UNDO_FAILED): False,
}
"""What recovery must have concluded about the marker for these transitions (design section 7, the recovery
table). `unknown` or `undo_unknown` to `verify_failed` follows either conclusion."""


def _check_recovered(expected: RequestState, target: RequestState, marker_found: bool | None) -> None:
    if expected not in (S.UNKNOWN, S.UNDO_UNKNOWN):
        if marker_found is not None:
            raise ValueError("only recovery of an unknown outcome concludes whether the marker was found")
        return
    if marker_found is None:
        raise ValueError("recovery records whether it found the marker")
    if RECOVERED_FOUND.get((expected, target), marker_found) is not marker_found:
        raise ValueError(f"{expected} to {target} does not follow a marker {'found' if marker_found else 'not found'}")


class SchemaTooNew(Exception):
    """The database was migrated by a newer version of the connector."""


class SchemaTooOld(Exception):
    """A read-only open found a database that this version has not migrated yet."""


class AliasKind(StrEnum):
    PART = "part"
    LOCATION = "location"


@dataclass(frozen=True)
class StoredAlias:
    """A stored alias as an admin sees it (`stockthread aliases`)."""

    id: int
    kind: AliasKind
    text: str
    record: int
    user: str | None
    """The Slack user whose posts use it; None for everyone's."""
    rivals: frozenset[int] | None
    learned_from: RequestKey | None
    """The verified request whose confirmation taught it last; None for one an admin typed."""
    created_at: str
    updated_at: str


class StateConflict(Exception):
    """The request is not in the expected state: someone else moved it first. Nothing changed."""


class WriteBlocked(Exception):
    """The transition is allowed by the state machine, but the post or plan forbids it now."""


class EventsWaiting(Exception):
    """An event waits in the queue, so no apply starts now: an edit or a deletion among them may take the approval
    back (`Worker.drain` handles it first). Nothing changed; the request may be tried again."""


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
    receipt_state: RequestState | None = None
    """The state the last receipt of the plan reported for the request; None when no receipt reported it."""


@dataclass(frozen=True)
class Attempt:
    id: int
    marker: Marker


@dataclass(frozen=True)
class UnknownAttempt:
    """The last attempt of a request whose outcome is unknown and not yet concluded by recovery."""

    marker: Marker
    age: timedelta
    """How long ago its outcome became unknown (the client gave up, or the send failed), by the store's clock. Not
    since the intent was stored: the send has no overall deadline (connect, DNS), so the server may get the write
    long after that."""
    snapshot: Any
    """As stored (`Snapshot.as_json`)."""
    body: Any
    """The request body as stored and sent."""


@dataclass(frozen=True)
class AppliedAttempt:
    """The last attempt of an `applied` request: the write InvenTree accepted, or whose marker recovery found."""

    marker: Marker
    snapshot: Any
    """As stored (`Snapshot.as_json`)."""
    body: Any
    """The request body as stored and sent."""


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
    plan_from: str | None = None
    """For a revision: the Slack time the post's plan expires from, fixed by its first dry run (migration 0015)."""


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
    changes_through: int | None = None
    """The newest noted edit or deletion the dry run knew of."""


@dataclass(frozen=True)
class NewQuestion:
    """A question asked in a dry-run reply, as stored with it. `question` and each choice are JSON of ids, codes
    and primary keys (core.questions), never words of the post."""

    question: Mapping[str, Any]
    approver_only: bool
    buttons: Sequence[tuple[str, Mapping[str, Any]]]
    """One (button id, choice) per button, in order. A question answered by a thread reply has none."""
    poster: str
    """The Slack user id of the post's author, as the dry run read the post."""
    expires_from: str
    """The Slack time the plan's expiry counts from."""


@dataclass(frozen=True)
class PlanRow:
    """A stored plan revision and its requests, in the post's order."""

    post: PostKey
    revision: int
    input_hash: str
    """`plan_record.plan_hash` of the dry-run record the plan was made from (ADR 0016)."""
    current: bool
    """False once a newer reading, a joined message or a noted change made it stale."""
    requests: tuple[RequestRow, ...]
    approved_by: str | None
    """The approver whose Apply click approved the plan; None when the policy approved it, or nothing did."""
    receipt_ts: str | None = None
    """The ts of the plan's receipt in the thread (`Store.receipt_posted`); None until it is posted."""


@dataclass(frozen=True)
class NewPlan:
    """The plan revision a dry-run reply that could apply in full makes (ADR 0016), stored with the reply."""

    input_hash: str
    """`plan_record.plan_hash` of the record the reply is stored with."""
    operations: Sequence[Operation]
    """One per movement, in the post's order."""


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    STALE = "stale"
    """A newer dry run of the post stands in the thread: a revised reply, or a notice that none stands."""


@dataclass(frozen=True)
class ReviewItem:
    id: int
    post: PostKey
    revision: int
    """The dry run that asked it."""
    question: Mapping[str, Any]
    approver_only: bool
    channel_id: str
    message_ts: str | None
    """The reply the question was asked in. None: Slack gave no usable timestamp, and no click fits it."""
    status: QuestionStatus
    poster_id: str | None = None
    """Who wrote the post when the question was asked. None for a question from before migration 0011."""
    expires_from: str | None = None
    """The Slack time the plan's expiry counts from. None for a question from before migration 0011."""


class Answered(StrEnum):
    ANSWERED = "answered"
    NOT_OPEN = "already_answered"
    """Answered, or gone stale, since it was checked."""
    POST_CHANGED = "post_changed"
    """The post changed after the dry run that asked the question, or a later dry run read it otherwise: the
    question may no longer be about what the post says."""
    EXPIRED = "expired"
    """Its plan expired and its buttons were taken down (migration 0014)."""


@dataclass(frozen=True)
class Button:
    id: str
    item: ReviewItem
    position: int
    choice: Mapping[str, Any]


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


def new_button_id() -> str:
    """A random button id of 128 bits: all a click carries (design section 4, "Interactivity")."""
    return secrets.token_urlsafe(16)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _review_item(post: PostKey, row: Sequence[Any]) -> ReviewItem:
    item_id, revision, question, approver_only, channel_id, message_ts, status, poster_id, expires_from = row
    return ReviewItem(
        int(item_id),
        post,
        int(revision),
        json.loads(question),
        bool(approver_only),
        channel_id,
        message_ts,
        QuestionStatus(status),
        poster_id,
        expires_from,
    )


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


def posters_seen(path: str | Path) -> list[str] | None:
    """The Slack user ids of everyone with a message in a post, read without changing the database (the setup
    check), or None when there is no database yet."""
    if not Path(path).exists():
        return None
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        return [row[0] for row in connection.execute("SELECT DISTINCT user_id FROM post_messages ORDER BY user_id")]
    finally:
        connection.close()


def _create_private(path: str | Path) -> None:
    """Create a missing database file readable by its owner only, whatever the umask: it holds every accepted Slack
    post in full (threat model T10). SQLite gives the -wal and -shm files the mode of the database file. An existing
    file keeps its mode."""
    if str(path) == ":memory:":
        return
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


class Store:
    def __init__(self, connection: sqlite3.Connection, clock: Callable[[], datetime]) -> None:
        self._db = connection
        self._clock = clock

    @classmethod
    def open(cls, path: str | Path, *, clock: Callable[[], datetime] = _utcnow) -> Store:
        """Open the database, creating and migrating it as needed."""
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(f"SQLite {'.'.join(map(str, MIN_SQLITE))} or later is needed")
        _create_private(path)
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

    @classmethod
    def open_read_only(cls, path: str | Path) -> Store:
        """Open an existing database without changing it (`stockthread trail`). Raises FileNotFoundError when there
        is none, and SchemaTooNew or SchemaTooOld unless its schema is the one this version writes."""
        if not Path(path).exists():
            raise FileNotFoundError(f"no database at {path}")
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            store = cls(connection, _utcnow)
            current, known = store.schema_version, len(_migrations())
            if current > known:
                raise SchemaTooNew(f"the database has schema version {current}; this version knows {known}")
            if current < known:
                raise SchemaTooOld(f"the database has schema version {current}; start the connector once to upgrade it")
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
        return self._stamp(self._clock_now())

    @staticmethod
    def _stamp(at: datetime) -> str:
        return at.astimezone(UTC).isoformat(timespec="microseconds")

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

        A message that joins a post makes its current plan stale at once, in the same transaction: the post is due
        for a revision, and until that revision is recorded no plan of it may be approved. A revision that reads
        the same makes the plan again (`finish_dry_run`).
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
            if not created:
                self._replace_plan(post_id, None)
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

    def posts_waiting_for_dry_run(
        self, revisable_since: Decimal | None = None, *, late_answers_since: Decimal | None = None
    ) -> list[WaitingPost]:
        """Posts without a dry-run outcome yet, then posts that a message joined, that had a message edited or
        deleted, or that had a question answered, after their last dry run (a revision), each oldest first.

        `revisable_since`: only posts whose plan expires from this Slack time on (`plan_from`, fixed by the first
        dry run) are revised. An older post's reply has expired with its plan: a message that joins it is kept, and
        starts no revision. It also keeps the step from reading every post ever revised.

        `late_answers_since`: also older posts whose plan expires from this Slack time on and that have an answer
        no dry run has read (taken just before the plan expired; its revision says it came too late). None: none.
        Bounded, so an upgrade or long downtime does not post notices into threads long gone quiet.
        """
        since = "-1" if revisable_since is None else format(revisable_since, "f")
        # Above any Slack time: no post is listed for a late answer.
        late = "9e18" if late_answers_since is None else format(late_answers_since, "f")
        first = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts, m.ts, 1, NULL FROM posts p"
            " JOIN post_messages m ON m.post_id = p.id"
            " WHERE p.dry_run_at IS NULL ORDER BY p.id"
        ).fetchall()
        revised = self._db.execute(
            "WITH recent AS ("
            " SELECT id FROM posts WHERE dry_run_at IS NOT NULL AND CAST(plan_from AS REAL) >= CAST(? AS REAL)"
            # An answer no dry run has read yet, of a plan that expired since `late`: it was taken while the plan
            # stood, and its revision says that it came too late. Each answered question is checked against the
            # post's records (by index).
            " UNION SELECT i.post_id FROM review_items i JOIN posts q ON q.id = i.post_id"
            " WHERE i.status = 'answered' AND CAST(q.plan_from AS REAL) >= CAST(? AS REAL)"
            " AND NOT EXISTS (SELECT 1 FROM dry_runs d WHERE d.post_id = i.post_id AND d.answers_through >= i.id)"
            " AND NOT EXISTS (SELECT 1 FROM dry_run_revisions r WHERE r.post_id = i.post_id"
            " AND r.answers_through >= i.id)"
            "),"
            " last AS ("
            " SELECT d.post_id, coalesce(max(r.read_through), d.read_through) AS through,"
            " coalesce(max(r.changes_through), d.changes_through) AS changes,"
            " coalesce(max(r.answers_through), d.answers_through) AS answers, count(r.id) + 2 AS next"
            " FROM recent JOIN dry_runs d ON d.post_id = recent.id"
            " LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id GROUP BY d.post_id"
            ") SELECT p.workspace_id, p.channel_id, p.ts, m.ts, last.next, p.plan_from FROM last"
            " JOIN posts p ON p.id = last.post_id JOIN post_messages m ON m.post_id = p.id"
            " WHERE EXISTS (SELECT 1 FROM post_messages n WHERE n.post_id = p.id AND n.id > last.through)"
            # A change noted since: looked up by message key (its index), never by scanning every change.
            " OR EXISTS (SELECT 1 FROM revised_messages r WHERE r.workspace_id = p.workspace_id"
            " AND r.channel_id = p.channel_id AND r.ts = p.ts AND r.id > last.changes)"
            " OR EXISTS (SELECT 1 FROM post_messages n JOIN revised_messages r ON r.workspace_id = n.workspace_id"
            " AND r.channel_id = n.channel_id AND r.ts = n.ts WHERE n.post_id = p.id AND r.id > last.changes)"
            " OR EXISTS (SELECT 1 FROM review_items i WHERE i.post_id = p.id AND i.status = 'answered'"
            " AND i.id > last.answers)"
            " ORDER BY p.id",
            (since, late),
        ).fetchall()
        found: dict[PostKey, tuple[int, str | None, list[str]]] = {}
        for workspace, channel, post_ts, ts, revision, plan_from in [*first, *revised]:
            found.setdefault(PostKey(workspace, channel, post_ts), (int(revision), plan_from, []))[2].append(ts)
        return [
            WaitingPost(key, max(ts, key=seconds), key.ts in ts, revision, plan_from)
            for key, (revision, plan_from, ts) in found.items()
        ]

    def post_of(self, message: PostKey) -> PostKey | None:
        """The post that holds this message, whether or not it has had its dry run."""
        row = self._db.execute(
            "SELECT p.workspace_id, p.channel_id, p.ts FROM post_messages m JOIN posts p ON p.id = m.post_id"
            " WHERE m.workspace_id = ? AND m.channel_id = ? AND m.ts = ?",
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
        """Remember that a message was edited or deleted, whether or not the message itself has arrived.

        Every event is noted, so each later edit of a message makes its post due again. Idempotent: once per
        event, and once per message for a note without an event. The post of the message, or the post it keys,
        loses its current plan at once, as in `place_message`.
        """
        with self._transaction():
            self._check_lock(owner)
            key = (message.workspace_id, message.channel_id, message.ts)
            if slack_event_id is None:
                noted = self._db.execute(
                    "SELECT 1 FROM revised_messages WHERE workspace_id = ? AND channel_id = ? AND ts = ?"
                    " AND slack_event_id IS NULL",
                    key,
                ).fetchone()
            else:
                noted = self._db.execute(
                    "SELECT 1 FROM revised_messages WHERE slack_event_id = ?", (slack_event_id,)
                ).fetchone()
            if noted:
                return
            self._db.execute(
                "INSERT INTO revised_messages (workspace_id, channel_id, ts, slack_event_id, noted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (*key, slack_event_id, self._now()),
            )
            self._audit("ingest", "revision_noted", slack_event_id=slack_event_id)
            # The message's post, or the post it keys: a root that has not arrived still makes its post due.
            for (post_id,) in self._db.execute(
                "SELECT post_id FROM post_messages WHERE workspace_id = ?1 AND channel_id = ?2 AND ts = ?3"
                " UNION SELECT id FROM posts WHERE workspace_id = ?1 AND channel_id = ?2 AND ts = ?3",
                key,
            ).fetchall():
                self._replace_plan(int(post_id), None)

    def post_change_bodies(self, key: PostKey, through: int) -> dict[str, list[str | None]]:
        """Each noted edit or deletion of a message of the post, or of the message it is keyed on, up to the
        change `through` (a `revised_messages.id`): by message timestamp, the raw bodies of the events, in the
        order they were noted. A body is None when retention expired it, when the note has no event, or when
        it was noted before migration 0009 (then a later edit of the message may not have been noted).
        """
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT r.ts, CASE WHEN r.id > (SELECT through FROM revised_messages_before) THEN e.body END"
            " FROM revised_messages r LEFT JOIN slack_events e ON e.id = r.slack_event_id"
            " WHERE r.id <= ?2 AND (EXISTS ("
            " SELECT 1 FROM post_messages m WHERE m.post_id = ?1"
            " AND m.workspace_id = r.workspace_id AND m.channel_id = r.channel_id AND m.ts = r.ts"
            ") OR EXISTS ("
            " SELECT 1 FROM posts p WHERE p.id = ?1"
            " AND p.workspace_id = r.workspace_id AND p.channel_id = r.channel_id AND p.ts = r.ts"
            ")) ORDER BY r.id",
            (post_id, through),
        ).fetchall()
        found: dict[str, list[str | None]] = {}
        for ts, body in rows:
            found.setdefault(ts, []).append(body)
        return found

    def post_has_revision(self, key: PostKey) -> bool:
        """Whether any message of the post, or the message it is keyed on, was edited or deleted, in whatever
        order the events arrived."""
        return self.post_changes_through(key) > 0

    def post_changes_through(self, key: PostKey) -> int:
        """The newest noted edit or deletion (`revised_messages.id`) of a message of the post, or of the message
        it is keyed on; 0 when there is none."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        return self._changes_through(post_id)

    def _changes_through(self, post_id: int) -> int:
        return max(self._changes(post_id), default=0)

    def post_answers_through(self, key: PostKey) -> int:
        """The newest answered question (`review_items.id`) of the post; 0 when none is answered."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        return self._answers_through(post_id)

    def post_answers_read(self, key: PostKey) -> int:
        """The newest answered question the post's newest dry run knew of; 0 when it knew of none."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        row = self._db.execute(
            "SELECT coalesce((SELECT answers_through FROM dry_run_revisions WHERE post_id = ?"
            " ORDER BY revision DESC LIMIT 1), (SELECT answers_through FROM dry_runs WHERE post_id = ?))",
            (post_id, post_id),
        ).fetchone()
        return int(row[0] or 0)

    def _answers_through(self, post_id: int) -> int:
        row = self._db.execute(
            "SELECT max(id) FROM review_items WHERE post_id = ? AND status = 'answered'", (post_id,)
        ).fetchone()
        return int(row[0] or 0)

    def answers(self, key: PostKey, through: int) -> list[tuple[ReviewItem, Mapping[str, Any], str]]:
        """The post's answered questions up to `through`, oldest first: each with its answer and who gave it.
        Answers given before migration 0013 (up to the first dry run's `answers_through`) are never listed."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT id, revision, question, approver_only, channel_id, message_ts, status, poster_id, expires_from,"
            " answer, answered_by FROM review_items WHERE post_id = ?1 AND status = 'answered' AND id <= ?2"
            " AND id > (SELECT answers_through FROM dry_runs WHERE post_id = ?1) ORDER BY id",
            (post_id, through),
        ).fetchall()
        return [(_review_item(key, row[:9]), json.loads(row[9]), str(row[10])) for row in rows]

    def _changes(self, post_id: int) -> set[int]:
        """The ids of every noted edit or deletion of a message of the post, or of the message it is keyed on."""
        rows = self._db.execute(
            "SELECT r.id FROM revised_messages r WHERE EXISTS ("
            " SELECT 1 FROM post_messages m WHERE m.post_id = ?1"
            " AND m.workspace_id = r.workspace_id AND m.channel_id = r.channel_id AND m.ts = r.ts"
            ") OR EXISTS ("  # the thread root the post is keyed on, even when it never arrived
            " SELECT 1 FROM posts p WHERE p.id = ?1"
            " AND p.workspace_id = r.workspace_id AND p.channel_id = r.channel_id AND p.ts = r.ts"
            ")",
            (post_id,),
        ).fetchall()
        return {int(row[0]) for row in rows}

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
        changes: int | None = None,
        answers: int | None = None,
        question: NewQuestion | None = None,
        plan: NewPlan | None = None,
    ) -> None:
        """Record the outcome of the post's dry run `revision`, and the timestamp of what was posted when Slack
        gave one. Each revision once, in order.

        `extractor` and `result` are what was decided and why (the candidates, matches and reasons): they are
        stored in the same transaction, so a finished post always has its record. `read` is the timestamps of
        the messages that were read; None means every message the post holds now (a post withheld unread). A
        message that joins the post later makes it wait for the next revision. `changes` is the newest noted
        edit or deletion the decision knew of (`post_changes_through` when it was decided); None means the
        newest there is now. A change noted later makes the post wait for the next revision too. `answers` is the
        newest answered question the decision knew of (`post_answers_through`), likewise.

        `question` is the question the reply asks, stored with its buttons in the same transaction, so no button
        a reply carries is without its row. A reply or a notice that a revision posts makes the post's open
        question stale: it no longer belongs to what stands in the thread.

        The first dry run fixes the Slack time the post's plan expires from (`plan_from`): its question's
        `expires_from`, or else the time of the newest message it read. The post's revisions, and every question
        asked about it, count from that one time.

        The post's current plan follows the reply that stands in the thread, in the same transaction, so no reply
        is ever stored with another reply's plan. `plan` is the plan of that reply when it could apply in full: for
        `replied`, the plan of the new reply; for `unchanged`, the plan of the reply that still stands, made again
        (the message or change that made the revision due staled it, as `stale_plan` or a reply given up on may
        have). Either becomes the current revision only when this dry run knew of every message, change and answer
        the post holds now. Every other outcome leaves the post with no current plan. A plan replaced becomes stale,
        and its requests that have not started a write are cancelled.
        """
        posted = (DryRunOutcome.REPLIED, DryRunOutcome.RETRACTED)
        if plan is not None and (outcome not in (DryRunOutcome.REPLIED, DryRunOutcome.UNCHANGED) or result is None):
            raise ValueError("only a reply with its record makes a plan")
        if plan is not None and question is not None:
            raise ValueError("a reply that asks a question needs a human: it makes no plan")
        if plan is not None and not plan.operations:
            raise ValueError("a plan revision holds at least one request")
        if reply_ts is not None and (outcome not in posted or not _SLACK_TS.fullmatch(reply_ts)):
            raise ValueError("only a reply has a timestamp, in Slack's form")
        if revision < 1 or (revision == 1 and outcome in (DryRunOutcome.UNCHANGED, DryRunOutcome.RETRACTED)):
            raise ValueError("only a revision is unchanged or retracted")
        if question is not None:
            if outcome is not DryRunOutcome.REPLIED:
                raise ValueError("only a reply asks a question")
            ids = [button_id for button_id, _ in question.buttons]
            if len(set(ids)) != len(ids) or any(len(button_id) < 22 for button_id in ids):
                raise ValueError("every button has its own random id")
        stored = None if result is None else _json(result)
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(key)
            if post_id is None:
                raise KeyError("no such post")
            through = self._read_through(post_id, read)
            if changes is None:
                changes = self._changes_through(post_id)
            elif changes != 0 and changes not in self._changes(post_id):
                raise ValueError("a dry run knows only of changes noted for its post")
            if revision == 1 and answers:
                raise ValueError("a first dry run knows of no answer")
            plan_from = self._plan_from(post_id, revision, through, question)
            now = self._now()
            if revision > 1:
                if answers is None:
                    answers = self._answers_through(post_id)
                elif answers < 0 or answers > self._answers_through(post_id):
                    raise ValueError("a dry run knows only of answers given about its post")
                last = self._db.execute(
                    "SELECT coalesce(max(r.read_through), d.read_through),"
                    " coalesce(max(r.changes_through), d.changes_through), count(r.id) + 1,"
                    " coalesce(max(r.answers_through), d.answers_through) FROM dry_runs d"
                    " LEFT JOIN dry_run_revisions r ON r.post_id = d.post_id WHERE d.post_id = ?",
                    (post_id,),
                ).fetchone()
                if (
                    last is None
                    or last[0] is None
                    or revision != last[2] + 1
                    or through < last[0]
                    or changes < last[1]
                    or answers < last[3]
                    or (through, changes, answers) == (last[0], last[1], last[3])
                ):
                    raise StateConflict(f"the post has no revision {revision} waiting")
                self._db.execute(
                    "INSERT INTO dry_run_revisions (post_id, revision, read_through, changes_through, answers_through,"
                    " outcome, extractor, result, reply_ts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        post_id,
                        revision,
                        through,
                        changes,
                        answers,
                        outcome.value,
                        extractor or None,
                        stored,
                        reply_ts,
                        now,
                    ),
                )
                self._audit(Actor.PLANNER, f"revision_{outcome.value}", post_id=post_id)
                if outcome in posted:
                    self._questions_go_stale(post_id, now)
                if question is not None:
                    self._ask(post_id, key, revision, reply_ts, question, now)
                self._replace_plan(post_id, self._if_newest(post_id, plan, through, changes, answers))
                return
            cursor = self._db.execute(
                "UPDATE posts SET dry_run_at = ?, reply_ts = ?, plan_from = ? WHERE id = ? AND dry_run_at IS NULL",
                (now, reply_ts, plan_from, post_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict("the post already has its dry-run outcome")
            self._db.execute(
                "INSERT INTO dry_runs (post_id, outcome, extractor, result, read_through, changes_through, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (post_id, outcome.value, extractor or None, stored, through, changes, now),
            )
            self._audit(Actor.PLANNER, f"dry_run_{outcome.value}", post_id=post_id)
            if question is not None:
                self._ask(post_id, key, revision, reply_ts, question, now)
            self._replace_plan(post_id, self._if_newest(post_id, plan, through, changes, 0))

    def _if_newest(
        self, post_id: int, plan: NewPlan | None, through: int, changes: int, answers: int
    ) -> NewPlan | None:
        """`plan`, when its dry run knew of every message, change and answer the post holds now; else None. A reply
        recorded late (its call was still running) may be due for the next revision already: no plan of it stands."""
        (newest,) = self._db.execute("SELECT max(id) FROM post_messages WHERE post_id = ?", (post_id,)).fetchone()
        if (newest, changes, answers) != (through, self._changes_through(post_id), self._answers_through(post_id)):
            return None
        return plan

    def _questions_go_stale(self, post_id: int, now: str) -> None:
        cursor = self._db.execute(
            "UPDATE review_items SET status = 'stale', closed_at = ? WHERE post_id = ? AND status = 'open'",
            (now, post_id),
        )
        if cursor.rowcount:
            self._audit(Actor.PLANNER, "question_stale", post_id=post_id)

    def _ask(
        self, post_id: int, key: PostKey, revision: int, reply_ts: str | None, question: NewQuestion, now: str
    ) -> None:
        item = self._db.execute(
            "INSERT INTO review_items (post_id, revision, question, approver_only, channel_id, message_ts, status,"
            " created_at, poster_id, expires_from) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (
                post_id,
                revision,
                _json(question.question),
                int(question.approver_only),
                key.channel_id,
                reply_ts,
                now,
                question.poster,
                question.expires_from,
            ),
        ).lastrowid
        self._db.executemany(
            "INSERT INTO interactions (id, review_item_id, position, choice) VALUES (?, ?, ?, ?)",
            [(button_id, item, n, _json(choice)) for n, (button_id, choice) in enumerate(question.buttons)],
        )
        self._audit(Actor.PLANNER, "question_asked", post_id=post_id)

    def questions(self, key: PostKey) -> list[ReviewItem]:
        """The questions asked about the post, oldest first."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT id, revision, question, approver_only, channel_id, message_ts, status, poster_id, expires_from"
            " FROM review_items WHERE post_id = ? ORDER BY id",
            (post_id,),
        ).fetchall()
        return [_review_item(key, row) for row in rows]

    def answer_question(self, owner: str, button: Button, user: str) -> Answered:
        """Record `user`'s answer by `button`: its question is answered with the button's choice, once. The caller
        checks who may answer, and the click's channel and message, first.

        Refused, and nothing changes, when the question is no longer open, or when the post has moved on from the
        dry run that asked it: a message joined it or one was edited or deleted since its newest dry run, or a
        dry run after the one that asked read it otherwise (only `unchanged` revisions keep a question).
        """
        answer = _json({"button": button.position, **button.choice})
        with self._transaction():
            self._check_lock(owner)
            row = self._db.execute(
                "SELECT i.post_id, i.revision, i.expired_at FROM review_items i"
                " JOIN interactions b ON b.review_item_id = i.id WHERE i.id = ? AND b.id = ? AND i.status = 'open'",
                (button.item.id, button.id),
            ).fetchone()
            if row is None:
                return Answered.NOT_OPEN
            post_id, revision = int(row[0]), int(row[1])
            if row[2] is not None:
                return Answered.EXPIRED
            if self._moved_on(post_id, revision):
                return Answered.POST_CHANGED
            self._db.execute(
                "UPDATE review_items SET status = 'answered', answer = ?, answered_by = ?, closed_at = ?"
                " WHERE id = ? AND status = 'open'",
                (answer, user, self._now(), button.item.id),
            )
            self._audit(Actor.HUMAN, "question_answered", post_id=post_id)
            return Answered.ANSWERED

    def answer_by_message(self, owner: str, item: ReviewItem, ts: str, edited: str | None, user: str) -> Answered:
        """Record `user`'s message `ts` in the post's thread as the answer to `item`, once (design section 4,
        "Answers by thread reply"). The caller judges the message and who may answer first. Only the message's
        timestamp is stored, with the stamp of the edit it was judged as (`edited`), never its text: on replay it is
        read as it is then, and an edit since makes it a thread message.

        Refused, as for a click, when the question is no longer open or has expired, or when the post has moved on
        from the dry run that asked it, except by the answer message itself: it must be the one message read by no
        dry run yet.
        """
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(item.post)
            row = self._db.execute(
                "SELECT revision, expired_at FROM review_items WHERE id = ? AND post_id = ? AND status = 'open'",
                (item.id, post_id),
            ).fetchone()
            if post_id is None or row is None:
                return Answered.NOT_OPEN
            if row[1] is not None:
                return Answered.EXPIRED
            if self._moved_on(post_id, int(row[0]), answer_ts=ts):
                return Answered.POST_CHANGED
            self._db.execute(
                "UPDATE review_items SET status = 'answered', answer = ?, answered_by = ?, closed_at = ?"
                " WHERE id = ? AND status = 'open'",
                (_json({"message": ts, "edited": edited}), user, self._now(), item.id),
            )
            self._audit(Actor.HUMAN, "question_answered", post_id=post_id)
            return Answered.ANSWERED

    def open_question(self, key: PostKey) -> ReviewItem | None:
        """The post's open question, or None (a post has one at most). An expired one is still open."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        row = self._db.execute(
            "SELECT id, revision, question, approver_only, channel_id, message_ts, status, poster_id, expires_from"
            " FROM review_items WHERE post_id = ? AND status = 'open'",
            (post_id,),
        ).fetchone()
        return None if row is None else _review_item(key, row)

    def _moved_on(self, post_id: int, revision: int, *, answer_ts: str | None = None) -> bool:
        """Whether the post moved on from the dry run `revision`: a later one read it otherwise, or a message joined
        or changed since the newest one. With `answer_ts`, that message must be the one that joined, and the only
        one."""
        records = self._db.execute(
            "SELECT 1, read_through, changes_through, outcome FROM dry_runs WHERE post_id = ?1 UNION ALL"
            " SELECT revision, read_through, changes_through, outcome FROM dry_run_revisions WHERE post_id = ?1"
            " ORDER BY 1",
            (post_id,),
        ).fetchall()
        if not records or any(rev > revision and outcome != "unchanged" for rev, _, _, outcome in records):
            return True
        _, through, changes, _ = records[-1]
        if self._changes_through(post_id) > changes:
            return True
        joined = self._db.execute("SELECT ts FROM post_messages WHERE post_id = ? AND id > ?", (post_id, through))
        found = [row[0] for row in joined.fetchall()]
        return found != ([] if answer_ts is None else [answer_ts])

    def expire_questions(self, owner: str, before: Decimal) -> int:
        """Mark open questions whose plan expiry counts from before `before` (Slack seconds) as expired, so their
        buttons are taken down (migration 0014). Questions from before migration 0011 count from the post's own time,
        as a click does. Compared as decimals, as a click's expiry is. Returns how many were marked."""
        rows = self._db.execute(
            "SELECT i.id, i.post_id, coalesce(i.expires_from, p.ts) FROM review_items i"
            " JOIN posts p ON p.id = i.post_id WHERE i.status = 'open' AND i.expired_at IS NULL ORDER BY i.id"
        ).fetchall()
        expired = [(item_id, post_id) for item_id, post_id, since in rows if seconds(since) < before]
        if not expired:  # the common case: no write
            return 0
        marked = 0
        with self._transaction():
            self._check_lock(owner)
            for item_id, post_id in expired:
                cursor = self._db.execute(
                    "UPDATE review_items SET expired_at = ? WHERE id = ? AND status = 'open' AND expired_at IS NULL",
                    (self._now(), item_id),
                )
                if cursor.rowcount:  # not closed since it was read
                    self._audit(Actor.PLANNER, "question_expired", post_id=int(post_id))
                    marked += 1
        return marked

    def closed_with_buttons(self, limit: int) -> list[tuple[ReviewItem, str | None]]:
        """Questions answered, gone stale or expired (still `open`, migration 0014) whose reply still shows them
        (migration 0012), with who answered (None for the others): closed before expired (an upgrade may expire many at
        once), then fewest failed tries, then oldest first."""
        rows = self._db.execute(
            "SELECT i.id, i.revision, i.question, i.approver_only, i.channel_id, i.message_ts, i.status, i.poster_id,"
            " i.expires_from, p.workspace_id, p.channel_id, p.ts, i.answered_by"
            " FROM review_items i JOIN posts p ON p.id = i.post_id"
            " WHERE (i.status <> 'open' OR i.expired_at IS NOT NULL) AND i.buttons = 'shown'"
            " AND i.message_ts IS NOT NULL ORDER BY i.status = 'open', i.buttons_tries, i.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [(_review_item(PostKey(*row[9:12]), row[:9]), row[12]) for row in rows]

    def buttons_taken_down(self, owner: str, item_id: int, *, removed: bool) -> None:
        """The reply of a closed question no longer shows it (`removed`), or the connector gave up on it."""
        with self._transaction():
            self._check_lock(owner)
            row = self._db.execute(
                "SELECT post_id FROM review_items WHERE id = ? AND buttons = 'shown'", (item_id,)
            ).fetchone()
            if row is None:
                return
            self._db.execute(
                "UPDATE review_items SET buttons = ? WHERE id = ?", ("removed" if removed else "left", item_id)
            )
            self._audit(Actor.PLANNER, "buttons_removed" if removed else "buttons_left", post_id=int(row[0]))

    def buttons_failed(self, owner: str, item_id: int) -> int:
        """Count a failed attempt to take a closed question's buttons down. Returns the failed attempts so far."""
        with self._transaction():
            self._check_lock(owner)
            self._db.execute(
                "UPDATE review_items SET buttons_tries = buttons_tries + 1 WHERE id = ? AND buttons = 'shown'",
                (item_id,),
            )
            row = self._db.execute("SELECT buttons_tries FROM review_items WHERE id = ?", (item_id,)).fetchone()
            return 0 if row is None else int(row[0])

    def answer(self, item_id: int) -> tuple[Mapping[str, Any], str] | None:
        """The answer of a question and who gave it, or None while it has none."""
        row = self._db.execute(
            "SELECT answer, answered_by FROM review_items WHERE id = ? AND status = 'answered'", (item_id,)
        ).fetchone()
        return None if row is None else (json.loads(row[0]), str(row[1]))

    def button(self, button_id: str) -> Button | None:
        """The stored button with this id, with its question, or None when there is none. A click carries only
        the id: the caller checks the channel, the message and the user against what is stored."""
        row = self._db.execute(
            "SELECT b.position, b.choice, i.id, i.revision, i.question, i.approver_only, i.channel_id, i.message_ts,"
            " i.status, i.poster_id, i.expires_from, p.workspace_id, p.channel_id, p.ts FROM interactions b"
            " JOIN review_items i ON i.id = b.review_item_id JOIN posts p ON p.id = i.post_id WHERE b.id = ?",
            (button_id,),
        ).fetchone()
        if row is None:
            return None
        item = _review_item(PostKey(*row[11:14]), row[2:11])
        return Button(button_id, item, int(row[0]), json.loads(row[1]))

    def plan_expires_from(self, key: PostKey) -> str | None:
        """The Slack time the post's plan expires from, fixed by its first dry run (migration 0015); None before
        it. KeyError when there is no such post."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        row = self._db.execute("SELECT plan_from FROM posts WHERE id = ?", (post_id,)).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _plan_from(self, post_id: int, revision: int, through: int, question: NewQuestion | None) -> str:
        """The time the post's plan expires from (migration 0015). A revision's is the one its first dry run
        stored. A first dry run takes its question's, which must be the time of one of the post's messages, or
        else the newest time of its messages up to post_messages.id `through`."""
        if revision > 1:
            row = self._db.execute("SELECT plan_from FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None or row[0] is None:
                raise StateConflict("the post's first dry run fixed no plan expiry")
            if question is not None and question.expires_from != row[0]:
                raise ValueError("a question expires with its post's plan")
            return str(row[0])
        if question is not None:
            held = self._db.execute(
                "SELECT 1 FROM post_messages WHERE post_id = ? AND ts = ?", (post_id, question.expires_from)
            ).fetchone()
            if held is None:
                raise ValueError("a plan expires from the time of one of its post's messages")
            return question.expires_from
        times = self._db.execute("SELECT ts FROM post_messages WHERE post_id = ? AND id <= ?", (post_id, through))
        return max((str(row[0]) for row in times), key=seconds)

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
            "SELECT d.outcome, d.extractor, d.result, 1, p.reply_ts, d.changes_through FROM dry_runs d"
            " JOIN posts p ON p.id = d.post_id WHERE d.post_id = ?1 UNION ALL"
            " SELECT outcome, extractor, result, revision, reply_ts, changes_through FROM dry_run_revisions"
            " WHERE post_id = ?1"
            " ORDER BY 4",
            (post_id,),
        ).fetchall()
        return [
            DryRunRecord(
                DryRunOutcome(outcome), extractor, None if result is None else json.loads(result), rev, ts, changes
            )
            for outcome, extractor, result, rev, ts, changes in rows
        ]

    def audit(self, key: PostKey) -> list[tuple[str, str, str, str | None, str | None]]:
        """The audit log rows of a post, its plans and their requests, oldest first: when, by whom, what, and the
        state change when there is one."""
        post_id = self._post_id(key)
        if post_id is None:
            raise KeyError("no such post")
        rows = self._db.execute(
            "SELECT at, actor, event, from_state, to_state FROM audit_log WHERE post_id = ?1"
            " OR plan_id IN (SELECT id FROM plans WHERE post_id = ?1)"
            " OR request_id IN (SELECT r.id FROM requests r JOIN plans p ON p.id = r.plan_id WHERE p.post_id = ?1)"
            " ORDER BY id",
            (post_id,),
        ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    def create_plan(self, post: PostKey, input_hash: str, operations: Sequence[Operation]) -> int:
        """Store a new plan revision with one planned request per operation, and return its revision.

        The earlier current revision becomes stale, and its planned and approved requests are
        cancelled. Requests that already started a write keep their state. The worker makes plans with
        `finish_dry_run` instead, together with the reply they belong to.
        """
        if not operations:
            raise ValueError("a plan revision holds at least one request")
        with self._transaction():
            post_id = self._post_id(post)
            if post_id is None:
                raise KeyError(f"no post {post}")
            self._replace_plan(post_id, None)
            return self._insert_plan(post_id, NewPlan(input_hash, operations))

    def stale_plan(self, owner: str, key: PostKey) -> None:
        """Leave the post with no current plan: a reply that is about to be posted may replace the one that stands.
        `finish_dry_run` makes its plan, if any, once its outcome is recorded."""
        with self._transaction():
            self._check_lock(owner)
            post_id = self._post_id(key)
            if post_id is None:
                raise KeyError("no such post")
            self._replace_plan(post_id, None)

    def _replace_plan(self, post_id: int, plan: NewPlan | None) -> None:
        """Make the post's current plan revision stale, cancelling its requests that have not started a write, and
        store `plan`, if any, as the new current revision. Call inside a transaction."""
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
        if plan is not None:
            self._insert_plan(post_id, plan)

    def _insert_plan(self, post_id: int, plan: NewPlan) -> int:
        """Store `plan` as the post's next revision, current, with one planned request per operation."""
        (last,) = self._db.execute(
            "SELECT coalesce(max(revision), 0) FROM plans WHERE post_id = ?", (post_id,)
        ).fetchone()
        revision = int(last) + 1
        cursor = self._db.execute(
            "INSERT INTO plans (post_id, revision, input_hash, state, created_at) VALUES (?, ?, ?, 'current', ?)",
            (post_id, revision, plan.input_hash, self._now()),
        )
        plan_id = int(cursor.lastrowid or 0)
        self._audit(Actor.PLANNER, "plan_created", post_id=post_id, plan_id=plan_id)
        for sequence, operation in enumerate(plan.operations, start=1):
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
        SELECT r.id, r.operation, r.state, r.marker_id, p.state, p.id, p.post_id, r.receipt_state
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
        request_id, operation, state, marker_id, plan_state, plan_id, post_id, receipt_state = row
        reported = None if receipt_state is None else S(receipt_state)
        return (
            RequestRow(key, request_id, Operation(operation), S(state), marker_id, plan_state, reported),
            plan_id,
            post_id,
        )

    def request(self, key: RequestKey) -> RequestRow:
        return self._request(key)[0]

    def request_with_marker(self, marker_id: str) -> RequestKey | None:
        """The request whose marker id is `marker_id` (the value of an Undo button), or None when there is none."""
        row = self._db.execute(
            "SELECT o.workspace_id, o.channel_id, o.ts, p.revision, r.sequence FROM requests r"
            " JOIN plans p ON p.id = r.plan_id JOIN posts o ON o.id = p.post_id WHERE r.marker_id = ?",
            (marker_id,),
        ).fetchone()
        if row is None:
            return None
        workspace_id, channel_id, ts, revision, sequence = row
        return RequestKey(PostKey(workspace_id, channel_id, ts), int(revision), int(sequence))

    def _set_state(self, request_id: int, expected: RequestState, target: RequestState, actor: Actor) -> None:
        """The compare-and-set. Call inside a transaction, after every check. An approval or an undo starts a new try
        (`NEW_TRY`), so it forgets the state a receipt reported: the try's outcome is reported even when it is that
        state again (an undo whose read-back disagrees ends `verify_failed`, as its apply may have)."""
        check_transition(expected, target, actor)
        cursor = self._db.execute(
            "UPDATE requests SET state = ?, updated_at = ?,"
            " receipt_state = CASE WHEN ? THEN NULL ELSE receipt_state END"
            " WHERE id = ? AND state = ?",
            (target.value, self._now(), target in NEW_TRY, request_id, expected.value),
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

    def transition(
        self,
        key: RequestKey,
        *,
        expected: RequestState,
        target: RequestState,
        actor: Actor,
        marker_found: bool | None = None,
    ) -> None:
        """Move a request from `expected` to `target`, or raise and change nothing.

        Raises IllegalTransition, StateConflict when the request is not in `expected`, and
        WriteBlocked when an approval is not allowed for the post now. Starting a write goes
        through `begin_attempt`, never through this method.

        `marker_found`: what recovery concluded about the unknown attempt's marker, required when
        the request leaves `unknown` or `undo_unknown` and recorded on that attempt (the marker
        check reads it). It must agree with the target: found for `applied` and `undone`, not
        found for `failed` and `undo_failed`.
        """
        if target in (S.APPLYING, S.UNDOING):
            raise ValueError("a write starts with begin_attempt, which records its intent")
        check_transition(expected, target, actor)
        _check_recovered(expected, target, marker_found)
        with self._transaction():
            row, plan_id, post_id = self._request(key)
            if row.state != expected:
                raise StateConflict(f"request {key} is {row.state}, not {expected}")
            if target is S.APPROVED:
                self._check_approvable(row, post_id)
            if target in (S.APPROVED, S.PLANNED):  # a click stored with the plan approves only with `approve_plan`
                self._db.execute("UPDATE plans SET approved_by = NULL WHERE id = ?", (plan_id,))
            if row.state in (S.APPLYING, S.UNDOING):
                self._finish_attempt(row.id, target)
            if marker_found is not None:
                self._recovered(row.id, marker_found)
            self._set_state(row.id, expected, target, actor)

    def _check_approvable(self, row: RequestRow, post_id: int) -> None:
        """WriteBlocked unless the request may be approved now: its plan revision is current, every request of an
        earlier revision left no stock change behind, and no write of the post may still land."""
        if row.plan_state != "current":
            raise WriteBlocked(f"plan revision {row.key.revision} is {row.plan_state}")
        earlier = [
            state for revision, _, state in self._post_states(post_id, exclude=row.id) if revision < row.key.revision
        ]
        if not may_approve_new_revision(earlier):
            raise WriteBlocked("an earlier revision of this post left a stock change behind")
        self._check_no_unknown_write(row.id, post_id, AttemptKind.APPLY)

    def approve_plan(
        self, owner: str, post: PostKey, revision: int, *, actor: Actor, approver: str | None = None
    ) -> None:
        """Approve every request of plan `revision` of the post at once, or raise and change nothing: StateConflict
        unless all of them are `planned`, and WriteBlocked as `transition` raises it for any one of them. A plan is
        approved whole, never one request of it. `owner` must hold the instance lock.

        `approver`: the Slack user id of whoever clicked Apply, required for a person's approval and refused for the
        policy's. It is stored with the plan (`PlanRow.approved_by`); the gate is judged with it before each write."""
        check_transition(S.PLANNED, S.APPROVED, actor)
        if (actor is Actor.HUMAN) != (approver is not None):
            raise ValueError("a person's approval names the approver, and only a person's does")
        with self._transaction():
            self._check_lock(owner)
            plan = self.plan(post, revision)
            if not plan.requests or any(row.state is not S.PLANNED for row in plan.requests):
                raise StateConflict(f"plan revision {revision} has requests that are not planned")
            for row in plan.requests:
                _, _, post_id = self._request(row.key)
                self._check_approvable(row, post_id)
                self._set_state(row.id, S.PLANNED, S.APPROVED, actor)
            self._db.execute("UPDATE plans SET approved_by = ? WHERE id = ?", (approver, self._request(row.key)[1]))

    def plan(self, post: PostKey, revision: int) -> PlanRow:
        """Plan revision `revision` of the post; KeyError when there is none."""
        row = self._db.execute(
            "SELECT p.id, p.input_hash, p.state, p.approved_by, p.receipt_ts FROM plans p"
            " JOIN posts o ON o.id = p.post_id"
            " WHERE o.workspace_id = ? AND o.channel_id = ? AND o.ts = ? AND p.revision = ?",
            (post.workspace_id, post.channel_id, post.ts, revision),
        ).fetchone()
        if row is None:
            raise KeyError(f"no plan revision {revision} of post {post}")
        plan_id, input_hash, state, approved_by, receipt_ts = row
        (count,) = self._db.execute("SELECT count(*) FROM requests WHERE plan_id = ?", (plan_id,)).fetchone()
        requests = tuple(self.request(RequestKey(post, revision, sequence)) for sequence in range(1, count + 1))
        return PlanRow(post, revision, str(input_hash), state == "current", requests, approved_by, receipt_ts)

    def current_plan(self, post: PostKey) -> PlanRow | None:
        """The post's current plan revision, or None when it has none."""
        row = self._db.execute(
            "SELECT p.revision FROM plans p JOIN posts o ON o.id = p.post_id"
            " WHERE o.workspace_id = ? AND o.channel_id = ? AND o.ts = ? AND p.state = 'current'",
            (post.workspace_id, post.channel_id, post.ts),
        ).fetchone()
        return None if row is None else self.plan(post, int(row[0]))

    def requests_to_work(self) -> list[RequestKey]:
        """Every request a step of the apply protocol is due for: `approved` (the write), `applied` (the read-back a
        read error left undone), `unknown` and `undo_unknown` (recovery, once settled), by post, revision and
        sequence."""
        rows = self._db.execute(
            "SELECT o.workspace_id, o.channel_id, o.ts, p.revision, r.sequence FROM requests r"
            " JOIN plans p ON p.id = r.plan_id JOIN posts o ON o.id = p.post_id"
            " WHERE r.state IN ('approved', 'applied', 'unknown', 'undo_unknown') ORDER BY o.id, p.revision, r.sequence"
        ).fetchall()
        return [RequestKey(PostKey(w, c, ts), int(revision), int(sequence)) for w, c, ts, revision, sequence in rows]

    def receipts_due(self) -> list[tuple[PostKey, int]]:
        """Every plan revision, as (post, revision), that is due a receipt (design section 7, step 6): no request of
        it is `WORKING`, and one of them is in a `RECEIPT_STATES` state that no receipt of the plan reported (none
        did, or the request was approved again since: `_set_state` forgets the reported state then). The receipt
        then reports every request (ADR 0002). By post and revision."""
        return self._receipts_due()

    def _receipts_due(self, plan_id: int | None = None) -> list[tuple[PostKey, int]]:
        """`receipts_due`, of every plan or of plan `plan_id` only."""
        working = sorted(state.value for state in WORKING)
        concluded = sorted(state.value for state in RECEIPT_STATES)
        working_marks, concluded_marks = (", ".join("?" for _ in states) for states in (working, concluded))
        rows = self._db.execute(
            "SELECT o.workspace_id, o.channel_id, o.ts, p.revision FROM plans p JOIN posts o ON o.id = p.post_id"  # noqa: S608
            " WHERE (? IS NULL OR p.id = ?)"
            f" AND EXISTS (SELECT 1 FROM requests r WHERE r.plan_id = p.id AND r.state IN ({concluded_marks})"
            " AND r.receipt_state IS NOT r.state)"
            f" AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.plan_id = p.id AND r.state IN ({working_marks}))"
            " ORDER BY o.id, p.revision",
            [plan_id, plan_id, *concluded, *working],
        ).fetchall()
        return [(PostKey(w, c, ts), int(revision)) for w, c, ts, revision in rows]

    def receipt_posted(
        self, owner: str, post: PostKey, revision: int, ts: str, reported: Sequence[RequestState]
    ) -> None:
        """Record that the receipt of plan revision `revision` of the post was posted as the thread reply `ts`,
        reporting the states `reported`, one per request in order. StateConflict, and nothing changes, unless the plan
        is still due (`receipts_due`) and its requests are still in exactly those states: a person acted in between,
        and the next receipt reports what they did. KeyError when there is no such plan. `owner` must hold the
        instance lock."""
        if not ts:
            raise ValueError("a receipt is a posted message: its ts is not empty")
        with self._transaction():
            self._check_lock(owner)
            plan = self.plan(post, revision)
            if [row.state for row in plan.requests] != list(reported):
                raise StateConflict(f"plan revision {revision} is no longer in the states its receipt reports")
            plan_id, post_id = self._request(plan.requests[0].key)[1:]
            if not self._receipts_due(plan_id):
                raise StateConflict(f"plan revision {revision} is not due a receipt")
            for row in plan.requests:
                if row.state in RECEIPT_STATES:
                    self._db.execute("UPDATE requests SET receipt_state = ? WHERE id = ?", (row.state.value, row.id))
            self._db.execute("UPDATE plans SET receipt_ts = ? WHERE id = ?", (ts, plan_id))
            self._audit(Actor.APPLIER, "receipt_posted", post_id=post_id, plan_id=plan_id)

    def apply_waits_for(self, key: RequestKey) -> str | None:
        """Why the apply of request `key` must wait for another request of its post, or None. It waits while another
        write of the post is in flight or not read back yet, and while an earlier request of its plan is `approved`
        (that one is written first). Each ends without a person, so the request keeps its approval; every other
        refusal of `begin_attempt` is for good."""
        _, _, post_id = self._request(key)
        for revision, sequence, state in self._post_states(post_id, exclude=self.request(key).id):
            if state in NOT_READ_BACK:
                return f"request {sequence} of plan revision {revision} is {state}"
            if revision == key.revision and sequence < key.sequence and state is S.APPROVED:
                return f"request {sequence} of this plan is approved and is written first"
        return None

    def plan_stopped_before(self, key: RequestKey) -> str | None:
        """Why the plan of request `key` stopped before it, or None: an earlier request of the plan is neither
        `verified` nor `rejected` and no step of it is due or in flight (`apply_waits_for` waits for those). The plan
        is applied in order and stops at the first request that went wrong, so this one goes back to review; asked
        before the gate, because a plan whose clicker was cleared would wait at the gate forever."""
        _, _, post_id = self._request(key)
        for revision, sequence, state in sorted(self._post_states(post_id, exclude=self.request(key).id)):
            if revision != key.revision or sequence >= key.sequence:
                continue
            if state not in (S.VERIFIED, S.REJECTED, S.APPROVED) and state not in NOT_READ_BACK:
                return f"request {sequence} of this plan is {state}"
        return None

    def _check_no_unknown_write(self, request_id: int, post_id: int, kind: AttemptKind) -> None:
        """WriteBlocked unless every earlier attempt of `kind` of the request was refused by InvenTree, and, for an
        apply, no write of the post has an unknown outcome whose marker recovery did not find (ADR 0015): a write
        whose outcome is unknown may still land, and another write would add to it. Checked at approval and again
        when the write starts."""
        name = "apply" if kind is AttemptKind.APPLY else "undo"
        if self._db.execute(
            "SELECT 1 FROM request_attempts WHERE request_id = ? AND kind = ? AND outcome IS NOT 'refused'",
            (request_id, kind.value),
        ).fetchone():
            raise WriteBlocked(
                f"an earlier {name} of this request was not refused by InvenTree: it wrote, or it may still land,"
                " so another would write twice"
            )
        if (
            kind is AttemptKind.APPLY
            and self._db.execute(
                "SELECT 1 FROM request_attempts a JOIN requests r ON r.id = a.request_id"
                " JOIN plans p ON p.id = r.plan_id WHERE p.post_id = ? AND a.outcome = 'unknown'"
                " AND a.marker_found IS NOT 1",
                (post_id,),
            ).fetchone()
        ):
            raise WriteBlocked("a write of this post has an outcome recovery could not establish: it may still land")

    def _recovered(self, request_id: int, marker_found: bool) -> None:
        cursor = self._db.execute(
            "UPDATE request_attempts SET marker_found = ?1 WHERE id = (SELECT max(id) FROM request_attempts"
            " WHERE request_id = ?2) AND outcome = 'unknown' AND marker_found IS NULL",
            (int(marker_found), request_id),
        )
        if cursor.rowcount != 1:
            raise AssertionError(f"request {request_id} has no last attempt with an unknown outcome to recover")

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

        Returns the marker of the attempt, which `request_body` must already lead its notes with
        (`next_marker` says which; ValueError otherwise). The caller sends the write only after this returns.
        Raises LockLost unless `lock_owner` holds the instance lock, StateConflict if someone else
        moved the request first, EventsWaiting for an apply while any event waits in the queue (`events_pending`:
        an edit received during the reads before the write cancels it, checked in the same transaction), and
        WriteBlocked if the post or plan forbids a write now:

        - another write of the post is in flight or not yet read back (`applied`);
        - for an apply: the plan is not current, a read-back of the post found something unexpected,
          an earlier revision left a stock change behind, or an earlier request of the plan is not
          verified (requests run in order and stop at the first failure; rejected ones are skipped);
        - another attempt of the same kind of this request was not refused by InvenTree: it wrote, or its outcome
          is unknown, and a write whose outcome is unknown may still land however long recovery waited (design
          section 7, "Unknown outcomes and recovery"). Only an attempt InvenTree refused may be tried again;
        - for an apply: a write of any request of the post has an unknown outcome whose marker recovery did not
          find, for the same reason: a new revision or the next request would add to it.
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
                if self.events_pending():
                    raise EventsWaiting("an event waits in the queue; it may take the approval back")
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
            self._check_no_unknown_write(row.id, post_id, kind)
            marker = Marker(row.marker_id, kind, int(count) + 1)
            if marked(request_body.get("notes"), row.marker_id) != marker:
                raise ValueError(f"the request's notes must lead with this attempt's marker, {marker}")
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

    def next_marker(self, key: RequestKey, kind: AttemptKind) -> Marker:
        """The marker the next attempt of `kind` will carry, to build its request body with. `begin_attempt` checks
        the body against the marker it assigns, so a request moved on in between fails there, not in InvenTree."""
        row = self.request(key)
        (count,) = self._db.execute(
            "SELECT count(*) FROM request_attempts WHERE request_id = ? AND kind = ?", (row.id, kind.value)
        ).fetchone()
        if count >= MAX_ATTEMPT:
            raise WriteBlocked(f"{MAX_ATTEMPT} attempts is the limit")
        return Marker(row.marker_id, kind, int(count) + 1)

    def markers(self, key: RequestKey) -> list[Marker]:
        """Every marker written or about to be written for the request, apply and undo, oldest first."""
        row = self.request(key)
        rows = self._db.execute(
            "SELECT marker FROM request_attempts WHERE request_id = ? ORDER BY id", (row.id,)
        ).fetchall()
        return [Marker.parse(marker) for (marker,) in rows]

    def attempt_lines(self, marker: Marker) -> list[tuple[int, Decimal]]:
        """The stock item pk and quantity of each line of the request body stored for the attempt with `marker`, in
        its order. KeyError if no attempt carries it."""
        _, body = self._attempt_intent(marker)
        lines = body.get("items") if isinstance(body, dict) else None
        if not isinstance(lines, list) or not all(isinstance(line, dict) for line in lines):
            raise ValueError(f"the stored body of {marker} has no list of items")
        pks = [line.get("pk") for line in lines]
        if not all(type(pk) is int for pk in pks):
            raise ValueError(f"the stored body of {marker} has an item without a pk")
        quantities = [line.get("quantity") for line in lines]
        if not all(isinstance(quantity, str) for quantity in quantities):
            raise ValueError(f"the stored body of {marker} has an item without a quantity")
        return [(int(pk), Decimal(str(quantity))) for pk, quantity in zip(pks, quantities, strict=True)]

    def unknown_attempt(self, key: RequestKey) -> UnknownAttempt:
        """The request's last attempt, which recovery has to conclude: its outcome is unknown and recovery has not
        recorded whether it found the marker. KeyError otherwise."""
        row = self.request(key)
        found = self._db.execute(
            "SELECT marker, finished_at, snapshot, request_body FROM request_attempts"
            " WHERE id = (SELECT max(id) FROM request_attempts WHERE request_id = ?)"
            " AND outcome = 'unknown' AND marker_found IS NULL",
            (row.id,),
        ).fetchone()
        if found is None:
            raise KeyError(f"request {key} has no last attempt with an unknown outcome to recover")
        marker, finished_at, snapshot, body = found
        if finished_at is None:
            raise AssertionError(f"request {key} has an unknown attempt with no time its outcome became unknown")
        age = self._clock_now() - datetime.fromisoformat(finished_at)
        return UnknownAttempt(Marker.parse(marker), age, json.loads(snapshot), json.loads(body))

    def conclude_crashed_writes(self, lock_owner: str) -> list[RequestKey]:
        """Move every request a crashed run left `applying` or `undoing` to `unknown` or `undo_unknown`, as recovery,
        and return their keys. Call it once at start, after taking the instance lock and before this process sends
        any write: a write of this process in flight would become unknown too.

        Its attempt's outcome becomes unknown now, so recovery's settle time starts now: the crashed run's write may
        have reached the server and still be running there. A run that lost the lock while it was sending (a takeover
        of a stale heartbeat) can no longer record its outcome (StateConflict); recovery concludes it. Such a run, if
        stalled rather than dead, may send after the settle time: recovery then finds no marker (`failed`), and the
        late write is the ADR 0015 case (the post stays blocked; the marker check reports it). Raises LockLost unless
        `lock_owner` holds the instance lock."""
        targets = {S.APPLYING: S.UNKNOWN, S.UNDOING: S.UNDO_UNKNOWN}
        with self._transaction():
            self._check_lock(lock_owner)
            rows = self._db.execute(
                "SELECT r.id, r.state, o.workspace_id, o.channel_id, o.ts, p.revision, r.sequence"
                " FROM requests r JOIN plans p ON p.id = r.plan_id JOIN posts o ON o.id = p.post_id"
                " WHERE r.state IN (?, ?) ORDER BY r.id",
                (S.APPLYING.value, S.UNDOING.value),
            ).fetchall()
            keys: list[RequestKey] = []
            for request_id, state, workspace, channel, ts, revision, sequence in rows:
                target = targets[S(state)]
                self._finish_attempt(request_id, target)
                self._set_state(request_id, S(state), target, Actor.RECOVERY)
                keys.append(RequestKey(PostKey(workspace, channel, ts), revision, sequence))
            return keys

    def applied_attempt(self, key: RequestKey) -> AppliedAttempt:
        """The last attempt of the `applied` request `key`, the one to read back: InvenTree accepted it, or recovery
        found its marker. KeyError if the request is not `applied`."""
        row = self.request(key)
        if row.state is not S.APPLIED:
            raise KeyError(f"request {key} is {row.state}, not applied")
        found = self._db.execute(
            "SELECT marker, snapshot, request_body FROM request_attempts"
            " WHERE id = (SELECT max(id) FROM request_attempts WHERE request_id = ?)"
            " AND kind = ? AND (outcome = 'accepted' OR (outcome = 'unknown' AND marker_found = 1))",
            (row.id, AttemptKind.APPLY.value),
        ).fetchone()
        if found is None:
            raise AssertionError(f"request {key} is applied, but its last attempt is not an apply accepted or found")
        marker, snapshot, body = found
        return AppliedAttempt(Marker.parse(marker), json.loads(snapshot), json.loads(body))

    def written_apply(self, key: RequestKey) -> AppliedAttempt:
        """The apply attempt of request `key` that wrote, the one an undo reverses: InvenTree accepted it, or recovery
        found its marker. KeyError if none did; AssertionError if two did (`begin_attempt` allows one)."""
        row = self.request(key)
        found = self._db.execute(
            "SELECT marker, snapshot, request_body FROM request_attempts WHERE request_id = ? AND kind = ?"
            " AND (outcome = 'accepted' OR (outcome = 'unknown' AND marker_found = 1))",
            (row.id, AttemptKind.APPLY.value),
        ).fetchall()
        if not found:
            raise KeyError(f"no apply of request {key} wrote")
        if len(found) > 1:
            raise AssertionError(f"{len(found)} applies of request {key} wrote")
        ((marker, snapshot, body),) = found
        return AppliedAttempt(Marker.parse(marker), json.loads(snapshot), json.loads(body))

    def undo_refusal(self, key: RequestKey, *, max_age: timedelta) -> str | None:
        """Why no undo of request `key` may be asked for now, or None: what an Undo button checks, as offered and as
        clicked, before `apply.undo_apply` (design section 7, "Undo"). An undo starts only from a `verified`,
        `verify_failed` or `undo_failed` request whose apply wrote less than `max_age` ago, by the store clock, from
        when its intent was stored (the write ran after that, and a crash or a recovery cannot stretch the time), and
        only when every earlier undo of it was refused by InvenTree: one that wrote, or whose outcome was unknown
        (even one recovery concluded `undo_failed`, ADR 0018), would reverse the apply twice. It reads the store only.
        Whatever passes here is checked again by `undo_apply` under the writer lock, with the stock, and a block that
        passes (another write of the post in flight) is its to report."""
        row, _, _ = self._request(key)
        if row.state not in UNDOABLE:
            return f"the request is {row.state}: only a movement that was written and read back can be undone"
        written = self._db.execute(
            "SELECT started_at FROM request_attempts WHERE request_id = ? AND kind = ?"
            " AND (outcome = 'accepted' OR (outcome = 'unknown' AND marker_found = 1))",
            (row.id, AttemptKind.APPLY.value),
        ).fetchall()
        if not written:
            return "no apply of this request wrote"
        if len(written) > 1:
            raise AssertionError(f"{len(written)} applies of request {key} wrote")
        undos = [
            outcome
            for (outcome,) in self._db.execute(
                "SELECT outcome FROM request_attempts WHERE request_id = ? AND kind = ?",
                (row.id, AttemptKind.UNDO.value),
            ).fetchall()
        ]
        if any(outcome != "refused" for outcome in undos):
            return "an earlier undo was not refused by InvenTree: it wrote, or it may still land"
        if len(undos) >= MAX_ATTEMPT:
            return f"{MAX_ATTEMPT} attempts is the limit"
        ((started_at,),) = written
        if self._clock_now() - datetime.fromisoformat(started_at) >= max_age:
            return (
                f"the movement was written more than {max_age.total_seconds() / 3600:g} hours ago: undo it in InvenTree"
            )
        return None

    def attempt_snapshot(self, marker: Marker) -> Any:
        """The snapshot stored for the attempt with `marker` (step 3 of the apply protocol). KeyError if no attempt
        carries it."""
        snapshot, _ = self._attempt_intent(marker)
        return snapshot

    def _attempt_intent(self, marker: Marker) -> tuple[Any, Any]:
        row = self._db.execute(
            "SELECT snapshot, request_body FROM request_attempts WHERE marker = ?", (str(marker),)
        ).fetchone()
        if row is None:
            raise KeyError(f"no attempt carries {marker}")
        return json.loads(row[0]), json.loads(row[1])

    def recorded_requests(self, post: PostKey) -> list[RecordedRequest]:
        """Every request of the post, of every revision, with its attempts oldest first: what the marker check of
        the apply protocol compares the stock history with (`core.marker_check`)."""
        rows = self._db.execute(  # one statement, so one consistent read of requests and attempts
            "SELECT p.revision, r.sequence, r.operation, r.state, r.marker_id, a.marker, a.outcome, a.marker_found"
            " FROM requests r JOIN plans p ON p.id = r.plan_id JOIN posts o ON o.id = p.post_id"
            " LEFT JOIN request_attempts a ON a.request_id = r.id"
            " WHERE o.workspace_id = ? AND o.channel_id = ? AND o.ts = ? ORDER BY p.revision, r.sequence, a.id",
            (post.workspace_id, post.channel_id, post.ts),
        ).fetchall()
        requests: dict[tuple[int, int], tuple[str, str, str, list[RecordedAttempt]]] = {}
        for revision, sequence, operation, state, marker_id, marker, outcome, found in rows:
            _, _, _, attempts = requests.setdefault((revision, sequence), (operation, state, marker_id, []))
            if marker is not None:
                attempts.append(
                    RecordedAttempt(
                        Marker.parse(marker),
                        None if outcome is None else Outcome(outcome),
                        None if found is None else bool(found),
                    )
                )
        return [
            RecordedRequest(RequestKey(post, revision, sequence), Operation(op), S(state), marker_id, tuple(attempts))
            for (revision, sequence), (op, state, marker_id, attempts) in requests.items()
        ]

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
                " AND (not_before IS NULL OR not_before <= ? OR not_before > ?) ORDER BY id LIMIT ?",
                (self._now(), self._stamp(self._clock_now() + MAX_EVENT_RETRY_DELAY), limit),
            ).fetchall()
            for row in rows:
                self._db.execute(
                    "UPDATE slack_events SET status = 'processing', attempts = attempts + 1, claimed_by = ?,"
                    " not_before = NULL WHERE id = ?",
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

    def events_pending(self) -> bool:
        """Whether any event is unfinished: waiting in the queue, including one that may not be claimed yet, or left
        processing."""
        row = self._db.execute("SELECT 1 FROM slack_events WHERE status IN ('pending', 'processing') LIMIT 1")
        return row.fetchone() is not None

    def retry_event_later(self, owner: str, event_id: int, after: timedelta) -> None:
        """Put an event this instance is processing back in the queue after a transient failure, not to be claimed
        again before `after` has passed (`Worker.drain`). Its attempts are kept, so the retries stay bounded."""
        if not timedelta(0) <= after <= MAX_EVENT_RETRY_DELAY:
            raise ValueError("an event is put off for no more than MAX_EVENT_RETRY_DELAY")
        with self._transaction():
            self._check_lock(owner)
            cursor = self._db.execute(
                "UPDATE slack_events SET status = 'pending', claimed_by = NULL, not_before = ? WHERE id = ?"
                " AND status = 'processing' AND claimed_by = ?",
                (self._stamp(self._clock_now() + after), event_id, owner),
            )
            if cursor.rowcount != 1:
                raise StateConflict("the event is not being processed by this instance")
            self._audit("ingest", "event_retry_later", slack_event_id=event_id)

    def requeue_unfinished(self, owner: str, *, max_attempts: int = MAX_EVENT_ATTEMPTS, own: bool = False) -> int:
        """At startup, put events that an earlier instance left processing back in the queue.

        Events claimed by `owner` itself are left alone: they are still being processed. With `own`, only those are
        put back instead: the worker does so before each drain, when none of its events is in flight, for the
        events a drain cut off by an error left claimed. An event claimed `max_attempts` times fails, so one bad
        payload cannot crash the connector in a loop. Returns how many events went back to pending.
        """
        with self._transaction():
            self._check_lock(owner)
            rows = self._db.execute(
                "SELECT id, attempts FROM slack_events WHERE status = 'processing' AND (claimed_by IS ?) = ?",
                (owner, own),
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

    # Aliases

    def learn_alias(
        self,
        owner: str,
        kind: AliasKind,
        text: str,
        record: int,
        *,
        rivals: Collection[int],
        learned_from: RequestKey,
        user: str | None = None,
    ) -> None:
        """Store `text` as a confirmed alias of part or location `record` (ADR 0012), confirmed in request
        `learned_from`, which must be `verified`: only a confirmation that moved stock teaches an alias. `rivals`: the
        records the whole-word and fuzzy rules fit it to now (`matching.part_rivals`, `location_rivals`). Every alias
        of the kind and user the matcher takes for the same text ("patch cable" for "patch cables") is replaced by
        this one. StateConflict, and nothing changes, when the request is not verified. `owner` must hold the instance
        lock."""
        if not words(text) or record <= 0:
            raise ValueError("an alias names one record with at least one word")
        key = " ".join(words(text))
        with self._transaction():
            self._check_lock(owner)
            request, _, post_id = self._request(learned_from)
            if request.state is not S.VERIFIED:
                raise StateConflict(f"request {learned_from} is {request.state}: only a verified one teaches an alias")
            stored = json.dumps(sorted(set(rivals)))
            now = self._now()
            # One alias per text as the matcher reads it: "patch cables" replaces "patch cable" (plurals folded).
            rows = self._db.execute(
                "SELECT id, text FROM aliases WHERE kind = ? AND ifnull(user_id, '') = ? ORDER BY id",
                (kind.value, user or ""),
            ).fetchall()
            same = [alias_id for alias_id, known in rows if same_words(known, text)]
            found = same[0] if same else None
            for other in same[1:]:
                self._db.execute("DELETE FROM aliases WHERE id = ?", (other,))
                self._audit(Actor.APPLIER, "alias_replaced", post_id=post_id, request_id=request.id, alias_id=other)
            if found is None:
                alias_id = self._db.execute(
                    "INSERT INTO aliases (kind, text, words, record, user_id, rivals, request_id, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind.value, text.strip(), key, record, user, stored, request.id, now, now),
                ).lastrowid
            else:
                alias_id = found
                self._db.execute(
                    "UPDATE aliases SET text = ?, words = ?, record = ?, rivals = ?, request_id = ?, updated_at = ?"
                    " WHERE id = ?",
                    (text.strip(), key, record, stored, request.id, now, alias_id),
                )
            self._audit(Actor.APPLIER, "alias_learned", post_id=post_id, request_id=request.id, alias_id=alias_id)

    def aliases(self, kind: AliasKind) -> tuple[Alias, ...]:
        """Every stored alias of `kind`, as the matcher takes them, oldest first."""
        rows = self._db.execute(
            "SELECT text, record, user_id, rivals FROM aliases WHERE kind = ? ORDER BY id", (kind.value,)
        ).fetchall()
        return tuple(
            Alias(text, record, user, None if rivals is None else frozenset(json.loads(rivals)))
            for text, record, user, rivals in rows
        )

    _STORED_ALIAS_SELECT = (
        "SELECT a.id, a.kind, a.text, a.record, a.user_id, a.rivals, a.created_at, a.updated_at,"
        " p.workspace_id, p.channel_id, p.ts, l.revision, r.sequence, a.request_id, l.post_id"
        " FROM aliases a LEFT JOIN requests r ON r.id = a.request_id LEFT JOIN plans l ON l.id = r.plan_id"
        " LEFT JOIN posts p ON p.id = l.post_id"
    )

    @staticmethod
    def _stored_alias(row: Sequence[Any]) -> StoredAlias:
        (
            alias_id,
            kind,
            text,
            record,
            user,
            rivals,
            created_at,
            updated_at,
            workspace,
            channel,
            ts,
            revision,
            sequence,
        ) = row[:13]
        learned_from = None if ts is None else RequestKey(PostKey(workspace, channel, ts), revision, sequence)
        return StoredAlias(
            alias_id,
            AliasKind(kind),
            text,
            record,
            user,
            None if rivals is None else frozenset(json.loads(rivals)),
            learned_from,
            created_at,
            updated_at,
        )

    def stored_aliases(self) -> tuple[StoredAlias, ...]:
        """Every stored alias with where it was learned: parts first, then locations, oldest first."""
        rows = self._db.execute(f"{self._STORED_ALIAS_SELECT} ORDER BY a.kind = 'location', a.id").fetchall()
        return tuple(self._stored_alias(row) for row in rows)

    def remove_alias(self, alias_id: int) -> StoredAlias:
        """Delete alias `alias_id` and return what it was. The matcher stops using it at its next catalog read, so a
        post read after that is matched as if it had never been learned. Audited as the admin's, with the request that
        taught it. KeyError, and nothing changes, when there is no such alias.

        Needs no instance lock: an admin removes an alias while the connector runs."""
        with self._transaction():
            row = self._db.execute(
                f"{self._STORED_ALIAS_SELECT} WHERE a.id = ?",
                (alias_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no alias {alias_id}")
            removed, (request_id, post_id) = self._stored_alias(row), row[13:]
            self._db.execute("DELETE FROM aliases WHERE id = ?", (alias_id,))
            self._audit(Actor.ADMIN, "alias_removed", post_id=post_id, request_id=request_id, alias_id=alias_id)
            return removed

    def release_lock(self, owner: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM instance_lock WHERE id = 1 AND owner = ?", (owner,))
