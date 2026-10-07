"""One undo end to end (`apply.undo_apply`), against the fake InvenTree that moves a whole stock item: the apply takes
item 57 from location 10 to location 11, the undo takes it back. The real server, splits, adds, removes and counts
included: integration/test_undo_server.py."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from fake_inventree import ITEM, FakeInvenTree
from views import DESTINATION, SOURCE, picked, view

from stockthread.apply import Applied, apply_request, undo_apply
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeError
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
async def test_a_read_error_before_the_undo_changes_nothing(store: Store) -> None:
    fake = await applied(store)
    fake.fail_stock_reads = True
    with pytest.raises(InvenTreeError):
        await undo(store, fake)
    assert store.request(KEY).state is S.VERIFIED
    assert store.markers(KEY) == [Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)]
