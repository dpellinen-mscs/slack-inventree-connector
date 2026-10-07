"""`apply.undo_apply` against a real server: apply a movement with the whole protocol, then undo it the same way, with
the connector's own reader and writer, and find every total the movement touched back where it was."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from live_server import planned, settings
from pydantic import SecretStr

from stockthread.apply import apply_request, undo_apply
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import MovementType, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeReader
from stockthread.plan import plan_requests
from stockthread.precondition import pairs
from stockthread.run import inventree_stock
from stockthread.slack.replies import MovementView
from stockthread.store import Store
from stockthread.writer import InvenTreeWriter

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

OWNER = "worker-it"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000200")
KEY = RequestKey(POST, 1, 1)
S = RequestState


def allowed(movement: MovementView) -> Callable[[Marker], Judgement]:
    def rejudge(marker: Marker) -> Judgement:
        requests = plan_requests([movement], [marker], user="U0SAM0001")
        return Judgement(Verdict.ALLOWED, "approved by the policy", None, requests)

    return rejudge


def token() -> SecretStr:
    return SecretStr(Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip())


def server() -> InvenTreeSettings:
    return InvenTreeSettings(url=os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=10)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    yield opened
    opened.close()


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [
        (MovementType.TAKE_TO_VEHICLE, "2.5"),  # a split: the new item goes back
        (MovementType.TAKE_TO_VEHICLE, "5"),  # the whole item goes back
        (MovementType.RECEIVE, "3"),
        (MovementType.CONSUME, "1.25"),
        (MovementType.CONSUME, "5"),  # emptied, not deleted: added back
        (MovementType.COUNT, "7"),
    ],
)
def test_an_applied_movement_is_undone_and_every_total_is_back(
    api: RawApi, scratch: Scratch, store: Store, movement_type: MovementType, quantity: str
) -> None:
    item = scratch.stock(5, delete_on_deplete=False)
    movement = planned(scratch, movement_type, item["pk"], quantity)
    marker = Marker(new_marker_id(), AttemptKind.APPLY, 1)
    (operation,) = {request.operation for request in plan_requests([movement], [marker], user="U0SAM0001")}
    store.create_plan(POST, "0" * 64, (operation,))
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)
    read = inventree_stock(settings())
    before = {pair: sum(stock.quantity for stock in items) for pair, items in read(pairs(movement)).items()}

    async def apply_then_undo() -> None:
        lock = anyio.Lock()
        async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(), token()) as writer:
            done = await apply_request(
                KEY, movement, allowed(movement), store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER
            )
            assert done.state is S.VERIFIED, done
            undone = await undo_apply(
                KEY, movement, store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER, user="U0DAN0001"
            )
            assert undone.state is S.UNDONE, undone

    anyio.run(apply_then_undo)
    after = {pair: sum(stock.quantity for stock in items) for pair, items in read(pairs(movement)).items()}
    assert after == before
