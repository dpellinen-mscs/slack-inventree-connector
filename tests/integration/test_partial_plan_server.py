# SPDX-License-Identifier: MIT
"""A plan of two requests against a real server (ADR 0002, M3 DoD): the first is verified, the second's write never
reaches the server and recovery concludes it `failed`. The plan is partly applied, and the undo of the first puts
every total back; the second's stock item never moved."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from faultproxy import Fault, FaultProxy, Rule
from live_server import planned, settings
from pydantic import SecretStr

from stockthread.apply import apply_request, recover_request, undo_apply
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import MovementType, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import PATHS
from stockthread.gate import Judgement, Verdict
from stockthread.inventree import InvenTreeReader
from stockthread.plan import plan_requests
from stockthread.precondition import pairs
from stockthread.run import inventree_stock
from stockthread.slack.replies import MovementView, receipt
from stockthread.store import Store
from stockthread.writer import InvenTreeWriter

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

OWNER = "worker-it"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000300")
FIRST, SECOND = RequestKey(POST, 1, 1), RequestKey(POST, 1, 2)
S = RequestState


def allowed(movements: list[MovementView], key: RequestKey) -> Callable[[Marker], Judgement]:
    """The whole plan judged again before request `key` is sent, with `marker` as its marker; the other requests carry
    placeholders of their own (`apply_request` sends request `key` alone)."""

    def rejudge(marker: Marker) -> Judgement:
        markers = [
            marker if sequence == key.sequence else Marker(new_marker_id(), AttemptKind.APPLY, 1)
            for sequence in range(1, len(movements) + 1)
        ]
        return Judgement(
            Verdict.ALLOWED, "approved by the policy", None, plan_requests(movements, markers, user="U0SAM0001")
        )

    return rejudge


def token() -> SecretStr:
    return SecretStr(Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip())


def server(url: str | None = None) -> InvenTreeSettings:
    return InvenTreeSettings(url=url or os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=10)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    yield opened
    opened.close()


def test_a_plan_whose_second_write_failed_is_partly_applied_and_undo_restores_the_first(
    api: RawApi, scratch: Scratch, store: Store
) -> None:
    one, two = scratch.stock(5, delete_on_deplete=False), scratch.stock(4, delete_on_deplete=False)
    movements = [
        planned(scratch, MovementType.TAKE_TO_VEHICLE, one["pk"], "2"),
        planned(scratch, MovementType.TAKE_TO_VEHICLE, two["pk"], "1"),
    ]
    operations = tuple(
        request.operation
        for request in allowed(movements, FIRST)(Marker(new_marker_id(), AttemptKind.APPLY, 1)).requests
    )
    store.create_plan(POST, "0" * 64, operations)
    store.approve_plan(OWNER, POST, 1, actor=Actor.POLICY)
    read = inventree_stock(settings())

    def totals() -> dict[tuple[int, int], object]:
        return {pair: sum(stock.quantity for stock in items) for pair, items in read(pairs(movements[0])).items()}

    before = totals()

    async def apply_both_then_undo() -> None:
        lock = anyio.Lock()
        with FaultProxy(os.environ["STOCKTHREAD_IT_URL"]) as proxy:
            async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(proxy.url), token()) as w:
                done = await apply_request(
                    FIRST,
                    movements[0],
                    allowed(movements, FIRST),
                    store=store,
                    reader=reader,
                    writer=w,
                    lock=lock,
                    lock_owner=OWNER,
                )
                assert done.state is S.VERIFIED, done
                proxy.inject(Rule("POST", PATHS[operations[1]], Fault.DROP_BEFORE))
                lost = await apply_request(
                    SECOND,
                    movements[1],
                    allowed(movements, SECOND),
                    store=store,
                    reader=reader,
                    writer=w,
                    lock=lock,
                    lock_owner=OWNER,
                )
                assert lost.state is S.UNKNOWN, lost
                proxy.wait_for(1)
                failed = await recover_request(
                    SECOND, movements[1], store=store, reader=reader, lock=lock, settle=timedelta(0)
                )
                assert failed.state is S.FAILED, failed
                states = [store.request(key).state for key in (FIRST, SECOND)]
                markers = [store.request(key).marker_id for key in (FIRST, SECOND)]
                shown = receipt(movements, states, markers, {}, {}, inventree_url="", approved_by=None)
                assert shown["text"] == "Partly applied: 2 movements."
                heading = shown["blocks"][0]["text"]["text"]
                assert "Check InvenTree before recording any of it again" in heading, heading
                assert "post it again" not in heading, heading  # the recovered write may still land (ADR 0015)
                undone = await undo_apply(
                    FIRST, movements[0], store=store, reader=reader, writer=w, lock=lock, lock_owner=OWNER,
                    user="U0DAN0001",
                )  # fmt: skip
                assert undone.state is S.UNDONE, undone

    anyio.run(apply_both_then_undo)
    assert [store.request(key).state for key in (FIRST, SECOND)] == [S.UNDONE, S.FAILED]
    assert totals() == before
    assert float(scratch.item(two["pk"])["quantity"]) == 4
    assert int(scratch.item(two["pk"])["location"]) == scratch.parent
