# SPDX-License-Identifier: MIT
"""Retention in the store (design section 6, "Privacy"; threat model T10): old message text is blanked, rows stay."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stockthread.core.marker import AttemptKind
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import write_notes
from stockthread.store import DryRunOutcome, EventStatus, ExpiredText, LockLost, NewPlan, Store

S = RequestState
OWNER = "instance-a"
POST = PostKey("T0ACME0001", "C0STOCK001", "1790000000.000100")
KEY = RequestKey(POST, 1, 1)
LATER = timedelta(days=1)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "stockthread.db"


@pytest.fixture
def store(path: Path) -> Iterator[Store]:
    opened = Store.open(path)
    opened.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    yield opened
    opened.close()


def event(store: Store, key: str, text: str, status: EventStatus | None = EventStatus.DONE) -> int:
    """A stored message event, finished as `status` (None: left pending)."""
    assert store.enqueue_event(key, "event", json.dumps({"event": {"type": "message", "text": text}}))
    (event_id,) = store._db.execute("SELECT id FROM slack_events WHERE event_key = ?", (key,)).fetchone()
    if status is not None:
        [claimed] = store.claim_events(OWNER, limit=1)
        assert claimed.id == event_id
        store.finish_event(OWNER, event_id, status)
    return int(event_id)


def replied(store: Store, post: PostKey = POST, *, plan: bool = True) -> int:
    """A post of one message with its dry-run reply and record, and a plan of one transfer when `plan`."""
    event_id = event(store, f"ev:{post.ts}", "moved 2 Disc 40 to Sam's truck")
    store.place_message(OWNER, post, user_id="U0SAM0001", thread_ts=None, window_seconds=5, slack_event_id=event_id)
    store.finish_dry_run(
        OWNER, post, DryRunOutcome.REPLIED, f"{post.ts[:-1]}9", extractor="rules@1",
        result={"movements": [{"part": "Disc 40"}]},
        plan=NewPlan("0" * 64, (Operation.TRANSFER,)) if plan else None,
    )  # fmt: skip
    return event_id


def to_state(store: Store, state: RequestState) -> None:
    """Take the request KEY from planned to `state` along the apply protocol."""
    if state is S.PLANNED:
        return
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    if state is S.APPROVED:
        return
    marker = store.next_marker(KEY, AttemptKind.APPLY)
    store.begin_attempt(
        KEY, AttemptKind.APPLY, expected=S.APPROVED, actor=Actor.APPLIER, snapshot={},
        request_body={"items": [], "location": 4, "notes": write_notes(marker, "U0SAM0001")}, lock_owner=OWNER,
    )  # fmt: skip
    if state is S.APPLYING:
        return
    store.transition(KEY, expected=S.APPLYING, target=S.APPLIED, actor=Actor.APPLIER)
    if state is S.APPLIED:
        return
    store.transition(KEY, expected=S.APPLIED, target=S.VERIFIED, actor=Actor.APPLIER)


def stored(path: Path) -> tuple[list[object], list[object], list[tuple[str, int | None, int | None]]]:
    raw = sqlite3.connect(path)
    try:
        results = [row[0] for row in raw.execute("SELECT result FROM dry_runs ORDER BY post_id")]
        results += [row[0] for row in raw.execute("SELECT result FROM dry_run_revisions ORDER BY id")]
        bodies = [row[0] for row in raw.execute("SELECT body FROM slack_events ORDER BY id")]
        audit = raw.execute(
            "SELECT event, post_id, slack_event_id FROM audit_log WHERE actor = 'retention' ORDER BY id"
        ).fetchall()
    finally:
        raw.close()
    return results, bodies, audit


def now() -> datetime:
    return datetime.now(UTC)


def test_the_text_of_a_post_done_with_is_blanked_and_its_rows_stay(store: Store, path: Path) -> None:
    event_id = replied(store, plan=False)
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=1, events=1)
    results, bodies, audit = stored(path)
    assert results == [None]
    assert bodies == [None]
    assert audit == [("text_expired", 1, None), ("body_expired", None, event_id)]
    [record] = store.dry_run_history(POST)
    assert (record.outcome, record.extractor, record.reply_ts) == (
        DryRunOutcome.REPLIED,
        "rules@1",
        "1790000000.000109",
    )
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=0)  # nothing twice
    assert len(stored(path)[2]) == 2


def test_text_newer_than_the_cutoff_is_kept(store: Store, path: Path) -> None:
    replied(store, plan=False)
    event(store, "ev:dropped", "lunch at noon?", EventStatus.DROPPED)
    assert store.expire_text(OWNER, now() - LATER) == ExpiredText(posts=0, events=0)
    results, bodies, audit = stored(path)
    assert results[0] is not None
    assert None not in bodies
    assert audit == []


def test_an_old_event_of_no_post_is_blanked_once_finished_and_never_while_queued(store: Store, path: Path) -> None:
    event(store, "ev:dropped", "lunch at noon?", EventStatus.DROPPED)
    event(store, "ev:failed", "moved 2 Disc 40", EventStatus.FAILED)
    event(store, "ev:pending", "moved 3 Disc 40", None)
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=2)
    _, bodies, _ = stored(path)
    assert bodies[:2] == [None, None]
    assert bodies[2] is not None
    [claimed] = store.claim_events(OWNER, limit=1)  # processing: still kept
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=0)
    store.finish_event(OWNER, claimed.id, EventStatus.DONE)
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=1)


def test_an_old_plan_nobody_acted_on_is_blanked(store: Store, path: Path) -> None:
    """With writes off no plan is ever applied; its post's text must still expire."""
    replied(store)
    assert store.request(KEY).state is S.PLANNED
    assert store.expire_text(OWNER, now() - LATER) == ExpiredText(posts=0, events=0)
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=1, events=1)
    results, bodies, _ = stored(path)
    assert results[0] is None
    assert bodies[0] is None


