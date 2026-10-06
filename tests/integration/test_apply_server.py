"""`apply.apply_request` against a real server: the whole protocol, with the connector's own reader and writer."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from faultproxy import Fault, FaultProxy, Rule
from pydantic import SecretStr
from views import picked, view

from stockthread.apply import apply_request, recover_request
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import Marker
from stockthread.core.model import Match, MatchRule, MatchStatus, Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import PATHS
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeReader
from stockthread.plan import plan_requests
from stockthread.slack.replies import MovementView
from stockthread.store import Store
from stockthread.writer import InvenTreeWriter

if TYPE_CHECKING:
    from conftest import Scratch

OWNER = "worker-it"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000100")
KEY = RequestKey(POST, 1, 1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    yield opened
    opened.close()


def approved(store: Store, operation: Operation) -> None:
    store.create_plan(POST, "0" * 64, (operation,))
    store.transition(KEY, expected=RequestState.PLANNED, target=RequestState.APPROVED, actor=Actor.HUMAN)


def taken(scratch: Scratch, pk: int, quantity: str) -> MovementView:
    """`quantity` of stock item `pk` from the scratch parent location to the other one."""
    return view(
        lines=(("Disc 40", quantity),),
        stock=(picked((pk, quantity)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.part),),
        source=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent),
        destination=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other),
    )


def allowed(movement: MovementView) -> Callable[[Marker], Judgement]:
    def rejudge(marker: Marker) -> Judgement:
        requests = plan_requests([movement], [marker], user="U0SAM0001")
        return Judgement(Verdict.ALLOWED, "approved by the policy", None, requests)

    return rejudge


def token() -> SecretStr:
    return SecretStr(Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip())


def server(url: str | None = None) -> InvenTreeSettings:
    return InvenTreeSettings(url=url or os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=10)


async def run(store: Store, movement: MovementView, write_via: str | None = None) -> RequestState:
    """`write_via`: the URL the writer sends to (a fault proxy); the reader always reads the server itself."""
    async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(write_via), token()) as writer:
        applied = await apply_request(
            KEY,
            movement,
            allowed(movement),
            store=store,
            reader=reader,
            writer=writer,
            lock=anyio.Lock(),
            lock_owner=OWNER,
        )
    assert applied.problems == (), applied.problems
    return applied.state


async def recover(store: Store, movement: MovementView) -> RequestState:
    async with InvenTreeReader(server(), token()) as reader:
        applied = await recover_request(
            KEY, movement, store=store, reader=reader, lock=anyio.Lock(), settle=timedelta(0)
        )
    assert applied.problems == (), applied.problems
    return applied.state


@pytest.mark.parametrize("quantity", ["2", "5"])  # a split, and the whole item
@pytest.mark.anyio
async def test_a_transfer_is_applied_and_verified(store: Store, scratch: Scratch, quantity: str) -> None:
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    assert await run(store, taken(scratch, item["pk"], quantity)) is RequestState.VERIFIED
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == Decimal(quantity)


@pytest.mark.anyio
async def test_stock_taken_since_the_plan_sends_nothing(store: Store, scratch: Scratch) -> None:
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    scratch.api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 4}], "notes": "x"})
    assert await run(store, taken(scratch, item["pk"], "2")) is RequestState.PLANNED
    assert scratch.api.items(scratch.part, scratch.other) == []


@pytest.fixture
def proxy(scratch: Scratch) -> Iterator[FaultProxy]:
    with FaultProxy(os.environ["STOCKTHREAD_IT_URL"]) as running:
        yield running


@pytest.mark.parametrize(
    ("fault", "landed"),
    [(Fault.DROP_AFTER, True), (Fault.DROP_BEFORE, False), (Fault.FAIL_AFTER, True), (Fault.FAIL_BEFORE, False)],
)
@pytest.mark.anyio
async def test_recovery_concludes_an_unknown_outcome_from_the_server(
    store: Store, scratch: Scratch, proxy: FaultProxy, fault: Fault, landed: bool
) -> None:
    """The answer is lost or is a proxy's 502. The write ran (after) or never reached the server (before)."""
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    movement = taken(scratch, item["pk"], "2")
    proxy.inject(Rule("POST", PATHS[Operation.TRANSFER], fault, status=502))
    assert await run(store, movement, write_via=proxy.url) is RequestState.UNKNOWN
    proxy.wait_for(1)
    assert await recover(store, movement) is (RequestState.VERIFIED if landed else RequestState.FAILED)
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == (Decimal(2) if landed else 0)
