"""One undo end to end (`apply.undo_apply`), against the fake InvenTree that moves a whole stock item: the apply takes
item 57 from location 10 to location 11, the undo takes it back. The real server, splits, adds, removes and counts
included: integration/test_undo_server.py."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from views import DESTINATION, SOURCE, picked, view

from stockthread.applier import undo_refusal
from stockthread.apply import (
    Applied,
    _check_stored_undo,
    apply_request,
    recover_request,
    recover_undo,
    undo_apply,
)
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import UNDOABLE, Actor, RequestState, next_states
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeError
from stockthread.inverse import Reversal, undo_request
from stockthread.plan import plan_requests
from stockthread.store import Store
from stockthread.writer import Outcome, WriteRefused

S = RequestState
OWNER = "worker-1"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
MOVE = view(stock=(picked((ITEM, "2")),))


def allowed(marker: Marker) -> Judgement:
    return Judgement(Verdict.ALLOWED, "approved by the policy", None, plan_requests([MOVE], [marker], user="U0SAM0001"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (Operation.TRANSFER,))
    opened.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    yield opened
    opened.close()


async def applied(store: Store) -> FakeInvenTree:
    """The fake after a verified apply of MOVE."""
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    done = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert done.state is S.VERIFIED
    return fake


async def undo(store: Store, fake: FakeInvenTree) -> Applied:
    assert fake.lock is not None
    return await undo_apply(
        KEY, MOVE, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER, user="U0DAN0001"
    )


def undo_marker(store: Store, attempt: int = 1) -> Marker:
    return Marker(store.request(KEY).marker_id, AttemptKind.UNDO, attempt)


@pytest.mark.anyio
async def test_a_verified_apply_is_undone_once_and_read_back(store: Store) -> None:
    fake = await applied(store)
    assert await undo(store, fake) == Applied(S.UNDONE, "undo read back: as built")
    assert fake.items[ITEM][1] == SOURCE
    assert len(fake.sent) == 2
    assert fake.sent[1].body == {
        "items": [{"pk": ITEM, "quantity": "2", "merge": False}],
        "notes": f"{undo_marker(store)} U0DAN0001",
        "location": SOURCE,
    }
    assert store.attempt_snapshot(undo_marker(store)) == {
        "totals": {f"1@{SOURCE}": "0", f"1@{DESTINATION}": "2"},
        "where": {str(ITEM): f"1@{DESTINATION}"},
        "history": {str(ITEM): [570, 1001]},
    }
    with pytest.raises(ValueError, match="undone, not one an undo may start from"):
        await undo(store, fake)
    assert len(fake.sent) == 2


@pytest.mark.anyio
async def test_a_request_not_written_cannot_be_undone(store: Store) -> None:
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    with pytest.raises(ValueError, match="approved, not one an undo may start from"):
        await undo(store, fake)
    assert fake.sent == []


@pytest.mark.anyio
async def test_stock_that_moved_on_since_the_apply_refuses_the_undo_and_changes_nothing(store: Store) -> None:
    fake = await applied(store)
    fake.items[ITEM][2] -= 1
    fake.write_entry(ITEM, "someone else", {"removed": 1.0}, 12)
    refused = await undo(store, fake)
    assert refused.state is S.VERIFIED
    assert refused.why.startswith("no undo: stock item 57 has history entries 1002 since the apply")
    assert store.request(KEY).state is S.VERIFIED
    assert store.markers(KEY) == [Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)]
    assert len(fake.sent) == 1


@pytest.mark.anyio
async def test_a_history_that_disagrees_with_the_records_refuses_the_undo(store: Store) -> None:
    fake = await applied(store)
    fake.write_entry(ITEM, f"{undo_marker(store)} U0DAN0001", {"location": SOURCE}, 20)  # an undo nobody recorded
    refused = await undo(store, fake)
    assert refused.state is S.VERIFIED
    assert refused.why == "no undo: the stock history disagrees with the records"
    assert refused.problems
    assert len(fake.sent) == 1


@pytest.mark.parametrize(("answer", "target"), [(Outcome.REFUSED, S.UNDO_FAILED), (Outcome.UNKNOWN, S.UNDO_UNKNOWN)])
@pytest.mark.anyio
async def test_an_undo_inventree_refused_or_did_not_answer(store: Store, answer: Outcome, target: S) -> None:
    fake = await applied(store)
    fake.answer = answer
    assert (await undo(store, fake)).state is target
    assert store.request(KEY).state is target
    assert fake.items[ITEM][1] == DESTINATION


@pytest.mark.anyio
async def test_a_refused_undo_may_be_tried_again_with_a_new_marker(store: Store) -> None:
    fake = await applied(store)
    fake.raises = WriteRefused("the body is not one the writer sends")
    assert (await undo(store, fake)).state is S.UNDO_FAILED
    fake.raises = None
    assert (await undo(store, fake)).state is S.UNDONE
    assert str(fake.sent[-1].body["notes"]).startswith(str(undo_marker(store, 2)))


@pytest.mark.anyio
async def test_an_undo_whose_send_raised_is_unknown(store: Store) -> None:
    fake = await applied(store)
    fake.raises = TimeoutError()
    with pytest.raises(TimeoutError):
        await undo(store, fake)
    assert store.request(KEY).state is S.UNDO_UNKNOWN


@pytest.mark.anyio
async def test_an_undo_accepted_whose_read_back_disagrees_is_verify_failed(store: Store) -> None:
    """InvenTree said 201, but the item did not move (no entry either): the undo is not verified, and a second undo
    is refused (an accepted undo may have written)."""
    fake = await applied(store)
    fake.moves = False
    done = await undo(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert "stock item 57 has 0 history entries of the undo, not 1" in done.problems
    assert f"stock item 57 is not at location {SOURCE}" in done.problems
    assert store.request(KEY).state is S.VERIFY_FAILED
    (request,) = store.recorded_requests(POST)
    assert [(attempt.marker, attempt.outcome.value if attempt.outcome else None) for attempt in request.attempts][
        -1
    ] == (
        undo_marker(store),
        "accepted",
    )
    fake.moves = True
    refused = await undo(store, fake)
    assert refused.state is S.VERIFY_FAILED
    assert refused.why.startswith("no undo: ")
    assert len(fake.sent) == 2


@pytest.mark.anyio
async def test_another_writer_between_the_check_and_the_undo_is_found_by_the_read_back(store: Store) -> None:
    fake = await applied(store)

    def meddle() -> None:
        fake.write_entry(ITEM, "someone else", {}, 1)

    fake.before_write = meddle
    done = await undo(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems == ("stock item 57 has history entries 1002 that are not this request's",)


@pytest.mark.anyio
async def test_a_read_error_in_the_undo_read_back_leaves_it_unknown(store: Store) -> None:
    fake = await applied(store)
    fake.fail_reads_after_write = True
    fake.sent.clear()
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    assert store.request(KEY).state is S.UNDO_UNKNOWN
    assert fake.items[ITEM][1] == SOURCE


@pytest.mark.anyio
async def test_a_reversed_item_deleted_before_the_undo_refuses_it_and_changes_nothing(store: Store) -> None:
    fake = await applied(store)
    del fake.items[ITEM]  # used up at the destination; InvenTree deleted it and keeps its history
    refused = await undo(store, fake)
    assert refused == Applied(S.VERIFIED, "no undo: stock item 57 no longer exists")
    assert store.markers(KEY) == [Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)]
    assert len(fake.sent) == 1


@pytest.mark.anyio
async def test_a_reversed_item_deleted_right_after_the_undo_is_verify_failed_not_unknown(store: Store) -> None:
    fake = await applied(store)

    def used_up() -> None:
        del fake.items[ITEM]

    fake.after_write = used_up
    done = await undo(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems == (
        f"stock item 57 is not at location {SOURCE}",
        f"part 1 at location {SOURCE} holds 0, the undo should leave 2",
        "stock item 57 is gone",
    )


@pytest.mark.anyio
async def test_a_read_error_before_the_undo_changes_nothing(store: Store) -> None:
    fake = await applied(store)
    fake.fail_stock_reads = True
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    assert store.request(KEY).state is S.VERIFIED
    assert store.markers(KEY) == [Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)]


async def unknown_undo(store: Store, *, lands: bool) -> FakeInvenTree:
    """The fake after a verified apply of MOVE and an undo whose answer was lost: `lands`, the undo ran all the
    same."""
    fake = await applied(store)
    fake.answer, fake.lands = Outcome.UNKNOWN, lands
    assert (await undo(store, fake)).state is S.UNDO_UNKNOWN
    fake.answer, fake.lands = Outcome.ACCEPTED, False
    return fake


async def recover(store: Store, fake: FakeInvenTree, settle: timedelta = timedelta(0)) -> Applied:
    assert fake.lock is not None
    return await recover_undo(KEY, MOVE, store=store, reader=fake, lock=fake.lock, settle=settle)


def undo_found(store: Store) -> bool | None:
    (request,) = store.recorded_requests(POST)
    return request.attempts[-1].marker_found


@pytest.mark.anyio
async def test_recovery_of_an_undo_waits_for_the_settle_time_and_reads_nothing(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    fake.fail_reads = True
    waiting = await recover(store, fake, settle=timedelta(hours=1))
    assert waiting == Applied(S.UNDO_UNKNOWN, "still settling: the undo may be running on the server")
    assert store.request(KEY).state is S.UNDO_UNKNOWN


@pytest.mark.anyio
async def test_an_undo_that_landed_is_found_and_read_back_as_undone(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    assert await recover(store, fake) == Applied(S.UNDONE, "undo marker found; read back: as built")
    assert undo_found(store) is True
    assert len(fake.sent) == 2


@pytest.mark.anyio
async def test_an_undo_that_landed_but_reads_back_wrong_is_verify_failed(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    fake.write_entry(ITEM, "someone else", {}, 1)
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems == ("stock item 57 has history entries 1003 that are not this request's",)
    assert undo_found(store) is True


@pytest.mark.anyio
async def test_an_undo_that_never_ran_is_undo_failed_and_never_sent_again(store: Store) -> None:
    fake = await unknown_undo(store, lands=False)
    recovered = await recover(store, fake)
    assert recovered == Applied(S.UNDO_FAILED, "undo marker not found; nothing changed, so it never ran")
    assert undo_found(store) is False
    assert fake.items[ITEM][1] == DESTINATION
    refused = await undo(store, fake)  # it may still land: a second undo would reverse the apply twice
    assert refused.state is S.UNDO_FAILED
    assert refused.why.startswith("no undo: an earlier undo of this request was not refused by InvenTree")
    assert len(fake.sent) == 2


@pytest.mark.anyio
async def test_an_undo_not_found_whose_stock_changed_is_verify_failed(store: Store) -> None:
    """The undo's entry is missing (1.5.6 with PostgreSQL drops an entry whose notes are too long), but the item moved
    back all the same: never concluded as not run."""
    fake = await unknown_undo(store, lands=False)
    fake.items[ITEM][1] = SOURCE
    fake.write_entry(ITEM, "", {"location": SOURCE}, 20)
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.why == "undo marker not found; the stock changed"
    assert done.problems == (
        f"stock item 57 is no longer at location {DESTINATION}",
        f"part 1 at location {SOURCE} holds 2, the undo's snapshot says 0",
        f"part 1 at location {DESTINATION} holds 0, the undo's snapshot says 2",
        "stock item 57 has history entries 1002 the undo's snapshot lacks",
    )
    assert undo_found(store) is False


@pytest.mark.anyio
async def test_an_undo_not_found_with_another_attempt_in_the_history_is_verify_failed(store: Store) -> None:
    fake = await unknown_undo(store, lands=False)
    fake.write_entry(ITEM, f"{undo_marker(store, 2)} U0DAN0001", {}, 1)  # an attempt nobody recorded
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems[0] == "history entries 1002 carry another attempt"


@pytest.mark.anyio
async def test_a_read_error_in_undo_recovery_changes_nothing(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    fake.fail_stock_reads = True
    with pytest.raises(InvenTreeError):
        await recover(store, fake)
    assert store.request(KEY).state is S.UNDO_UNKNOWN
    assert undo_found(store) is None
    fake.fail_stock_reads = False
    assert (await recover(store, fake)).state is S.UNDONE


@pytest.mark.anyio
async def test_recovery_concludes_an_undo_whose_read_back_could_not_read(store: Store) -> None:
    fake = await applied(store)
    fake.fail_reads_after_write = True
    fake.sent.clear()
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    fake.fail_reads_after_write = False
    assert (await recover(store, fake)).state is S.UNDONE


@pytest.mark.anyio
async def test_only_an_unknown_undo_is_recovered(store: Store) -> None:
    fake = await applied(store)
    with pytest.raises(ValueError, match="verified, not undo_unknown"):
        await recover(store, fake)


def test_a_stored_undo_the_apply_history_does_not_make_is_refused() -> None:
    found = [Reversal(ITEM, Decimal(2), Decimal(2))]
    marker = Marker(new_marker_id(), AttemptKind.UNDO, 1)
    body = dict(undo_request(MOVE, found, marker, user="U0DAN0001").body)
    _check_stored_undo(MOVE, found, marker, body)
    with pytest.raises(ValueError, match="not the one the apply's history makes"):
        _check_stored_undo(MOVE, found, marker, {**body, "items": [{"pk": ITEM, "quantity": "1", "merge": False}]})
    with pytest.raises(ValueError, match="does not lead its notes with its marker"):
        _check_stored_undo(MOVE, found, Marker(marker.marker_id, AttemptKind.UNDO, 2), body)


@pytest.mark.anyio
async def test_an_undo_found_with_another_attempt_in_the_history_is_verify_failed(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    fake.write_entry(ITEM, f"{undo_marker(store, 2)} U0DAN0001", {}, 1)  # an attempt nobody recorded
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems[0] == "history entries 1003 carry another attempt"
    assert undo_found(store) is True


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.anyio
async def test_an_apply_history_that_no_longer_shows_what_the_undo_reversed_is_verify_failed(
    store: Store, lands: bool
) -> None:
    fake = await unknown_undo(store, lands=lands)
    fake.entries = [entry for entry in fake.entries if entry[0] != 1001]  # the apply's entry, deleted by an admin
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.why == "the apply's history no longer shows what the undo reversed"
    (problem,) = done.problems
    assert problem.startswith("stock item 57 has 0 history entries of the apply, not 1: ")
    assert undo_found(store) is lands


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.anyio
async def test_a_reversed_item_deleted_before_recovery_is_verify_failed(store: Store, lands: bool) -> None:
    """InvenTree refuses the history of an item it deleted (`test_char_deplete_deletes_item_keeps_history`):
    recovery reports the item gone instead of failing on every run, the undo found or not."""
    fake = await unknown_undo(store, lands=lands)
    del fake.items[ITEM]
    done = await recover(store, fake)
    assert done.state is S.VERIFY_FAILED
    assert done.problems[-1] == "stock item 57 is gone"
    assert undo_found(store) is lands


@pytest.mark.anyio
async def test_a_history_read_error_on_an_item_that_exists_still_raises_in_recovery(store: Store) -> None:
    fake = await unknown_undo(store, lands=True)
    fake.history_fails = {ITEM}
    with pytest.raises(InvenTreeError):
        await recover(store, fake)
    assert store.request(KEY).state is S.UNDO_UNKNOWN


@pytest.mark.anyio
async def test_a_history_read_error_on_an_item_that_exists_raises_around_the_undo(store: Store) -> None:
    """Only an item InvenTree says is gone is reported gone: before the undo the error raises and nothing is sent;
    in the undo's read-back it leaves the undo unknown."""
    fake = await applied(store)
    fake.history_fails = {ITEM}
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    assert store.request(KEY).state is S.VERIFIED
    assert len(fake.sent) == 1

    def fail_history() -> None:
        fake.history_fails = {ITEM}

    fake.history_fails = set()
    fake.after_write = fail_history
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    assert store.request(KEY).state is S.UNDO_UNKNOWN