@pytest.mark.parametrize("state", [S.APPROVED, S.APPLYING, S.APPLIED])
def test_a_post_whose_request_may_still_be_written_or_read_back_keeps_its_text(
    store: Store, path: Path, state: RequestState
) -> None:
    replied(store)
    to_state(store, state)
    assert store.request(KEY).state is state
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=0)
    results, bodies, _ = stored(path)
    assert results[0] is not None
    assert bodies[0] is not None


@pytest.mark.parametrize("state", [S.UNKNOWN, S.UNDOING, S.UNDO_UNKNOWN])
def test_a_post_with_a_write_in_flight_keeps_its_text(store: Store, path: Path, state: RequestState) -> None:
    replied(store)
    to_state(store, S.APPLYING)
    store._db.execute(  # the protocol's way there is long; the guard reads the state only
        "UPDATE requests SET state = ?, updated_at = '2000-01-01T00:00:00.000000+00:00'", (state.value,)
    )
    assert store.expire_text(OWNER, now() + LATER) == ExpiredText(posts=0, events=0)
    assert stored(path)[0][0] is not None


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_a_post_whose_request_changed_after_the_cutoff_keeps_its_text(path: Path) -> None:
    """Undo's age limit counts from the write: the text stays while an undo may still rebuild the plan."""
    clock = Clock()
    store = Store.open(path, clock=clock)
    store.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    replied(store)
    clock.now += timedelta(days=10)
    to_state(store, S.VERIFIED)
    store.receipt_posted(OWNER, POST, 1, "1790000100.000100", [S.VERIFIED])
    start = datetime(2026, 10, 1, tzinfo=UTC)
    assert store.expire_text(OWNER, start + timedelta(days=5)) == ExpiredText(posts=0, events=0)
    assert stored(path)[0][0] is not None
    assert stored(path)[1][0] is not None
    assert store.expire_text(OWNER, start + timedelta(days=11)) == ExpiredText(posts=1, events=1)
    assert stored(path)[0] == [None]
    store.close()


def test_an_edit_of_a_message_of_a_kept_post_keeps_its_body(path: Path) -> None:
    clock = Clock()
    store = Store.open(path, clock=clock)
    store.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    replied(store)
    to_state(store, S.APPLYING)  # a write in flight keeps the post (an edit cancels a request not yet written)
    edit = event(store, "ev:edit", "moved 3 Disc 40 to Sam's truck")
    store.note_revision(OWNER, POST, edit)
    other = PostKey(POST.workspace_id, POST.channel_id, "1790000500.000100")
    replied(store, other, plan=False)
    edit_of_other = event(store, "ev:edit-other", "moved 4 Disc 40 to Sam's truck")
    store.note_revision(OWNER, other, edit_of_other)
    assert store.expire_text(OWNER, clock.now + LATER) == ExpiredText(posts=1, events=2)
    _, bodies, audit = stored(path)
    assert [body is None for body in bodies] == [False, False, True, True]
    assert ("body_expired", None, edit_of_other) in audit
    store.close()


def test_a_post_due_a_receipt_keeps_its_text_until_the_receipt_is_posted(path: Path) -> None:
    """The receipt is built from the record: a Slack outage longer than retention must not lose it."""
    clock = Clock()
    store = Store.open(path, clock=clock)
    store.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    replied(store)
    to_state(store, S.VERIFIED)
    clock.now += timedelta(days=40)
    cutoff = clock.now - timedelta(days=30)
    assert store.receipts_due() == [(POST, 1)]
    assert store.expire_text(OWNER, cutoff) == ExpiredText(posts=0, events=0)
    store.receipt_posted(OWNER, POST, 1, "1790000100.000100", [S.VERIFIED])
    assert store.expire_text(OWNER, cutoff) == ExpiredText(posts=1, events=1)
    store.close()


def test_a_revision_newer_than_the_cutoff_keeps_the_post_s_text_and_is_blanked_with_it(path: Path) -> None:
    clock = Clock()
    store = Store.open(path, clock=clock)
    store.acquire_lock(OWNER, stale_after=timedelta(minutes=1))
    replied(store, plan=False)
    clock.now += timedelta(days=10)
    joined = PostKey(POST.workspace_id, POST.channel_id, "1790000030.000100")
    store.place_message(OWNER, joined, user_id="U0SAM0001", thread_ts=POST.ts, window_seconds=5)
    store.finish_dry_run(
        OWNER, POST, DryRunOutcome.REPLIED, "1790000030.000109", extractor="rules@1",
        result={"movements": [{"part": "Disc 40 Pro"}]}, revision=2,
    )  # fmt: skip
    start = datetime(2026, 10, 1, tzinfo=UTC)
    assert store.expire_text(OWNER, start + timedelta(days=5)) == ExpiredText(posts=0, events=0)
    assert None not in stored(path)[0]
    assert store.expire_text(OWNER, start + timedelta(days=11)) == ExpiredText(posts=1, events=1)
    assert stored(path)[0] == [None, None]
    store.close()


def test_retention_needs_the_lock(store: Store, path: Path) -> None:
    replied(store, plan=False)
    with pytest.raises(LockLost):
        store.expire_text("instance-b", now() + LATER)
    assert stored(path)[0][0] is not None
