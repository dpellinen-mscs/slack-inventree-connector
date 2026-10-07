"""Undo against a real server: write a planned request, build its undo from the entries InvenTree wrote
(`inverse`), send it, and find every total the movement touched back where the snapshot had it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from live_server import entries, histories, planned, settings

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import MovementType
from stockthread.inverse import NoUndo, check_undo, reversals, undo_request
from stockthread.plan import plan_requests
from stockthread.precondition import Snapshot, check, pairs
from stockthread.run import inventree_stock

if TYPE_CHECKING:
    from conftest import RawApi, Scratch


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [
        (MovementType.TAKE_TO_VEHICLE, "2.5"),  # a split: the new item goes back
        (MovementType.TAKE_TO_VEHICLE, "5"),  # the whole item goes back
        (MovementType.RECEIVE, "3"),
        (MovementType.CONSUME, "1.25"),
        (MovementType.CONSUME, "5"),  # emptied, not deleted: added back
        (MovementType.COUNT, "7"),
        (MovementType.COUNT, "2.75"),
    ],
)
def test_an_undo_built_from_the_history_puts_every_total_back(
    api: RawApi, scratch: Scratch, movement_type: MovementType, quantity: str
) -> None:
    item = scratch.stock(5, delete_on_deplete=False)  # a removal of all 5 keeps it, so its undo adds back to it
    movement = planned(scratch, movement_type, item["pk"], quantity)
    marker_id = new_marker_id()
    apply_marker = Marker(marker_id, AttemptKind.APPLY, 1)
    (request,) = plan_requests([movement], [apply_marker], user="U0SAM0001")
    read = inventree_stock(settings())
    named = {int(line["pk"]) for line in request.body["items"]}
    before = histories(named)
    snapshot = check(movement, request, read(pairs(movement)), history=before)
    assert isinstance(snapshot, Snapshot), snapshot
    assert api.post(request.path, dict(request.body)).status_code == 201

    found = reversals(movement, request, apply_marker, snapshot, entries(marker_id))
    assert not isinstance(found, NoUndo), found
    touched = {pk for reversal in found for pk in reversal.history_items}
    now = entries(marker_id)
    assert (
        check_undo(
            movement, found, apply_marker, snapshot, read(pairs(movement)), entries=now, history=histories(touched)
        )
        is None
    )
    undo = undo_request(movement, found, Marker(marker_id, AttemptKind.UNDO, 1), user="U0DAN0001")
    response = api.post(undo.path, dict(undo.body))
    assert response.status_code == 201, response.text[:500]

    after = read(pairs(movement))
    for pair, total in snapshot.totals.items():
        assert sum(stock.quantity for stock in after.get(pair, ())) == total, pair


def test_another_writers_change_since_the_apply_refuses_the_undo(api: RawApi, scratch: Scratch) -> None:
    item = scratch.stock(5)
    movement = planned(scratch, MovementType.TAKE_TO_VEHICLE, item["pk"], "2")
    marker_id = new_marker_id()
    apply_marker = Marker(marker_id, AttemptKind.APPLY, 1)
    (request,) = plan_requests([movement], [apply_marker], user="U0SAM0001")
    read = inventree_stock(settings())
    snapshot = check(movement, request, read(pairs(movement)), history=histories({item["pk"]}))
    assert isinstance(snapshot, Snapshot), snapshot
    assert api.post(request.path, dict(request.body)).status_code == 201
    found = reversals(movement, request, apply_marker, snapshot, entries(marker_id))
    assert not isinstance(found, NoUndo), found
    (moved,) = found
    assert api.post("/api/stock/add/", {"items": [{"pk": moved.item, "quantity": 1}], "notes": "x"}).status_code == 201
    marked = entries(marker_id)
    refused = check_undo(
        movement,
        found,
        apply_marker,
        snapshot,
        read(pairs(movement)),
        entries=marked,
        history=histories(set(moved.history_items)),
    )
    assert isinstance(refused, NoUndo)
    assert "since the apply that are not this request's" in refused.why


def test_a_move_of_the_split_item_between_the_snapshot_and_the_apply_refuses_the_undo(
    api: RawApi, scratch: Scratch
) -> None:
    """Another writer moves the item out of the source after the snapshot; the apply then splits 2 from it where it
    now is. Sending those 2 to the source would be no undo."""
    item = scratch.stock(5)
    movement = planned(scratch, MovementType.TAKE_TO_VEHICLE, item["pk"], "2")
    marker_id = new_marker_id()
    apply_marker = Marker(marker_id, AttemptKind.APPLY, 1)
    (request,) = plan_requests([movement], [apply_marker], user="U0SAM0001")
    read = inventree_stock(settings())
    snapshot = check(movement, request, read(pairs(movement)), history=histories({item["pk"]}))
    assert isinstance(snapshot, Snapshot), snapshot
    moved_away = {"items": [{"pk": item["pk"], "quantity": 5}], "location": scratch.child, "notes": "x"}
    assert api.post("/api/stock/transfer/", moved_away).status_code == 201
    assert api.post(request.path, dict(request.body)).status_code == 201
    found = reversals(movement, request, apply_marker, snapshot, entries(marker_id))
    assert not isinstance(found, NoUndo), found
    (split,) = found
    assert split.split_from == item["pk"]
    refused = check_undo(
        movement,
        found,
        apply_marker,
        snapshot,
        read(pairs(movement)),
        entries=entries(marker_id),
        history=histories(set(split.history_items)),
    )
    assert isinstance(refused, NoUndo)
    assert "between the snapshot and the apply" in refused.why
