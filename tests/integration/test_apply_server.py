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

from stockthread.apply import apply_request, check_post_markers, recover_request, recover_undo, undo_apply
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import Marker
from stockthread.core.marker_check import FindingKind
from stockthread.core.model import Match, MatchRule, MatchStatus, Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import PATHS
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeReader
from stockthread.plan import plan_requests
from stockthread.slack.replies import MovementView
from stockthread.store import Store, WriteBlocked
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


def server(url: str | None = None, timeout: int = 10) -> InvenTreeSettings:
    return InvenTreeSettings(url=url or os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=timeout)


async def run(store: Store, movement: MovementView, write_via: str | None = None, timeout: int = 10) -> RequestState:
    """`write_via`: the URL the writer sends to (a fault proxy), with `timeout`; the reader always reads the server
    itself."""
    writes = server(write_via, timeout)
    async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(writes, token()) as writer:
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


@pytest.mark.parametrize(
    ("fault", "landed"),
    [(Fault.DROP_AFTER, True), (Fault.DROP_BEFORE, False), (Fault.FAIL_AFTER, True), (Fault.FAIL_BEFORE, False)],
)
@pytest.mark.parametrize("quantity", ["2", "5"])  # the undo of a split, and of a whole item
@pytest.mark.anyio
async def test_recovery_concludes_an_unknown_undo_from_the_server(
    store: Store, scratch: Scratch, proxy: FaultProxy, fault: Fault, landed: bool, quantity: str
) -> None:
    """The undo's answer is lost or is a proxy's 502. It ran (after): undone, every item back at the source. It never
    reached the server (before): undo_failed, the apply's stock standing, and no second undo sent."""
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    movement = taken(scratch, item["pk"], quantity)
    assert await run(store, movement) is RequestState.VERIFIED
    proxy.inject(Rule("POST", PATHS[Operation.TRANSFER], fault, status=502))
    lock = anyio.Lock()
    async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(proxy.url), token()) as writer:
        undo = await undo_apply(
            KEY, movement, store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER, user="U0DAN0001"
        )
        assert undo.state is RequestState.UNDO_UNKNOWN, undo
        proxy.wait_for(1)
        recovered = await recover_undo(KEY, movement, store=store, reader=reader, lock=lock, settle=timedelta(0))
        assert recovered.problems == (), recovered.problems
        assert recovered.state is (RequestState.UNDONE if landed else RequestState.UNDO_FAILED)
        if not landed:  # it may still land: a second undo would reverse the apply twice
            again = await undo_apply(
                KEY,
                movement,
                store=store,
                reader=reader,
                writer=writer,
                lock=lock,
                lock_owner=OWNER,
                user="U0DAN0001",
            )
            assert again.why.startswith("no undo: an earlier undo of this request was not refused"), again
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == (0 if landed else Decimal(quantity))
    back = scratch.api.items(scratch.part, scratch.parent)
    assert sum(Decimal(str(row["quantity"])) for row in back) == (Decimal(5) if landed else 5 - Decimal(quantity))


@pytest.mark.anyio
async def test_a_named_item_deleted_since_the_plan_sends_nothing(store: Store, scratch: Scratch) -> None:
    item = scratch.stock(5, delete_on_deplete=True)
    approved(store, Operation.TRANSFER)
    scratch.api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 5}], "notes": "x"})
    assert scratch.api.status(f"/api/stock/{item['pk']}/") == 404
    assert await run(store, taken(scratch, item["pk"], "2")) is RequestState.PLANNED
    assert scratch.api.items(scratch.part, scratch.other) == []


@pytest.mark.parametrize(("fault", "landed"), [(Fault.DROP_AFTER, True), (Fault.DROP_BEFORE, False)])
@pytest.mark.anyio
async def test_recovery_of_an_unknown_undo_whose_item_was_deleted_concludes(
    store: Store, scratch: Scratch, proxy: FaultProxy, fault: Fault, landed: bool
) -> None:
    """The reversed item is used up and deleted before recovery runs. Its history query would answer 400; recovery
    concludes verify_failed instead of failing on every run. On this server the apply's entries no longer show the
    deleted item either, so the apply-history check concludes it first; the 400 path is in test_undo_apply.py."""
    item = scratch.stock(5, delete_on_deplete=True)
    approved(store, Operation.TRANSFER)
    movement = taken(scratch, item["pk"], "5")
    assert await run(store, movement) is RequestState.VERIFIED
    proxy.inject(Rule("POST", PATHS[Operation.TRANSFER], fault, status=502))
    lock = anyio.Lock()
    async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(proxy.url), token()) as writer:
        undo = await undo_apply(
            KEY, movement, store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER, user="U0DAN0001"
        )
        assert undo.state is RequestState.UNDO_UNKNOWN, undo
        proxy.wait_for(1)
        scratch.api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 5}], "notes": "used up"})
        assert scratch.api.status(f"/api/stock/{item['pk']}/") == 404
        recovered = await recover_undo(KEY, movement, store=store, reader=reader, lock=lock, settle=timedelta(0))
    assert recovered.state is RequestState.VERIFY_FAILED, recovered
    assert recovered.problems, recovered
    assert store.recorded_requests(POST)[0].attempts[-1].marker_found is landed


