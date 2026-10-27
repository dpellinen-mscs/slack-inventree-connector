# SPDX-License-Identifier: MIT
"""M3 DoD against a real server: depletion with deletion, and serialized stock, are verified and undone correctly or
go to review.

Depletion: a write that leaves a delete_on_deplete item at 0 deletes it. The apply is still verified (the marker's
entry stays, the totals are right); its undo is refused with a reason, since a remove or a count is undone on the
item itself, which is gone. A transfer of the whole item deletes nothing and is undone.

Serialized stock is moved whole, by transfer: verified and undone. A remove or a count of a serialized item, which
InvenTree would answer with success and leave unchanged, is never sent: the precondition sends the request back to
review, and the stock is as it was."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from live_server import planned, settings
from pydantic import SecretStr
from views import picked, view

from stockthread.apply import Applied, apply_request, undo_apply
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Match, MatchRule, MatchStatus, MovementType, PostKey, RequestKey
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
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000300")
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


def approve(store: Store, movement: MovementView) -> None:
    marker = Marker(new_marker_id(), AttemptKind.APPLY, 1)
    (operation,) = {request.operation for request in plan_requests([movement], [marker], user="U0SAM0001")}
    store.create_plan(POST, "0" * 64, (operation,))
    store.transition(KEY, expected=S.PLANNED, target=S.APPROVED, actor=Actor.HUMAN)


def totals(movement: MovementView) -> dict[tuple[int, int], Decimal]:
    read = inventree_stock(settings())
    return {pair: sum((stock.quantity for stock in items), Decimal(0)) for pair, items in read(pairs(movement)).items()}


def apply_then_undo(
    store: Store, movement: MovementView, between: Callable[[], None] = lambda: None
) -> tuple[Applied, Applied]:
    """Apply `movement` with the whole protocol, run `between`, then ask for its undo."""

    async def both() -> tuple[Applied, Applied]:
        lock = anyio.Lock()
        async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(), token()) as writer:
            done = await apply_request(
                KEY, movement, allowed(movement), store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER
            )
            between()
            undone = await undo_apply(
                KEY, movement, store=store, reader=reader, writer=writer, lock=lock, lock_owner=OWNER, user="U0DAN0001"
            )
            return done, undone

    return anyio.run(both)


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [(MovementType.CONSUME, "5"), (MovementType.COUNT, "0")],  # a remove of all it held, a count to 0
)
def test_a_write_that_deletes_its_item_is_verified_and_its_undo_refused(
    api: RawApi, scratch: Scratch, store: Store, movement_type: MovementType, quantity: str
) -> None:
    item = scratch.stock(5, delete_on_deplete=True)
    movement = planned(scratch, movement_type, item["pk"], quantity)
    approve(store, movement)
    before = totals(movement)

    def deleted() -> None:
        assert api.status(f"/api/stock/{item['pk']}/") == 404

    done, undone = apply_then_undo(store, movement, deleted)
    assert (done.state, done.problems) == (S.VERIFIED, ()), done
    assert undone.state is S.VERIFIED, undone
    assert undone.why.startswith("no undo: "), undone
    assert "is deleted" in undone.why, undone
    assert set(totals(movement).values()) == {Decimal(0)} != set(before.values())
    assert [attempt.marker.kind for attempt in store.recorded_requests(POST)[0].attempts] == [AttemptKind.APPLY]


def test_a_transfer_of_a_whole_delete_on_deplete_item_is_undone(api: RawApi, scratch: Scratch, store: Store) -> None:
    """The whole item moves: nothing is left at 0, nothing is deleted, and the item itself goes back."""
    item = scratch.stock(5, delete_on_deplete=True)
    movement = planned(scratch, MovementType.TAKE_TO_VEHICLE, item["pk"], "5")
    approve(store, movement)
    before = totals(movement)
    done, undone = apply_then_undo(store, movement)
    assert (done.state, undone.state) == (S.VERIFIED, S.UNDONE), (done, undone)
    assert totals(movement) == before
    assert scratch.item(item["pk"])["location"] == scratch.parent


def serialized(scratch: Scratch, *serials: str) -> list[dict[str, Any]]:
    """One stock item of the scratch trackable part per serial number, at the scratch parent location."""
    body = {"part": scratch.trackable_part, "location": scratch.parent, "quantity": len(serials)}
    response = scratch.api.post("/api/stock/", {**body, "serial_numbers": ",".join(serials)})
    assert response.status_code == 201, response.text[:500]
    found = [row for row in scratch.api.items(scratch.trackable_part, scratch.parent) if row.get("serial") in serials]
    assert sorted(row["serial"] for row in found) == sorted(serials), found
    return sorted(found, key=lambda row: str(row["serial"]))


def serial_movement(
    scratch: Scratch, movement_type: MovementType, pks: tuple[int, ...], serials: tuple[str, ...] = ()
) -> MovementView:
    """`movement_type` of the serialized items `pks` (1 each), naming `serials` on the line."""
    acts_at_source = movement_type in (MovementType.TAKE_TO_VEHICLE, MovementType.CONSUME)
    place = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent)
    other = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other)
    built = view(
        movement_type,
        lines=(("ONT", str(len(pks))),),
        stock=(picked(*((pk, "1") for pk in pks)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.trackable_part),),
        source=place if acts_at_source else None,
        destination=None if movement_type is MovementType.CONSUME else (other if acts_at_source else place),
    )
    (line,) = built.movement.line_items
    return replace(built, movement=replace(built.movement, line_items=(replace(line, serials=serials),)))


@pytest.mark.parametrize("named", [False, True])
def test_a_transfer_of_serialized_items_is_verified_and_undone(
    api: RawApi, scratch: Scratch, store: Store, named: bool
) -> None:
    """Every serialized item of the pair, or the ones the line names by serial number."""
    items = serialized(scratch, "SN-A1", "SN-A2", "SN-A3")
    taken = items[:2] if named else items
    serials = tuple(str(row["serial"]) for row in taken) if named else ()
    movement = serial_movement(scratch, MovementType.TAKE_TO_VEHICLE, tuple(int(row["pk"]) for row in taken), serials)
    approve(store, movement)
    before = totals(movement)
    done, undone = apply_then_undo(store, movement)
    assert (done.state, done.problems) == (S.VERIFIED, ()), done
    assert undone.state is S.UNDONE, undone
    assert totals(movement) == before
    assert {scratch.item(int(row["pk"]))["location"] for row in items} == {scratch.parent}


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [(MovementType.CONSUME, "1"), (MovementType.COUNT, "0")],
)
def test_a_remove_or_count_of_a_serialized_item_goes_to_review(
    api: RawApi, scratch: Scratch, store: Store, movement_type: MovementType, quantity: str
) -> None:
    """InvenTree would answer it with success and change nothing. Source selection never plans one; a plan whose
    item is serialized now (here: built on it directly) is stopped by the precondition, and nothing is sent."""
    (item,) = serialized(scratch, "SN-B1")
    movement = serial_movement(scratch, movement_type, (int(item["pk"]),))
    if movement_type is MovementType.COUNT:
        (line,) = movement.movement.line_items
        counted = replace(line, quantity=Decimal(quantity))
        movement = replace(
            movement,
            movement=replace(movement.movement, line_items=(counted,)),
            stock=(picked((int(item["pk"]), quantity)),),
        )
    approve(store, movement)
    history = api.get("/api/stock/track/", item=item["pk"])
    before = totals(movement)

    async def once() -> Applied:
        async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(), token()) as writer:
            return await apply_request(
                KEY,
                movement,
                allowed(movement),
                store=store,
                reader=reader,
                writer=writer,
                lock=anyio.Lock(),
                lock_owner=OWNER,
            )

    applied = anyio.run(once)
    assert applied.state is S.PLANNED, applied
    assert applied.why.startswith("back to review: "), applied
    assert totals(movement) == before
    assert api.get("/api/stock/track/", item=item["pk"]) == history
    assert store.recorded_requests(POST)[0].attempts == ()
