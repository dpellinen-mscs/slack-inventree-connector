# SPDX-License-Identifier: MIT
"""The audit store: keys, append-only rows, compare-and-set transitions and write guards (design sections 7, 8)."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import Outcome, RecordedAttempt, check_markers
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, IllegalTransition, RequestState
from stockthread.core.write_request import write_notes
from stockthread.store import (
    EventStatus,
    EventsWaiting,
    InstanceLocked,
    LockLost,
    SchemaTooNew,
    StateConflict,
    Store,
    WriteBlocked,
)
from stockthread.store import db as store_db

S = RequestState
A = Actor
POST = PostKey("T0ACME", "C0STOCK", "1727600000.000100")
HASH = "0" * 64
SNAPSHOT = {"items": [{"pk": 7, "location": 3, "quantity": "10"}]}
OWNER = "test-instance"
BODY = {"items": [{"pk": 7, "quantity": "2"}], "location": 4}
"""A request body without its notes: `begin` adds notes that lead with the attempt's marker."""


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
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
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
        "lock_owner": OWNER,
    }
    options.update(kwargs)
    if "request_body" not in options:
        options["request_body"] = BODY | {"notes": write_notes(store.next_marker(request, kind), "U0SAM")}
    return store.begin_attempt(request, kind, **options).marker  # type: ignore[arg-type]


def verified(store: Store, request: RequestKey) -> None:
    approve(store, request)
    begin(store, request)
    store.transition(request, expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER)
    store.transition(request, expected=S.APPLIED, target=S.VERIFIED, actor=A.APPLIER)


LATEST = len(store_db._migrations())


def raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# Opening and migrating


def test_open_creates_the_schema_and_is_repeatable(path: Path) -> None:
    Store.open(path).close()
    store = Store.open(path)
    assert store.schema_version == LATEST
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
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE|taken"):
        db.execute(
            "INSERT INTO requests (plan_id, sequence, operation, state, marker_id, updated_at)"
            " VALUES (1, 1, 'add', 'planned', ?, 'now')",
            ("x" * 22,),
        )
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE|taken"):
        db.execute(
            "INSERT INTO plans (post_id, revision, input_hash, state, created_at) VALUES (1, 1, ?, 'current', 'now')",
            (HASH,),
        )
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE|taken"):
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
        f'{{"items":[{{"pk":7,"quantity":"2"}}],"location":4,"notes":"{marker} U0SAM"}}',
        None,
    )


def test_no_apply_begins_while_an_event_waits_in_the_queue(store: Store) -> None:
    """Review #137 finding 1: an edit received during the reads before a write is in the queue when the attempt would
    begin; it may take the approval back, so the attempt is refused in the transaction that would store it."""
    plan(store)
    approve(store, key())
    assert store.enqueue_event("Ev1", "event", "{}")
    with pytest.raises(EventsWaiting):
        begin(store, key())
    assert store.request(key()).state is S.APPROVED
    assert store.markers(key()) == []
    (claimed,) = store.claim_events(OWNER)
    with pytest.raises(EventsWaiting):  # one being processed too
        begin(store, key())
    store.finish_event(OWNER, claimed.id, EventStatus.DONE)
    begin(store, key())
    assert store.request(key()).state is S.APPLYING


def test_an_event_another_connection_stored_holds_the_apply_back(store: Store, path: Path) -> None:
    """The Slack listener stores events through a connection of its own, as here."""
    plan(store)
    approve(store, key())
    listener = Store.open(path)
    try:
        assert listener.enqueue_event("Ev1", "event", "{}")
    finally:
        listener.close()
    with pytest.raises(EventsWaiting):
        begin(store, key())
    assert store.request(key()).state is S.APPROVED


def test_an_undo_begins_while_an_event_waits_in_the_queue(store: Store) -> None:
    """An undo is asked for by a click, an event being processed while it runs; an edit does not take it back."""
    plan(store)
    verified(store, key())
    assert store.enqueue_event("Ev1", "event", "{}")
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    assert store.request(key()).state is S.UNDOING


