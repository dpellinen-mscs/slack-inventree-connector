"""The apply protocol end to end for one request (`apply.apply_request`), against a fake InvenTree that moves a
whole stock item the way InvenTree does (`test_char_history_deltas`). The real server: test_apply_server.py."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from views import DESTINATION, SOURCE, picked, view

from stockthread.apply import Applied, apply_request
from stockthread.core.marker import AttemptKind, Marker, marked
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import WriteRequest
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, MarkedEntry, ServerInfo
from stockthread.plan import plan_requests
from stockthread.slack.replies import MovementView
from stockthread.store import Store
from stockthread.writer import Outcome, Sent, WriteRefused

S = RequestState
OWNER = "worker-1"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000100")
KEY = RequestKey(POST, 1, 1)
ITEM = 57
MOVE = view(stock=(picked((ITEM, "2")),))
"""Take all 2 of item 57 (part 1) from location 10 to location 11: a whole move, one entry of type 20."""


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


class FakeInvenTree:
    """Stock items by pk as [part, location, quantity], and history entries as (pk, item, notes, deltas, type)."""

    def __init__(self, held: str = "2") -> None:
        self.items: dict[int, list[Any]] = {ITEM: [1, SOURCE, Decimal(held)]}
        self.entries: list[tuple[int, int | None, str, dict[str, Any], int]] = [(570, ITEM, "", {}, 1)]
        self.sent: list[WriteRequest] = []
        self.answer = Outcome.ACCEPTED
        self.raises: BaseException | None = None
        self.moves = True
        self.before_write: Callable[[], None] | None = None
        self.fail_reads = False
        self.fail_reads_after_write = False
        self.lock: anyio.Lock | None = None

    def write_entry(self, item: int, notes: str, deltas: dict[str, Any], kind: int) -> None:
        self.entries.append((1000 + len(self.entries), item, notes, deltas, kind))

    def _read(self) -> None:
        if self.fail_reads or (self.fail_reads_after_write and self.sent):
            raise InvenTreeError("GET /api/stock/ failed: ConnectError")

    async def server_info(self) -> ServerInfo:
        self._read()
        return ServerInfo(version="1.5.6", api_version=DEPLETING_MARKER_FOUND_FROM)

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]:
        self._read()
        return [
            MarkedEntry(pk, item, found, deltas, kind)
            for pk, item, notes, deltas, kind in self.entries
            if (found := marked(notes, marker_id)) is not None
        ]

    async def stock_item_gone(self, pk: int) -> bool:
        self._read()
        return pk not in self.items

    async def item_entries(self, pk: int) -> list[int]:
        self._read()
        return [entry for entry, item, *_ in self.entries if item == pk]

    async def stock_items(self, part: int, location: int) -> list[dict[str, Any]]:
        self._read()
        return [
            {"pk": pk, "part": part, "location": location, "quantity": str(quantity), "status": 10, "allocated": 0}
            for pk, (on, at, quantity) in sorted(self.items.items())
            if (on, at) == (part, location)
        ]

    async def send(self, request: WriteRequest) -> Sent:
        assert self.lock is not None
        assert self.lock.locked(), "a write is sent under the writer lock"
        self.sent.append(request)
        if self.raises is not None:
            raise self.raises
        if self.answer is not Outcome.ACCEPTED:
            return Sent(self.answer, f"POST {request.path}: {self.answer}")
        if self.before_write is not None:
            self.before_write()
        if self.moves:
            for line in request.body["items"]:
                stock = self.items[int(line["pk"])]
                stock[1] = request.body["location"]
                self.write_entry(
                    int(line["pk"]),
                    str(request.body["notes"]),
                    {"location": request.body["location"], "quantity": float(stock[2])},
                    20,
                )
        return Sent(Outcome.ACCEPTED, f"POST {request.path}: 201", 201)


def allowed(movement: MovementView = MOVE) -> Callable[[Marker], Judgement]:
    def rejudge(marker: Marker) -> Judgement:
        requests = plan_requests([movement], [marker], user="U0SAM0001")
        return Judgement(Verdict.ALLOWED, "approved by the policy", None, requests)

    return rejudge


async def run(store: Store, fake: FakeInvenTree, rejudge: Callable[[Marker], Judgement] | None = None) -> Applied:
    lock = fake.lock = anyio.Lock()
    return await apply_request(
        KEY, MOVE, rejudge or allowed(), store=store, reader=fake, writer=fake, lock=lock, lock_owner=OWNER
    )


@pytest.mark.anyio
async def test_a_movement_as_planned_is_written_once_and_verified(store: Store) -> None:
    fake = FakeInvenTree()
    applied = await run(store, fake)
    assert applied == Applied(S.VERIFIED, "read back: as planned")
    assert store.request(KEY).state is S.VERIFIED
    assert len(fake.sent) == 1
    assert fake.items[ITEM][1] == DESTINATION
    (marker,) = store.markers(KEY)
    assert marker == Marker(store.request(KEY).marker_id, AttemptKind.APPLY, 1)
    assert store.attempt_lines(marker) == [(ITEM, Decimal(2))]
    assert store.attempt_snapshot(marker)["stock_items"] == {str(ITEM): "2"}
    assert store.attempt_snapshot(marker)["history"] == {str(ITEM): [570]}


@pytest.mark.anyio
async def test_a_blocked_gate_reads_and_writes_nothing(store: Store) -> None:
    fake = FakeInvenTree()
    fake.fail_reads = True  # none may happen
    blocked = Judgement(Verdict.BLOCKED, "stock writes are off")
    applied = await run(store, fake, lambda marker: blocked)
    assert applied == Applied(S.APPROVED, "not written: stock writes are off")
    assert store.request(KEY).state is S.APPROVED
    assert fake.sent == []
    assert store.markers(KEY) == []


@pytest.mark.anyio
async def test_a_marker_already_in_the_history_stops_the_apply(store: Store) -> None:
    fake = FakeInvenTree()
    marker = store.next_marker(KEY, AttemptKind.APPLY)
    fake.write_entry(ITEM, f"{marker} U0SAM0001", {}, 20)  # written by a lost database's earlier run
    applied = await run(store, fake)
    assert applied.state is S.VERIFY_FAILED
    assert len(applied.problems) == 1
    assert str(marker) in applied.problems[0]
    assert store.request(KEY).state is S.VERIFY_FAILED
    assert fake.sent == []
    assert store.markers(KEY) == []


@pytest.mark.anyio
async def test_stock_that_changed_since_the_plan_goes_back_to_review(store: Store) -> None:
    fake = FakeInvenTree(held="1")
    applied = await run(store, fake)
    assert applied.state is S.PLANNED
    assert applied.why.startswith("back to review: ")
    assert store.request(KEY).state is S.PLANNED
    assert fake.sent == []
    assert store.markers(KEY) == []


@pytest.mark.parametrize(("answer", "state"), [(Outcome.REFUSED, S.FAILED), (Outcome.UNKNOWN, S.UNKNOWN)])
@pytest.mark.anyio
async def test_an_answer_that_is_not_an_acceptance_is_recorded_and_not_read_back(
    store: Store, answer: Outcome, state: RequestState
) -> None:
    fake = FakeInvenTree()
    fake.answer = answer
    applied = await run(store, fake)
    assert applied.state is state
    assert store.request(KEY).state is state
    assert len(fake.sent) == 1
    assert len(store.markers(KEY)) == 1  # the intent was stored before the write


@pytest.mark.anyio
async def test_a_request_the_writer_refuses_to_send_failed(store: Store) -> None:
    fake = FakeInvenTree()
    fake.raises = WriteRefused("not a request build_request makes")
    applied = await run(store, fake)
    assert applied == Applied(S.FAILED, "not sent: not a request build_request makes")
    assert store.request(KEY).state is S.FAILED


@pytest.mark.anyio
async def test_an_error_while_sending_is_an_unknown_outcome(store: Store) -> None:
    fake = FakeInvenTree()
    fake.raises = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await run(store, fake)
    assert store.request(KEY).state is S.UNKNOWN


@pytest.mark.anyio
async def test_a_cancellation_while_sending_is_an_unknown_outcome(store: Store) -> None:
    fake = FakeInvenTree()
    fake.raises = anyio.get_cancelled_exc_class()()
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await run(store, fake)
    assert store.request(KEY).state is S.UNKNOWN


@pytest.mark.anyio
async def test_an_accepted_write_that_changed_nothing_fails_the_read_back(store: Store) -> None:
    fake = FakeInvenTree()
    fake.moves = False
    applied = await run(store, fake)
    assert applied.state is S.VERIFY_FAILED
    assert applied.problems
    assert store.request(KEY).state is S.VERIFY_FAILED


@pytest.mark.anyio
async def test_another_writer_between_the_snapshot_and_the_write_fails_the_read_back(store: Store) -> None:
    fake = FakeInvenTree()
    fake.before_write = lambda: fake.write_entry(ITEM, "recount by hand", {"quantity": 2.0}, 10)
    applied = await run(store, fake)
    assert applied.state is S.VERIFY_FAILED
    assert applied.problems == ("stock item 57: history entries 1001 are another writer's, during the apply",)


@pytest.mark.anyio
async def test_a_read_error_in_the_read_back_leaves_the_request_applied(store: Store) -> None:
    fake = FakeInvenTree()
    fake.fail_reads_after_write = True
    with pytest.raises(InvenTreeError):
        await run(store, fake)
    assert store.request(KEY).state is S.APPLIED
    assert len(fake.sent) == 1


@pytest.mark.anyio
async def test_a_read_error_before_the_write_leaves_the_request_approved(store: Store) -> None:
    fake = FakeInvenTree()

    async def broken(part: int, location: int) -> list[dict[str, Any]]:
        raise InvenTreeError("GET /api/stock/ failed: ConnectError")

    fake.stock_items = broken  # type: ignore[method-assign]
    with pytest.raises(InvenTreeError):
        await run(store, fake)
    assert store.request(KEY).state is S.APPROVED
    assert fake.sent == []
    assert store.markers(KEY) == []


@pytest.mark.anyio
async def test_only_an_approved_request_is_applied(store: Store) -> None:
    store.transition(KEY, expected=S.APPROVED, target=S.PLANNED, actor=Actor.APPLIER)
    fake = FakeInvenTree()
    with pytest.raises(ValueError, match="not approved"):
        await run(store, fake)
    assert fake.sent == []


@pytest.mark.anyio
async def test_a_second_run_after_a_verified_write_sends_nothing(store: Store) -> None:
    fake = FakeInvenTree()
    await run(store, fake)
    with pytest.raises(ValueError, match="not approved"):
        await run(store, fake)
    assert len(fake.sent) == 1