WEEK = timedelta(days=7)


def test_an_undo_starts_by_a_person_from_exactly_the_undoable_states() -> None:
    assert {state for state in S if S.UNDOING in next_states(state, Actor.HUMAN)} == UNDOABLE


@pytest.mark.anyio
async def test_a_verified_movement_may_be_undone_once(store: Store) -> None:
    fake = await applied(store)
    assert store.undo_refusal(KEY, max_age=WEEK) is None
    assert (await undo(store, fake)).state is S.UNDONE
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "the request is undone: only a movement that was written and read back can be undone"
    )


def test_a_request_never_written_may_not_be_undone(store: Store) -> None:
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "the request is approved: only a movement that was written and read back can be undone"
    )


@pytest.mark.anyio
async def test_a_failed_apply_may_not_be_undone(store: Store) -> None:
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    fake.answer = Outcome.REFUSED
    done = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert done.state is S.FAILED
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "the request is failed: only a movement that was written and read back can be undone"
    )


@pytest.mark.anyio
async def test_an_undo_inventree_refused_may_be_asked_again(store: Store) -> None:
    fake = await applied(store)
    fake.answer = Outcome.REFUSED
    assert (await undo(store, fake)).state is S.UNDO_FAILED
    assert store.undo_refusal(KEY, max_age=WEEK) is None