@pytest.mark.parametrize(
    "notes",
    [
        None,
        "",
        "U0SAM",
        "{other} U0SAM",  # another request's marker
        "{retry} U0SAM",  # the next attempt's
        "{undo} U0SAM",  # the other kind's
        "U0SAM {this}",  # not first
    ],
)
def test_begin_attempt_refuses_a_body_whose_notes_do_not_lead_with_its_marker(
    store: Store, path: Path, notes: str | None
) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    approve(store, key())
    this = store.next_marker(key(), AttemptKind.APPLY)
    markers = {
        "this": this,
        "other": store.next_marker(key(1, 2), AttemptKind.APPLY),
        "retry": Marker(this.marker_id, AttemptKind.APPLY, 2),
        "undo": Marker(this.marker_id, AttemptKind.UNDO, 1),
    }
    body = BODY if notes is None else BODY | {"notes": notes.format(**markers)}
    with pytest.raises(ValueError, match="lead with this attempt's marker"):
        begin(store, key(), request_body=body)
    assert store.request(key()).state is S.APPROVED
    assert raw(path).execute("SELECT count(*) FROM request_attempts").fetchone()[0] == 0
    assert begin(store, key()) == this


def test_next_marker_counts_per_kind_and_stops_at_the_limit(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    plan(store)
    approve(store, key())
    assert store.next_marker(key(), AttemptKind.UNDO).attempt == 1
    begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
    assert store.next_marker(key(), AttemptKind.APPLY).attempt == 2
    assert store.next_marker(key(), AttemptKind.UNDO).attempt == 1
    monkeypatch.setattr(store_db, "MAX_ATTEMPT", 1)
    with pytest.raises(WriteBlocked, match="limit"):
        store.next_marker(key(), AttemptKind.APPLY)


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
    store.transition(key(), expected=S.UNDO_UNKNOWN, target=S.UNDONE, actor=A.RECOVERY, marker_found=True)
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
    # An approval of a stale plan is withdrawn, so it cannot block the new revision.
    assert store.request(key(1, 1)).state is S.CANCELLED
    approve(store, key(2, 1))
    begin(store, key(2, 1))


def test_a_failed_request_of_a_stale_plan_cannot_be_approved_again(store: Store) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    with pytest.raises(WriteBlocked, match="stale"):
        store.transition(key(), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    assert store.request(key()).state is S.FAILED


def test_begin_attempt_checks_earlier_revisions_itself(store: Store, path: Path) -> None:
    plan(store)
    verified(store, key(1, 1))
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    raw(path).execute("UPDATE requests SET state = 'approved' WHERE id = ?", (store.request(key(2, 1)).id,))
    with pytest.raises(WriteBlocked, match="earlier revision"):
        begin(store, key(2, 1))


def test_begin_attempt_refuses_a_stale_plan_itself(store: Store, path: Path) -> None:
    plan(store)
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    raw(path).execute("UPDATE requests SET state = 'approved' WHERE id = ?", (store.request(key(1, 1)).id,))
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
    store.release_lock(OWNER)
    store.acquire_lock("first", stale_after=timedelta(seconds=60))
    with pytest.raises(InstanceLocked):
        store.acquire_lock("second", stale_after=timedelta(seconds=60))
    clock.now += timedelta(seconds=30)
    store.heartbeat("first")
    clock.now += timedelta(seconds=45)
    with pytest.raises(InstanceLocked):
        store.acquire_lock("second", stale_after=timedelta(seconds=60))


def test_a_stale_lock_is_taken_over_and_the_old_owner_learns_it(store: Store, clock: Clock) -> None:
    store.release_lock(OWNER)
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


def test_only_the_lock_holder_starts_a_write(store: Store, clock: Clock) -> None:
    plan(store)
    approve(store, key())
    with pytest.raises(LockLost):
        begin(store, key(), lock_owner="someone-else")
    clock.now += timedelta(seconds=61)  # this instance stalls, and another one takes over
    store.acquire_lock("new-instance", stale_after=timedelta(seconds=60))
    with pytest.raises(LockLost):
        begin(store, key())
    assert store.request(key()).state is S.APPROVED
    begin(store, key(), lock_owner="new-instance")


def test_lock_owners_are_unique() -> None:
    assert len({store_db.new_lock_owner() for _ in range(100)}) == 100


def test_a_naive_clock_is_refused(path: Path) -> None:
    store = Store.open(path, clock=lambda: datetime(2026, 9, 29, 12, 0))
    with pytest.raises(ValueError, match="aware"):
        store.create_post(POST)
    store.close()


# More write guards


@pytest.mark.parametrize("state", [S.APPLYING, S.UNKNOWN, S.APPLIED])
def test_an_unread_apply_blocks_an_undo_of_the_post(store: Store, state: RequestState) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    verified(store, key(1, 1))
    approve(store, key(1, 2))
    begin(store, key(1, 2))
    if state is not S.APPLYING:
        store.transition(key(1, 2), expected=S.APPLYING, target=state, actor=A.APPLIER)
    with pytest.raises(WriteBlocked, match="in flight or not read back"):
        begin(store, key(1, 1), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)


@pytest.mark.parametrize("state", [S.UNDOING, S.UNDO_UNKNOWN])
def test_an_unsettled_undo_blocks_another_undo_of_the_post(store: Store, state: RequestState) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    verified(store, key(1, 1))
    verified(store, key(1, 2))
    begin(store, key(1, 1), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    if state is S.UNDO_UNKNOWN:
        store.transition(key(1, 1), expected=S.UNDOING, target=state, actor=A.APPLIER)
    with pytest.raises(WriteBlocked, match="in flight"):
        begin(store, key(1, 2), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)


def test_an_undo_failed_request_is_retried_through_begin_attempt(store: Store) -> None:
    plan(store)
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_FAILED, actor=A.APPLIER)
    marker = begin(store, key(), AttemptKind.UNDO, expected=S.UNDO_FAILED, actor=A.HUMAN)
    assert (marker.kind, marker.attempt) == (AttemptKind.UNDO, 2)


@pytest.mark.parametrize(
    ("first", "allowed"),
    [(S.APPROVED, False), (S.FAILED, False), (S.VERIFIED, True), (S.REJECTED, True)],
)
def test_requests_of_a_plan_run_in_order_and_stop_at_a_failure(
    store: Store, first: RequestState, allowed: bool
) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    if first is S.REJECTED:
        store.transition(key(1, 1), expected=S.PLANNED, target=S.REJECTED, actor=A.HUMAN)
    elif first is S.VERIFIED:
        verified(store, key(1, 1))
    else:
        approve(store, key(1, 1))
        if first is S.FAILED:
            begin(store, key(1, 1))
            store.transition(key(1, 1), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
    approve(store, key(1, 2))
    if allowed:
        begin(store, key(1, 2))
    else:
        with pytest.raises(WriteBlocked, match="not verified"):
            begin(store, key(1, 2))


def test_two_requests_of_a_post_racing_start_one_write(store: Store, path: Path, clock: Clock) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    verified(store, key(1, 1))
    verified(store, key(1, 2))
    barrier = threading.Barrier(8)
    results: list[str] = []

    def run(sequence: int) -> None:
        worker = Store.open(path, clock=clock)
        try:
            barrier.wait()
            begin(worker, key(1, sequence), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
            results.append("started")
        except (StateConflict, WriteBlocked):
            results.append("refused")
        finally:
            worker.close()

    threads = [threading.Thread(target=run, args=(1 + n % 2,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["refused"] * 7 + ["started"]


def test_the_attempt_limit(store: Store) -> None:
    plan(store)
    approve(store, key())
    for _ in range(99):
        begin(store, key())
        store.transition(key(), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
        store.transition(key(), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    with pytest.raises(WriteBlocked, match="limit"):
        store.next_marker(key(), AttemptKind.APPLY)
    # A body built before the limit was reached: begin_attempt holds the limit itself.
    last = Marker(store.request(key()).marker_id, AttemptKind.APPLY, 99)
    with pytest.raises(WriteBlocked, match="limit"):
        begin(store, key(), request_body=BODY | {"notes": write_notes(last, "U0SAM")})


# REPLACE deletes a row without firing DELETE triggers (unless recursive_triggers is on, which a
# plain connection does not have). The schema must refuse it by itself.


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("INSERT OR REPLACE INTO audit_log (id, at, actor, event) VALUES (1, 'x', 'x', 'forged')", "append-only"),
        (
            "REPLACE INTO requests (id, plan_id, sequence, operation, state, marker_id, updated_at)"
            " SELECT id, plan_id, sequence, operation, 'approved', marker_id, updated_at FROM requests",
            "taken",
        ),
        (
            "REPLACE INTO request_attempts (id, request_id, kind, attempt, marker, snapshot, request_body, started_at)"
            " SELECT id, request_id, kind, attempt, marker, '{}', '{}', started_at FROM request_attempts",
            "taken",
        ),
        (
            "REPLACE INTO posts (id, workspace_id, channel_id, ts, created_at)"
            " SELECT id, 'T9', 'C9', '1.0', 'x' FROM posts",
            "taken",
        ),
        (
            "REPLACE INTO plans (id, post_id, revision, input_hash, state, created_at)"
            " SELECT id, post_id, 7, input_hash, 'current', 'x' FROM plans",
            "taken",
        ),
        ("UPDATE posts SET id = 50", "never changes"),
        ("UPDATE plans SET id = 50", "never changes"),
        ("UPDATE requests SET id = 50", "never changes"),
        ("UPDATE request_attempts SET id = 50", "never changes"),
        ("UPDATE requests SET marker_id = 'iiiiiiiiiiiiiiiiiiiiii'", "CHECK|never changes"),
    ],
)
def test_replace_and_id_changes_are_refused(store: Store, path: Path, statement: str, message: str) -> None:
    plan(store)
    verified(store, key())
    db = raw(path)
    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match=message):
        db.execute(statement)
    assert store.request(key()).state is S.VERIFIED


def test_a_bad_marker_id_is_refused_on_insert(store: Store, path: Path) -> None:
    plan(store)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        raw(path).execute(
            "INSERT INTO requests (plan_id, sequence, operation, state, marker_id, updated_at)"
            " VALUES (1, 9, 'add', 'planned', 'iiiiiiiiiiiiiiiiiiiiii', 'now')"
        )


# Migrations


def test_a_failed_migration_changes_nothing(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Store.open(path).close()
    real = store_db._migrations()
    broken = "CREATE TABLE extra (a INTEGER) STRICT;\nCREATE TABLE posts (a INTEGER);\n"
    monkeypatch.setattr(store_db, "_migrations", lambda: [*real, (LATEST + 1, broken)])
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Store.open(path)
    db = raw(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'extra'").fetchone()[0] == 0


def test_processes_opening_a_new_file_at_once_all_succeed(path: Path) -> None:
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def run() -> None:
        barrier.wait()
        try:
            Store.open(path).close()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert raw(path).execute("PRAGMA user_version").fetchone()[0] == LATEST


def test_statements_keep_trigger_bodies_whole() -> None:
    script = (
        "CREATE TABLE t (a);\n-- a comment\n"
        "CREATE TRIGGER x BEFORE DELETE ON t\nBEGIN SELECT RAISE(ABORT, 'no'); END;\n"
    )
    assert [part.strip() for part in store_db._statements(script)] == [
        "CREATE TABLE t (a);",
        "-- a comment\nCREATE TRIGGER x BEFORE DELETE ON t\nBEGIN SELECT RAISE(ABORT, 'no'); END;",
    ]
    with pytest.raises(RuntimeError, match="incomplete"):
        list(store_db._statements("CREATE TABLE t (a)"))


@pytest.mark.parametrize("table", ["posts", "plans", "requests", "request_attempts", "slack_events"])
@pytest.mark.parametrize("column", ["id", "rowid", "oid", "_rowid_"])
def test_a_row_id_never_changes_under_any_of_its_names(store: Store, path: Path, table: str, column: str) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    assert store.enqueue_event("Ev1", "event", "{}")
    # Without foreign keys, as a tool that opens the file directly would: the triggers alone must hold.
    connection = sqlite3.connect(path, isolation_level=None)
    before = connection.execute(f"SELECT id FROM {table}").fetchall()  # noqa: S608
    assert before == [(1,)]
    with pytest.raises(sqlite3.IntegrityError, match="never changes"):
        connection.execute(f"UPDATE {table} SET {column} = 99")  # noqa: S608
    assert connection.execute(f"SELECT id FROM {table}").fetchall() == before  # noqa: S608
    connection.close()


def test_a_file_from_before_the_row_id_triggers_gets_them_and_keeps_working(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:4])
    old = Store.open(path)
    old.create_post(POST)
    raw(path).execute("UPDATE posts SET rowid = 7")  # the hole, as it was
    old.close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    assert upgraded.schema_version == len(real)
    with pytest.raises(sqlite3.IntegrityError, match="never changes"):
        raw(path).execute("UPDATE posts SET rowid = 8")
    upgraded.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    assert upgraded.create_plan(POST, HASH, (Operation.TRANSFER,)) == 1  # ordinary updates still pass
    upgraded.transition(key(), expected=S.PLANNED, target=S.APPROVED, actor=A.HUMAN)
    upgraded.close()


def test_questions_closed_before_buttons_were_taken_down_are_left_as_they_are(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:11])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    old.place_message(OWNER, POST, user_id="U0SAM0001", thread_ts=None, window_seconds=5)
    old.close()
    with sqlite3.connect(path) as db:  # this code's finish_dry_run writes a later column
        db.execute("UPDATE posts SET dry_run_at = 'then', reply_ts = '1790000020.000100'")
        db.execute(
            "INSERT INTO dry_runs (post_id, outcome, read_through, changes_through, created_at)"
            " VALUES (1, 'replied', 1, 0, 'then')"
        )
        db.execute(
            "INSERT INTO review_items (post_id, revision, question, approver_only, channel_id, message_ts, status,"
            " created_at, poster_id, expires_from) VALUES (1, 1, '{\"ask\": \"confirm\"}', 0, ?, ?, 'open', 'then',"
            " ?, ?)",
            (POST.channel_id, "1790000020.000100", "U0SAM0001", POST.ts),
        )
    raw(path).execute("UPDATE review_items SET status = 'stale', closed_at = 'then'")
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    assert upgraded.closed_with_buttons(limit=10) == []  # never edited after the upgrade
    assert raw(path).execute("SELECT buttons, buttons_tries FROM review_items").fetchall() == [("left", 0)]
    upgraded.close()


# What the marker check reads


def test_recorded_requests_hold_every_request_of_the_post_with_its_attempts(store: Store) -> None:
    plan(store, Operation.TRANSFER, Operation.REMOVE)
    approve(store, key(1, 1))
    first = begin(store, key(1, 1))
    store.transition(key(1, 1), expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)
    store.transition(key(1, 1), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    second = begin(store, key(1, 1))
    other_post = PostKey("T0ACME", "C0STOCK", "1727600099.000100")
    store.create_post(other_post)
    store.create_plan(other_post, HASH, (Operation.ADD,))

    recorded = store.recorded_requests(POST)

    assert [(r.key, r.operation, r.state) for r in recorded] == [
        (key(1, 1), Operation.TRANSFER, S.APPLYING),
        (key(1, 2), Operation.REMOVE, S.PLANNED),
    ]
    assert [r.marker_id for r in recorded] == [store.request(key(1, 1)).marker_id, store.request(key(1, 2)).marker_id]
    assert recorded[0].attempts == (RecordedAttempt(first, Outcome.REFUSED), RecordedAttempt(second, None))
    assert recorded[1].attempts == ()


def test_recorded_requests_span_revisions_and_feed_the_marker_check(store: Store) -> None:
    plan(store)
    approve(store, key())
    applied = begin(store, key())
    store.transition(key(), expected=S.APPLYING, target=S.UNKNOWN, actor=A.APPLIER)
    store.create_plan(POST, HASH, (Operation.TRANSFER,))

    recorded = store.recorded_requests(POST)

    assert [(r.key, r.state) for r in recorded] == [(key(1, 1), S.UNKNOWN), (key(2, 1), S.PLANNED)]
    assert recorded[0].attempts == (RecordedAttempt(applied, Outcome.UNKNOWN),)
    found: dict[str, list[Marker]] = {r.marker_id: [] for r in recorded} | {applied.marker_id: [applied]}
    assert check_markers(recorded, found) == []


def test_recorded_requests_of_an_unknown_post_are_none(store: Store) -> None:
    assert store.recorded_requests(POST) == []


# What recovery concluded about the marker


def unknown(store: Store, request: RequestKey, expected: S | None = None) -> None:
    if expected is S.FAILED:
        store.transition(request, expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    else:
        approve(store, request)
    begin(store, request)
    store.transition(request, expected=S.APPLYING, target=S.UNKNOWN, actor=A.APPLIER)


def refused(store: Store, request: RequestKey) -> None:
    approve(store, request)
    begin(store, request)
    store.transition(request, expected=S.APPLYING, target=S.FAILED, actor=A.APPLIER)


@pytest.mark.parametrize(
    ("target", "found"),
    [(S.APPLIED, True), (S.FAILED, False), (S.VERIFY_FAILED, True), (S.VERIFY_FAILED, False)],
)
def test_recovery_records_whether_it_found_the_marker(store: Store, path: Path, target: S, found: bool) -> None:
    plan(store)
    unknown(store, key())
    store.transition(key(), expected=S.UNKNOWN, target=target, actor=A.RECOVERY, marker_found=found)
    assert store.request(key()).state is target
    assert raw(path).execute("SELECT outcome, marker_found FROM request_attempts").fetchall() == [("unknown", found)]
    (recorded,) = store.recorded_requests(POST)
    assert [attempt.marker_found for attempt in recorded.attempts] == [found]


def test_recovery_of_an_undo_records_it_on_the_undo(store: Store, path: Path) -> None:
    plan(store)
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_UNKNOWN, actor=A.APPLIER)
    store.transition(key(), expected=S.UNDO_UNKNOWN, target=S.VERIFY_FAILED, actor=A.RECOVERY, marker_found=False)
    rows = raw(path).execute("SELECT kind, marker_found FROM request_attempts ORDER BY id").fetchall()
    assert rows == [("a", None), ("u", 0)]


@pytest.mark.parametrize(
    ("expected", "target", "found"),
    [
        (S.UNKNOWN, S.APPLIED, False),
        (S.UNKNOWN, S.FAILED, True),
        (S.UNDO_UNKNOWN, S.UNDONE, False),
        (S.UNDO_UNKNOWN, S.UNDO_FAILED, True),
    ],
)
def test_recovery_must_agree_with_the_target(expected: S, target: S, found: bool) -> None:
    with pytest.raises(ValueError, match="does not follow"):
        store_db._check_recovered(expected, target, found)


def test_recovery_must_say_whether_it_found_the_marker(store: Store, path: Path) -> None:
    plan(store)
    unknown(store, key())
    with pytest.raises(ValueError, match="whether it found"):
        store.transition(key(), expected=S.UNKNOWN, target=S.VERIFY_FAILED, actor=A.RECOVERY)
    with pytest.raises(ValueError, match="does not follow"):
        store.transition(key(), expected=S.UNKNOWN, target=S.APPLIED, actor=A.RECOVERY, marker_found=False)
    assert store.request(key()).state is S.UNKNOWN
    assert raw(path).execute("SELECT marker_found FROM request_attempts").fetchall() == [(None,)]


def test_only_recovery_of_an_unknown_outcome_records_a_marker_conclusion(store: Store) -> None:
    plan(store)
    approve(store, key())
    begin(store, key())
    with pytest.raises(ValueError, match="only recovery"):
        store.transition(key(), expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER, marker_found=True)
    assert store.request(key()).state is S.APPLYING


def test_a_recovered_conclusion_is_recorded_once_and_only_on_an_unknown_outcome(store: Store, path: Path) -> None:
    plan(store)
    refused(store, key())
    unknown(store, key(), expected=S.FAILED)
    store.transition(key(), expected=S.UNKNOWN, target=S.FAILED, actor=A.RECOVERY, marker_found=False)
    db = raw(path)
    with pytest.raises(sqlite3.IntegrityError, match="recovery concludes once"):
        db.execute("UPDATE request_attempts SET marker_found = 1 WHERE outcome = 'unknown'")
    with pytest.raises(sqlite3.IntegrityError, match="recovery concludes once"):
        db.execute("UPDATE request_attempts SET marker_found = 1 WHERE outcome = 'refused'")
    with pytest.raises(sqlite3.IntegrityError, match="starts unrecovered"):
        db.execute(
            "INSERT INTO request_attempts (request_id, kind, attempt, marker, snapshot, request_body, started_at,"
            " marker_found) SELECT request_id, 'u', 1, marker || 'x', '{}', '{}', started_at, 0 FROM request_attempts"
            " LIMIT 1"
        )
    assert db.execute("SELECT marker_found FROM request_attempts ORDER BY id").fetchall() == [(None,), (0,)]


def test_recovery_concludes_about_the_last_attempt_only(store: Store, path: Path) -> None:
    plan(store)
    refused(store, key())
    unknown(store, key(), expected=S.FAILED)
    store.transition(key(), expected=S.UNKNOWN, target=S.APPLIED, actor=A.RECOVERY, marker_found=True)
    assert raw(path).execute("SELECT attempt, outcome, marker_found FROM request_attempts ORDER BY id").fetchall() == [
        (1, "refused", None),
        (2, "unknown", 1),
    ]


def test_a_request_leaves_unknown_only_with_recoverys_conclusion(store: Store, path: Path) -> None:
    plan(store)
    unknown(store, key())
    with pytest.raises(sqlite3.IntegrityError, match="before the request leaves"):
        raw(path).execute("UPDATE requests SET state = 'failed'")
    assert store.request(key()).state is S.UNKNOWN


def found_undo_then_verify_failed(store: Store) -> None:
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_UNKNOWN, actor=A.APPLIER)
    store.transition(key(), expected=S.UNDO_UNKNOWN, target=S.VERIFY_FAILED, actor=A.RECOVERY, marker_found=True)


def test_a_second_undo_after_one_that_wrote_is_blocked(store: Store) -> None:
    # Recovery found the undo's marker but the quantities differed: the undo wrote. Another would reverse twice.
    plan(store)
    found_undo_then_verify_failed(store)
    with pytest.raises(WriteBlocked, match="earlier undo"):
        begin(store, key(), AttemptKind.UNDO, expected=S.VERIFY_FAILED, actor=A.HUMAN)
    assert store.request(key()).state is S.VERIFY_FAILED
    store.transition(key(), expected=S.VERIFY_FAILED, target=S.ACCEPTED, actor=A.HUMAN)


def test_a_new_undo_after_one_recovery_did_not_find_is_blocked(store: Store) -> None:
    # u01 timed out and recovery found no marker but changed quantities. A missing marker proves nothing (u01 may
    # still land, or its history entry was never saved), so u02 could reverse the apply a second time.
    plan(store)
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_UNKNOWN, actor=A.APPLIER)
    store.transition(key(), expected=S.UNDO_UNKNOWN, target=S.VERIFY_FAILED, actor=A.RECOVERY, marker_found=False)
    with pytest.raises(WriteBlocked, match="earlier undo of this request was not refused"):
        begin(store, key(), AttemptKind.UNDO, expected=S.VERIFY_FAILED, actor=A.HUMAN)
    assert store.request(key()).state is S.VERIFY_FAILED


def test_a_new_undo_after_a_refused_one_may_start(store: Store) -> None:
    plan(store)
    verified(store, key())
    begin(store, key(), AttemptKind.UNDO, expected=S.VERIFIED, actor=A.HUMAN)
    store.transition(key(), expected=S.UNDOING, target=S.UNDO_FAILED, actor=A.APPLIER)
    marker = begin(store, key(), AttemptKind.UNDO, expected=S.UNDO_FAILED, actor=A.HUMAN)
    assert (marker.kind, marker.attempt) == (AttemptKind.UNDO, 2)


def test_a_new_apply_after_a_refused_one_may_start(store: Store) -> None:
    plan(store)
    refused(store, key())
    store.transition(key(), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    marker = begin(store, key())
    assert (marker.kind, marker.attempt) == (AttemptKind.APPLY, 2)


def test_a_new_apply_after_one_recovery_did_not_find_is_blocked(store: Store, path: Path) -> None:
    # a01 timed out; recovery found no marker and unchanged quantities, so the request is failed. a01 may still be
    # running on the server and commit after any check the connector makes: a02 would then write twice.
    plan(store)
    unknown(store, key())
    store.transition(key(), expected=S.UNKNOWN, target=S.FAILED, actor=A.RECOVERY, marker_found=False)
    with pytest.raises(WriteBlocked, match="earlier apply of this request was not refused"):
        store.transition(key(), expected=S.FAILED, target=S.APPROVED, actor=A.HUMAN)
    assert store.request(key()).state is S.FAILED
    raw(path).execute("UPDATE requests SET state = 'approved' WHERE id = ?", (store.request(key()).id,))
    with pytest.raises(WriteBlocked, match="earlier apply of this request was not refused"):
        begin(store, key())  # begin_attempt checks again itself


def test_a_write_of_the_post_recovery_did_not_find_blocks_a_new_revision(store: Store, path: Path) -> None:
    # A failed request left no stock change behind, so a new revision of the post could be approved, but the same
    # late landing would add to it.
    plan(store)
    unknown(store, key())
    store.transition(key(), expected=S.UNKNOWN, target=S.FAILED, actor=A.RECOVERY, marker_found=False)
    store.create_plan(POST, "1" * 64, [Operation.TRANSFER])
    with pytest.raises(WriteBlocked, match="recovery could not establish"):
        approve(store, key(2, 1))
    assert store.request(key(2, 1)).state is S.PLANNED
    raw(path).execute("UPDATE requests SET state = 'approved' WHERE id = ?", (store.request(key(2, 1)).id,))
    with pytest.raises(WriteBlocked, match="recovery could not establish"):
        begin(store, key(2, 1))


def test_a_file_from_before_migration_0016_gets_the_recovery_column(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:15])
    old = Store.open(path)
    old.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    plan(old)
    old.close()
    db = raw(path)  # an apply the code of migration 0015 accepted (today's begin_attempt reads marker_found)
    request_id, marker_id = db.execute("SELECT id, marker_id FROM requests").fetchone()
    db.execute("UPDATE requests SET state = 'applied' WHERE id = ?", (request_id,))
    db.execute(
        "INSERT INTO request_attempts (request_id, kind, attempt, marker, snapshot, request_body, started_at, outcome,"
        " finished_at) VALUES (?, 'a', 1, ?, '{}', '{}', '2026-10-01T00:00:00+00:00', 'accepted',"
        " '2026-10-01T00:00:01+00:00')",
        (request_id, str(Marker(marker_id, AttemptKind.APPLY, 1))),
    )
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    assert upgraded.schema_version == len(real)
    (recorded,) = upgraded.recorded_requests(POST)
    assert [(a.outcome, a.marker_found) for a in recorded.attempts] == [(Outcome.ACCEPTED, None)]
    upgraded.transition(key(), expected=S.APPLIED, target=S.VERIFIED, actor=A.APPLIER)
    upgraded.close()


def test_a_new_database_and_its_wal_files_are_readable_by_the_owner_only(tmp_path: Path) -> None:
    """The database holds every accepted Slack post in full: a shared host's umask 022 must not open it (#153)."""
    path = tmp_path / "stockthread.db"
    old = os.umask(0o022)
    try:
        store = Store.open(path)
        store.enqueue_event("Ev0001", "event", "{}")
        files = sorted(tmp_path.iterdir())
        assert [file.name for file in files] == ["stockthread.db", "stockthread.db-shm", "stockthread.db-wal"]
        assert {stat.S_IMODE(file.stat().st_mode) for file in files} == {0o600}
        store.close()
    finally:
        os.umask(old)


def test_an_existing_database_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "stockthread.db"
    Store.open(path).close()
    path.chmod(0o640)
    Store.open(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