def transfers_sent(proxy: FaultProxy) -> list[int | None]:
    """The upstream status of every transfer the proxy saw: None for one that never reached the server."""
    return [entry.upstream_status for entry in proxy.log if entry.path == PATHS[Operation.TRANSFER]]


@pytest.mark.anyio
async def test_an_answer_after_the_client_timeout_is_recovered_and_never_sent_again(
    store: Store, scratch: Scratch, proxy: FaultProxy
) -> None:
    """M3 DoD: a response that arrives after the client timeout never leads to a second write. The server commits and
    answers 3 s later, after the writer gave up at 1 s: unknown, then recovery finds the marker and reads it back.
    A person asking to apply it again is refused, and so is a new revision of the post: the stock moved once."""
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    movement = taken(scratch, item["pk"], "2")
    proxy.inject(Rule("POST", PATHS[Operation.TRANSFER], Fault.DELAY, seconds=3.0))
    assert await run(store, movement, write_via=proxy.url, timeout=1) is RequestState.UNKNOWN
    assert [entry.upstream_status for entry in proxy.wait_for(1)] == [201]
    assert await recover(store, movement) is RequestState.VERIFIED
    with pytest.raises(ValueError, match="not approved"):  # the same request again
        await run(store, movement, write_via=proxy.url)
    revision = store.create_plan(POST, "1" * 64, (Operation.TRANSFER,))
    with pytest.raises(WriteBlocked, match="left a stock change behind"):
        store.transition(
            RequestKey(POST, revision, 1), expected=RequestState.PLANNED, target=RequestState.APPROVED,
            actor=Actor.HUMAN,
        )  # fmt: skip
    assert transfers_sent(proxy) == [201]
    assert len(store.markers(KEY)) == 1
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == Decimal(2)
    assert Decimal(str(scratch.item(item["pk"])["quantity"])) == Decimal(3)


@pytest.mark.anyio
async def test_a_write_that_lands_after_recovery_concluded_failed_is_never_sent_again(
    store: Store, scratch: Scratch, proxy: FaultProxy
) -> None:
    """M3 DoD: the same with the worst timing (ADR 0015). The write reaches the server only after the writer gave up
    and after recovery found no marker and no change (`failed`). Before it lands and after, a person's retry and a
    new revision of the post are refused, so nothing else is sent; once it lands the marker check reports it."""
    item = scratch.stock(5)
    approved(store, Operation.TRANSFER)
    movement = taken(scratch, item["pk"], "2")
    proxy.inject(Rule("POST", PATHS[Operation.TRANSFER], Fault.HOLD))
    assert await run(store, movement, write_via=proxy.url, timeout=1) is RequestState.UNKNOWN
    proxy.wait_held()
    assert await recover(store, movement) is RequestState.FAILED
    with pytest.raises(WriteBlocked, match="may still land"):  # a person's retry of the same request
        store.transition(KEY, expected=RequestState.FAILED, target=RequestState.APPROVED, actor=Actor.HUMAN)
    revision = store.create_plan(POST, "1" * 64, (Operation.TRANSFER,))  # an edit: the post's next revision

    def refused() -> None:
        with pytest.raises(WriteBlocked, match="may still land"):
            store.transition(
                RequestKey(POST, revision, 1), expected=RequestState.PLANNED, target=RequestState.APPROVED,
                actor=Actor.HUMAN,
            )  # fmt: skip

    refused()
    async with InvenTreeReader(server(), token()) as reader:
        assert await check_post_markers(reader, store, POST) == []
    proxy.release()
    assert proxy.wait_for(1)[0].upstream_status == 201  # it landed, after recovery
    refused()
    async with InvenTreeReader(server(), token()) as reader:
        findings = await check_post_markers(reader, store, POST)
    assert [(finding.key, finding.kind) for finding in findings] == [(KEY, FindingKind.HIT)], findings
    assert transfers_sent(proxy) == [201]
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == Decimal(2)
    assert Decimal(str(scratch.item(item["pk"])["quantity"])) == Decimal(3)