@pytest.mark.anyio
async def test_an_undo_recovery_found_never_ran_is_not_asked_again(store: Store) -> None:
    """`undo_failed` twice over: refused by InvenTree (may be asked again) or concluded by recovery (it may still
    land, so never again)."""
    fake = await unknown_undo(store, lands=False)
    assert (await recover(store, fake)).state is S.UNDO_FAILED
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "an earlier undo was not refused by InvenTree: it wrote, or it may still land"
    )


@pytest.mark.anyio
async def test_a_verify_failed_undo_is_not_asked_again_but_a_verify_failed_apply_is(store: Store) -> None:
    fake = await applied(store)
    fake.moves = False
    assert (await undo(store, fake)).state is S.VERIFY_FAILED
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "an earlier undo was not refused by InvenTree: it wrote, or it may still land"
    )


@pytest.mark.anyio
async def test_an_apply_whose_read_back_disagreed_may_be_undone(store: Store) -> None:
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    fake.before_write = lambda: fake.write_entry(ITEM, "someone else", {}, 1)
    done = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert done.state is S.VERIFY_FAILED
    assert store.undo_refusal(KEY, max_age=WEEK) is None


def test_an_undo_expires_max_age_after_the_apply_wrote_by_the_store_clock(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 7, 9, 0, tzinfo=UTC)]
    opened = Store.open(tmp_path / "clock.db", clock=lambda: now[0])
    opened.acquire_lock(OWNER, stale_after=timedelta(days=30))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (Operation.TRANSFER,))
    opened.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    anyio.run(applied, opened)
    now[0] += WEEK - timedelta(seconds=1)
    assert opened.undo_refusal(KEY, max_age=WEEK) is None
    now[0] += timedelta(seconds=1)
    assert opened.undo_refusal(KEY, max_age=WEEK) == (
        "the movement was written more than 168 hours ago: undo it in InvenTree"
    )
    opened.close()


