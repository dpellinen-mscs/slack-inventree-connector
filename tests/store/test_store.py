"""The audit store: keys, append-only rows, compare-and-set transitions and write guards (design sections 7, 8)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, IllegalTransition, RequestState
from stockthread.store import InstanceLocked, LockLost, SchemaTooNew, StateConflict, Store, WriteBlocked

S = RequestState
A = Actor
POST = PostKey("T0ACME", "C0STOCK", "1727600000.000100")
HASH = "0" * 64
SNAPSHOT = {"items": [{"pk": 7, "location": 3, "quantity": "10"}]}
BODY = {"items": [{"pk": 7, "quantity": "2"}], "location": 4, "notes": "marker goes here"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(path: Path, clock: Clock) -> Iterator[Store]:
    opened = Store.open(path, clock=clock)
    yield opened
    opened.close()


def key(revision: int = 1, sequence: int = 1) -> RequestKey:
    return RequestKey(POST, revision, sequence)


def plan(store: Store, *operations: Operation) -> int:
    store.create_post(POST)
    return store.create_plan(POST, HASH, operations or (Operation.TRANSFER,))


def approve(store: Store, request: RequestKey) -> None:
    store.transition(request, expected=S.PLANNED, target=S.APPROVED, actor=A.HUMAN)


def begin(store: Store, request: RequestKey, kind: AttemptKind = AttemptKind.APPLY, **kwargs: object) -> Marker:
    options: dict[str, object] = {
        "expected": S.APPROVED,
        "actor": A.APPLIER,
        "snapshot": SNAPSHOT,
        "request_body": BODY,
    }
    options.update(kwargs)
    return store.begin_attempt(request, kind, **options).marker  # type: ignore[arg-type]


def verified(store: Store, request: RequestKey) -> None:
    approve(store, request)
    begin(store, request)
    store.transition(request, expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER)
    store.transition(request, expected=S.APPLIED, target=S.VERIFIED, actor=A.APPLIER)


def raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# Opening and migrating


def test_open_creates_the_schema_and_is_repeatable(path: Path) -> None:
    Store.open(path).close()
    store = Store.open(path)
    assert store.schema_version == 1
    store.close()


def test_the_connection_is_durable(store: Store, path: Path) -> None:
    db = store._db
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_newer_schema_is_refused(path: Path) -> None:
    Store.open(path).close()
    raw(path).execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNew):
        Store.open(path)


# Keys


def test_create_post_is_idempotent(store: Store) -> None:
    assert store.create_post(POST) == store.create_post(POST)


def test_revisions_count_up_and_requests_get_their_own_markers(store: Store) -> None:
    assert plan(store, Operation.TRANSFER, Operation.REMOVE) == 1
    first, second = store.request(key(1, 1)), store.request(key(1, 2))
    assert (first.operation, second.operation) == (Operation.TRANSFER, Operation.REMOVE)
    assert first.state is S.PLANNED
    assert first.marker_id != second.marker_id
    assert store.create_plan(POST, HASH, [Operation.ADD]) == 2


def test_a_plan_needs_a_post_and_a_request(store: Store) -> None:
    with pytest.raises(KeyError):
        store.create_plan(POST, HASH, [Operation.ADD])
    store.create_post(POST)
    with pytest.raises(ValueError, match="at least one"):
        store.create_plan(POST, HASH, [])


def test_the_database_enforces_the_request_key(store: Store, path: Path) -> None:
    plan(store)
    db = raw(path)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "INSERT INTO requests (plan_id, sequence, operation, state, marker_id, updated_at)"
            " VALUES (1, 1, 'add', 'planned', ?, 'now')",
            ("x" * 22,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "INSERT INTO plans (post_id, revision, input_hash, state, created_at) VALUES (1, 1, ?, 'current', 'now')",
            (HASH,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "INSERT INTO posts (workspace_id, channel_id, ts, created_at) VALUES (?, ?, ?, 'now')",
            (POST.workspace_id, POST.channel_id, POST.ts),
        )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE audit_log SET event = 'x'", "append-only"),
        ("DELETE FROM audit_log", "append-only"),
        ("UPDATE posts SET ts = '1.2'", "never changes"),
        ("DELETE FROM posts", "never deleted"),
        ("UPDATE plans SET revision = 5", "never changes"),
        ("UPDATE plans SET input_hash = '" + "1" * 64 + "'", "never changes"),
        ("DELETE FROM plans", "never deleted"),
        ("UPDATE requests SET marker_id = '" + "y" * 22 + "'", "never changes"),
        ("UPDATE requests SET sequence = 9", "never changes"),
        ("UPDATE requests SET operation = 'add'", "never changes"),
        ("DELETE FROM requests", "never deleted"),
        ("UPDATE request_attempts SET request_body = '{}'", "never changes"),
        ("UPDATE request_attempts SET snapshot = '{}'", "never changes"),
        ("UPDATE request_attempts SET marker = 'x'", "never changes"),
        ("DELETE FROM request_attempts", "never deleted"),
    ],
)
def test_triggers_protect_keys_intent_and_the_log(store: Store, path: Path, statement: str, message: str) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    with pytest.raises(sqlite3.IntegrityError, match=message):
        raw(path).execute(statement)


def test_an_attempt_outcome_is_recorded_once(store: Store, path: Path) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.UNKNOWN, actor=A.APPLIER)
    with pytest.raises(sqlite3.IntegrityError, match="once"):
        raw(path).execute("UPDATE request_attempts SET outcome = 'accepted'")


def test_the_log_holds_ids_and_states_in_order(store: Store, path: Path) -> None:
    plan(store)
    verified(store, key())
    rows = raw(path).execute("SELECT actor, event, from_state, to_state FROM audit_log ORDER BY id").fetchall()
    assert rows == [
        ("planner", "post_created", None, None),
        ("planner", "plan_created", None, None),
        ("planner", "request_created", None, "planned"),
        ("human", "request_state", "planned", "approved"),
        ("applier", "request_state", "approved", "applying"),
        ("applier", "attempt_started", None, None),
        ("applier", "request_state", "applying", "applied"),
        ("applier", "request_state", "applied", "verified"),
    ]


# Compare-and-set


def test_a_transition_from_the_wrong_state_changes_nothing(store: Store) -> None:
    plan(store)
    approve(store, key())
    with pytest.raises(StateConflict):
        approve(store, key())  # the second click
    assert store.request(key()).state is S.APPROVED


def test_the_state_machine_is_enforced(store: Store) -> None:
    plan(store)
    with pytest.raises(IllegalTransition):
        store.transition(key(), expected=S.PLANNED, target=S.VERIFIED, actor=A.APPLIER)
    with pytest.raises(IllegalTransition):
        store.transition(key(), expected=S.PLANNED, target=S.APPROVED, actor=A.RECOVERY)
    with pytest.raises(ValueError, match="begin_attempt"):
        store.transition(key(), expected=S.APPROVED, target=S.APPLYING, actor=A.APPLIER)


def test_two_stores_racing_for_one_request_start_one_attempt(store: Store, path: Path, clock: Clock) -> None:
    plan(store)
    approve(store, key())
    barrier = threading.Barrier(8)
    results: list[str] = []

    def run() -> None:
        worker = Store.open(path, clock=clock)  # one connection per thread, as separate processes would have
        try:
            barrier.wait()
            begin(worker, key())
            results.append("started")
        except StateConflict:
            results.append("conflict")
        finally:
            worker.close()

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict"] * 7 + ["started"]
    assert len(store.markers(key())) == 1


# The intent of a write


def test_begin_attempt_stores_the_intent_and_returns_the_marker(store: Store, path: Path) -> None:
    plan(store)
    approve(store, key())
    marker = begin(store, key())
    assert marker == Marker(store.request(key()).marker_id, AttemptKind.APPLY, 1)
    assert store.request(key()).state is S.APPLYING
    stored = raw(path).execute("SELECT marker, snapshot, request_body, outcome FROM request_attempts").fetchone()
    assert stored == (
        str(marker),
        '{"items":[{"location":3,"pk":7,"quantity":"10"}]}',
        '{"items":[{"pk":7,"quantity":"2"}],"location":4,"notes":"marker goes here"}',
        None,
    )


def test_a_failed_intent_leaves_no_attempt(store: Store, path: Path) -> None:
    plan(store)
    approve(store, key())
    with pytest.raises(TypeError):
        begin(store, key(), snapshot={"not json": object()})
    assert store.request(key()).state is S.APPROVED
    assert raw(path).execute("SELECT count(*) FROM request_attempts").fetchone()[0] == 0


def test_attempts_share_the_id_and_count_up_per_kind(store: Store) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
    store.transition(key(), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER)
    store.transition(key(), expected=S.APPLIED, target=S.VERIFIED, actor=A.APPLIER)
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    markers = store.markers(key())
    assert [(m.kind, m.attempt) for m in markers] == [
        (AttemptKind.APPLY, 1),
        (AttemptKind.APPLY, 2),
        (AttemptKind.UNDO, 1),
    ]
    assert len({m.marker_id for m in markers}) == 1


def test_the_outcome_follows_the_state(store: Store, path: Path) -> None:
    plan(store)
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_UNKNOWN, actor=A.APPLIER)
    store.transition(key(), expected=S.UNDO_UNKNOWN, target=S.UNDONE, actor=A.RECOVERY)
    outcomes = raw(path).execute("SELECT kind, outcome FROM request_attempts ORDER BY id").fetchall()
    assert outcomes == [("a", "accepted"), ("u", "unknown")]


# Write guards


def test_only_one_write_of_a_post_in_flight(store: Store) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    approve(store, key(1, 1))
    approve(store, key(1, 2))
    begin(store, key(1, 1))
    with pytest.raises(WriteBlocked, match="in flight"):
        begin(store, key(1, 2))
    assert store.request(key(1, 2)).state is S.APPROVED


def test_verify_failed_blocks_applies_of_the_post_but_not_undo(store: Store) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    approve(store, key(1, 1))
    approve(store, key(1, 2))
    begin(store, key(1, 1))
    store.transition(key(1, 1), expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER)
    store.transition(key(1, 1), expected=S.APPLIED, target=S.VERIFY_FAILED, actor=A.APPLIER)
    with pytest.raises(WriteBlocked, match="unexpected"):
        begin(store, key(1, 2))
    begin(store, key(1, 1), AttemptKind.UNDO, expected=S.VERIFY_FAILED, actor=A.HUMAN)


def test_a_new_revision_makes_the_old_one_stale(store: Store) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    approve(store, key(1, 1))
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    assert store.request(key(1, 1)).plan_state == "stale"
    assert store.request(key(1, 2)).state is S.CANCELLED
    # The approved request of the stale plan stays approved, but can never be applied.
    assert store.request(key(1, 1)).state is S.APPROVED
    with pytest.raises(WriteBlocked, match="stale"):
        begin(store, key(1, 1))


def test_a_new_revision_is_approved_only_after_the_old_one_is_undone(store: Store) -> None:
    plan(store)
    verified(store, key(1, 1))
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    with pytest.raises(WriteBlocked, match="earlier revision"):
        approve(store, key(2, 1))
    begin(store, key(1, 1), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(1, 1), expected=S.UNDOING, target=S.UNDONE, actor=A.APPLIER)
    approve(store, key(2, 1))
    begin(store, key(2, 1))


def test_a_stale_revision_cannot_be_approved(store: Store) -> None:
    plan(store)
    store.transition(key(1, 1), expected=S.PLANNED, target=S.REJECTED, actor=A.HUMAN)
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    store.transition(key(2, 1), expected=S.PLANNED, target=S.REJECTED, actor=A.HUMAN)
    store.create_plan(POST, "2" * 64, [Operation.TRANSFER])
    with pytest.raises(StateConflict):
        approve(store, key(2, 1))  # rejected is final
    approve(store, key(3, 1))


# The single-instance lock


def test_a_second_instance_is_refused_while_the_first_is_alive(store: Store, clock: Clock) -> None:
    store.acquire_lock("first", stale_after=timedelta(seconds=60))
    with pytest.raises(InstanceLocked):
        store.acquire_lock("second", stale_after=timedelta(seconds=60))
    clock.now += timedelta(seconds=30)
    store.heartbeat("first")
    clock.now += timedelta(seconds=45)
    with pytest.raises(InstanceLocked):
        store.acquire_lock("second", stale_after=timedelta(seconds=60))


def test_a_stale_lock_is_taken_over_and_the_old_owner_learns_it(store: Store, clock: Clock) -> None:
    store.acquire_lock("first", stale_after=timedelta(seconds=60))
    clock.now += timedelta(seconds=61)
    store.acquire_lock("second", stale_after=timedelta(seconds=60))
    with pytest.raises(LockLost):
        store.heartbeat("first")
    store.release_lock("first")  # releasing someone else's lock does nothing
    with pytest.raises(InstanceLocked):
        store.acquire_lock("third", stale_after=timedelta(seconds=60))
    store.release_lock("second")
    store.acquire_lock("third", stale_after=timedelta(seconds=60))
