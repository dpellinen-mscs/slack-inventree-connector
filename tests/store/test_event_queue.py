"""The durable Slack event queue: store before acknowledging, pick up after a restart (design section 4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stockthread.store import MAX_EVENT_ATTEMPTS, EventStatus, LockLost, StateConflict, Store
from stockthread.store import db as store_db

OWNER = "instance-a"
OTHER = "instance-b"
BODY = '{"event_id": "Ev1"}'


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def store(path: Path) -> Iterator[Store]:
    opened = Store.open(path)
    opened.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    yield opened
    opened.close()


def raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def test_a_retry_is_stored_once(store: Store) -> None:
    assert store.enqueue_event("Ev1", "event", BODY) is True
    assert store.enqueue_event("Ev1", "event", BODY) is False
    (event,) = store.claim_events(OWNER)
    assert (event.key, event.kind, event.body, event.attempts) == ("Ev1", "event", BODY, 1)


def test_events_are_claimed_oldest_first_and_once(store: Store) -> None:
    for key in ("Ev1", "Ev2", "Ev3"):
        store.enqueue_event(key, "event", BODY)
    assert [e.key for e in store.claim_events(OWNER, limit=2)] == ["Ev1", "Ev2"]
    assert [e.key for e in store.claim_events(OWNER)] == ["Ev3"]
    assert store.claim_events(OWNER) == []


def test_only_the_lock_holder_claims_and_finishes(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with pytest.raises(LockLost):
        store.claim_events(OTHER)
    (event,) = store.claim_events(OWNER)
    with pytest.raises(LockLost):
        store.finish_event(OTHER, event.id, EventStatus.DONE)


def test_an_event_finishes_once(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    store.finish_event(OWNER, event.id, EventStatus.DROPPED, "channel_not_allowed")
    assert store.event_status(event.id) == (EventStatus.DROPPED, 1, "channel_not_allowed")
    with pytest.raises(StateConflict):
        store.finish_event(OWNER, event.id, EventStatus.DONE)
    with pytest.raises(ValueError, match="finishes"):
        store.finish_event(OWNER, event.id, EventStatus.PENDING)


def test_a_pending_event_cannot_be_finished(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with pytest.raises(StateConflict):
        store.finish_event(OWNER, 1, EventStatus.DONE)


def test_unfinished_events_are_picked_up_after_a_restart(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    store.claim_events(OWNER)
    store.close()
    restarted = Store.open(path)  # the old process died while processing; its lock is stale
    restarted.acquire_lock(OTHER, stale_after=timedelta(0))
    assert restarted.requeue_unfinished(OTHER) == 1
    (event,) = restarted.claim_events(OTHER)
    assert event.attempts == 2
    restarted.close()


def restart(store: Store, owner: str) -> None:
    """Another process takes over after this one died."""
    store.acquire_lock(owner, stale_after=timedelta(0))
    store.requeue_unfinished(owner, max_attempts=3)


def test_an_event_that_keeps_crashing_the_connector_stops_being_retried(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    owners = ["run-1", "run-2", "run-3"]
    for owner in owners:
        restart(store, owner)
        (event,) = store.claim_events(owner)  # and the process dies while processing it
    restart(store, "run-4")
    assert store.event_status(event.id) == (EventStatus.FAILED, 3, "too_many_attempts")
    assert store.claim_events("run-4") == []


def test_requeue_leaves_this_instances_own_work_alone(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    assert store.requeue_unfinished(OWNER) == 0
    assert store.event_status(event.id)[0] is EventStatus.PROCESSING


def test_delivery_is_at_least_once_across_a_takeover(store: Store) -> None:
    # The old instance stalls mid-event; the new one runs the event again. Downstream must be idempotent.
    store.enqueue_event("Ev1", "event", BODY)
    (first,) = store.claim_events(OWNER)
    restart(store, OTHER)
    (second,) = store.claim_events(OTHER)
    assert (second.id, second.attempts) == (first.id, 2)
    with pytest.raises(LockLost):
        store.finish_event(OWNER, first.id, EventStatus.DONE)
    store.finish_event(OTHER, second.id, EventStatus.DONE)


def test_claims_need_a_positive_limit(store: Store) -> None:
    with pytest.raises(ValueError, match="at least one"):
        store.claim_events(OWNER, limit=-1)


def test_an_outcome_is_a_code_never_text(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    with pytest.raises(ValueError, match="never text"):
        store.finish_event(OWNER, event.id, EventStatus.DONE, "hello from alice")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        raw(path).execute("UPDATE slack_events SET outcome = 'hello from alice'")


def test_the_audit_log_records_ids_only(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    store.finish_event(OWNER, event.id, EventStatus.DONE)
    rows = raw(path).execute("SELECT event, slack_event_id FROM audit_log ORDER BY id").fetchall()
    assert rows == [("event_stored", event.id), ("event_done", event.id)]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE slack_events SET event_key = 'Ev2'", "never changes its key"),
        ("UPDATE slack_events SET received_at = 'now'", "never changes its key"),
        ("UPDATE slack_events SET body = '{}'", "only expired"),
        ("UPDATE slack_events SET status = 'done'", "illegal"),  # pending -> done skips processing
        ("DELETE FROM slack_events", "never deleted"),
        ("INSERT OR REPLACE INTO slack_events (event_key, kind, received_at) VALUES ('Ev1', 'event', 'now')", "taken"),
        ("INSERT INTO slack_events (event_key, kind, received_at) VALUES ('', 'event', 'now')", "CHECK"),
    ],
)
def test_the_database_guards_the_queue(store: Store, path: Path, statement: str, message: str) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with pytest.raises(sqlite3.IntegrityError, match=message):
        raw(path).execute(statement)


def test_a_finished_event_never_goes_back(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    store.finish_event(OWNER, event.id, EventStatus.DONE)
    for status in ("pending", "processing", "failed"):
        with pytest.raises(sqlite3.IntegrityError, match="illegal"):
            raw(path).execute("UPDATE slack_events SET status = ?", (status,))


def test_retention_may_expire_the_body_only_of_a_finished_event(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with pytest.raises(sqlite3.IntegrityError, match="only expired once finished"):
        raw(path).execute("UPDATE slack_events SET body = NULL")  # pending: it was acknowledged, keep it
    (event,) = store.claim_events(OWNER)
    with pytest.raises(sqlite3.IntegrityError, match="only expired once finished"):
        raw(path).execute("UPDATE slack_events SET body = NULL")
    store.finish_event(OWNER, event.id, EventStatus.DONE)
    raw(path).execute("UPDATE slack_events SET body = NULL")
    assert raw(path).execute("SELECT body FROM slack_events").fetchone() == (None,)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE slack_events SET attempts = 0", "never go down"),
        ("UPDATE slack_events SET claimed_by = 'thief'", "changes hands"),
        ("UPDATE slack_events SET claimed_by = NULL", "changes hands"),
    ],
)
def test_the_database_guards_a_claim(store: Store, path: Path, statement: str, message: str) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    store.claim_events(OWNER)
    with pytest.raises(sqlite3.IntegrityError, match=message):
        raw(path).execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE slack_events SET outcome = 'something_else'",
        "UPDATE slack_events SET outcome = NULL",
        "UPDATE slack_events SET attempts = attempts + 1",
    ],
)
def test_a_finished_event_is_frozen(store: Store, path: Path, statement: str) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    store.finish_event(OWNER, event.id, EventStatus.DROPPED, "bot")
    with pytest.raises(sqlite3.IntegrityError, match="never changes"):
        raw(path).execute(statement)


def test_a_version_1_file_upgrades(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = store_db._migrations()
    monkeypatch.setattr(store_db, "_migrations", lambda: real[:1])
    Store.open(path).close()
    monkeypatch.setattr(store_db, "_migrations", lambda: real)
    upgraded = Store.open(path)
    assert upgraded.schema_version == len(real)
    upgraded.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    assert upgraded.enqueue_event("Ev1", "event", BODY)
    upgraded.close()


def test_an_event_put_off_is_claimed_again_only_once_its_time_comes(path: Path) -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    store = Store.open(path, clock=lambda: now[0])
    store.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    store.enqueue_event("Ev1", "event", BODY)
    store.enqueue_event("Ev2", "event", BODY)
    (first,) = store.claim_events(OWNER, limit=1)
    store.retry_event_later(OWNER, first.id, timedelta(seconds=5))
    assert store.event_status(first.id) == (EventStatus.PENDING, 1, None)
    assert store.events_pending()
    (second,) = store.claim_events(OWNER)  # the put-off event is skipped, not the one behind it
    assert second.key == "Ev2"
    store.finish_event(OWNER, second.id, EventStatus.DONE)
    now[0] += timedelta(seconds=4, microseconds=999999)
    assert store.claim_events(OWNER) == []
    assert store.events_pending()
    now[0] += timedelta(microseconds=1)
    (again,) = store.claim_events(OWNER)
    assert (again.id, again.attempts) == (first.id, 2)
    store.finish_event(OWNER, again.id, EventStatus.DONE)
    assert not store.events_pending()
    with raw(path) as connection:
        assert connection.execute("SELECT not_before FROM slack_events").fetchall() == [(None,), (None,)]
    store.close()


def test_only_the_instance_processing_an_event_may_put_it_off(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with pytest.raises(StateConflict):
        store.retry_event_later(OWNER, 1, timedelta(seconds=5))  # pending, not claimed
    (event,) = store.claim_events(OWNER)
    restart(store, OTHER)
    with pytest.raises(LockLost):
        store.retry_event_later(OWNER, event.id, timedelta(seconds=5))
    (event,) = store.claim_events(OTHER)
    store.finish_event(OTHER, event.id, EventStatus.FAILED, "error_operational_error")
    with pytest.raises(StateConflict):
        store.retry_event_later(OTHER, event.id, timedelta(seconds=5))


def test_retries_and_restarts_share_one_attempt_budget(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    for _ in range(MAX_EVENT_ATTEMPTS - 1):
        (event,) = store.claim_events(OWNER)
        store.retry_event_later(OWNER, event.id, timedelta(0))
    (event,) = store.claim_events(OWNER)  # and the process dies while processing it
    store.acquire_lock(OTHER, stale_after=timedelta(0))
    store.requeue_unfinished(OTHER)
    assert store.event_status(event.id) == (EventStatus.FAILED, MAX_EVENT_ATTEMPTS, "too_many_attempts")


def test_an_event_is_put_off_only_as_it_goes_back_to_the_queue(store: Store, path: Path) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    with raw(path) as connection, pytest.raises(sqlite3.IntegrityError, match="put off only"):
        connection.execute("UPDATE slack_events SET not_before = '2026-10-04T12:00:00.000000+00:00'")
    (event,) = store.claim_events(OWNER)
    store.finish_event(OWNER, event.id, EventStatus.DONE)
    with raw(path) as connection, pytest.raises(sqlite3.IntegrityError, match="put off only"):
        connection.execute("UPDATE slack_events SET not_before = '2026-10-04T12:00:00.000000+00:00'")


def test_an_event_put_off_past_the_longest_delay_is_claimable_at_once(path: Path) -> None:
    """The clock stepped back after the event was put off: it does not wait for the clock to catch up."""
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    store = Store.open(path, clock=lambda: now[0])
    store.acquire_lock(OWNER, stale_after=timedelta(days=1))
    store.enqueue_event("Ev1", "event", BODY)
    (event,) = store.claim_events(OWNER)
    store.retry_event_later(OWNER, event.id, store_db.MAX_EVENT_RETRY_DELAY)
    assert store.claim_events(OWNER) == []  # within the longest delay: it waits
    now[0] -= timedelta(hours=1)
    (again,) = store.claim_events(OWNER)
    assert again.id == event.id
    with pytest.raises(ValueError, match="no more than"):
        store.retry_event_later(OWNER, event.id, store_db.MAX_EVENT_RETRY_DELAY + timedelta(microseconds=1))
    store.close()


def test_the_worker_takes_up_its_own_events_left_processing(store: Store) -> None:
    store.enqueue_event("Ev1", "event", BODY)
    store.enqueue_event("Ev2", "event", BODY)
    store.claim_events(OWNER)
    assert store.events_pending()
    assert store.requeue_unfinished(OWNER, own=True) == 2
    assert [event.attempts for event in store.claim_events(OWNER)] == [2, 2]