@pytest.mark.parametrize(
    ("user", "refusal"),
    [
        ("U0SAM0001", None),  # the poster
        ("U0DAN0001", None),  # an approver
        ("U0ALEX001", "only the poster or an approver may undo this movement"),
    ],
)
@pytest.mark.anyio
async def test_the_poster_or_an_approver_may_ask_for_an_undo(store: Store, user: str, refusal: str | None) -> None:
    await applied(store)
    asked = undo_refusal(store, KEY, user=user, poster="U0SAM0001", approvers={"U0DAN0001"}, max_age=WEEK)
    assert asked == refusal


@pytest.mark.anyio
async def test_the_poster_may_ask_for_an_undo_with_no_approvers(store: Store) -> None:
    """What check-setup's no-approvers warning says (`setup_check.check_policy`): only the poster can Undo."""
    await applied(store)
    assert undo_refusal(store, KEY, user="U0SAM0001", poster="U0SAM0001", approvers=(), max_age=WEEK) is None


def test_who_asks_is_checked_before_the_store(store: Store) -> None:
    asked = undo_refusal(store, KEY, user="U0ALEX001", poster="U0SAM0001", approvers=(), max_age=WEEK)
    assert asked == "only the poster or an approver may undo this movement"


@pytest.mark.anyio
async def test_a_read_back_failure_before_any_write_may_not_be_undone(store: Store) -> None:
    """verify_failed with no apply that wrote: the marker check found the request's marker before it was sent."""
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    marker = Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)
    fake.write_entry(ITEM, f"{marker} written by hand", {}, 1)
    done = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert done.state is S.VERIFY_FAILED
    assert fake.sent == []
    assert store.undo_refusal(KEY, max_age=WEEK) == "no apply of this request wrote"


@pytest.mark.anyio
async def test_an_apply_refused_then_tried_again_is_undone_from_the_one_that_wrote(store: Store) -> None:
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    fake.answer = Outcome.REFUSED
    first = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert first.state is S.FAILED
    store.transition(KEY, expected=S.FAILED, target=S.APPROVED, actor=Actor.HUMAN)
    fake.answer = Outcome.ACCEPTED
    again = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert again.state is S.VERIFIED
    assert store.undo_refusal(KEY, max_age=WEEK) is None


@pytest.mark.anyio
async def test_an_apply_recovery_found_may_be_undone(store: Store) -> None:
    fake = FakeInvenTree()
    fake.lock = anyio.Lock()
    fake.answer, fake.lands = Outcome.UNKNOWN, True
    done = await apply_request(
        KEY, MOVE, allowed, store=store, reader=fake, writer=fake, lock=fake.lock, lock_owner=OWNER
    )
    assert done.state is S.UNKNOWN
    assert store.undo_refusal(KEY, max_age=WEEK) == (
        "the request is unknown: only a movement that was written and read back can be undone"
    )
    fake.answer, fake.lands = Outcome.ACCEPTED, False
    recovered = await recover_request(KEY, MOVE, store=store, reader=fake, lock=fake.lock, settle=timedelta(0))
    assert recovered.state is S.VERIFIED
    assert store.undo_refusal(KEY, max_age=WEEK) is None
    assert (await undo(store, fake)).state is S.UNDONE
