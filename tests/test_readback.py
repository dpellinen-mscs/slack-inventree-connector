"""Step 5: the read-back, checks 1 (the marker's entries have the plan's deltas) and 2 (totals are snapshot + plan)."""

from __future__ import annotations

from typing import Any

import pytest
from views import DESTINATION, SOURCE, item, picked, view

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import MovementType
from stockthread.core.resolve import StockItem
from stockthread.core.write_request import WriteRequest
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, MarkedEntry
from stockthread.plan import plan_requests
from stockthread.precondition import Snapshot, check
from stockthread.readback import verify
from stockthread.slack.replies import MovementView

MARKER = Marker(new_marker_id(), AttemptKind.APPLY, 1)
NEW = 90
"""The stock item a partial transfer creates."""
Read = dict[tuple[int, int], list[StockItem]]


class Applied:
    """A movement planned and checked as the apply protocol does: its request with MARKER, and its snapshot."""

    def __init__(self, planned: MovementView, before: Read) -> None:
        self.view = planned
        (self.request,) = plan_requests([planned], [MARKER], user="U0SAM0001")
        snapshot = check(planned, self.request, before)
        assert isinstance(snapshot, Snapshot)
        self.snapshot = snapshot

    def verify(self, entries: list[MarkedEntry], now: Read, gone: tuple[int, ...] = (), **given: Any) -> list[str]:
        version = given.pop("api_version", DEPLETING_MARKER_FOUND_FROM)
        return list(
            verify(
                self.view,
                self.request,
                MARKER,
                self.snapshot,
                entries,
                now,
                gone=gone,
                api_version=version,
                **given,
            )
        )


def entry(pk: int, on: int | None, kind: int, *, marker: Marker = MARKER, **deltas: Any) -> MarkedEntry:
    return MarkedEntry(pk, on, marker, deltas, kind)


def transfer(quantity: str = "2") -> Applied:
    """Item 57 holds 5 at the source (with item 58, 1); item 80 holds 3 at the destination."""
    before = {(1, SOURCE): [item(57, "5"), item(58, "1")], (1, DESTINATION): [item(80, "3")]}
    return Applied(view(lines=(("Disc 40", quantity),), stock=(picked((57, quantity)),)), before)


def split_entries(removed: float = 2.0, left: float = 3.0) -> list[MarkedEntry]:
    """As InvenTree writes them (`test_char_history_deltas`): quantities as JSON numbers, details beside."""
    return [
        entry(700, 57, 42, removed=removed, quantity=left, stockitem=NEW, stockitem_detail={"pk": NEW}),
        entry(701, NEW, 40, stockitem=57, quantity=removed, stockitem_detail={"pk": 57}),
    ]


AFTER_SPLIT: Read = {
    (1, SOURCE): [item(57, "3"), item(58, "1")],
    (1, DESTINATION): [item(80, "3"), item(NEW, "2")],
}


def test_a_partial_transfer_that_did_what_the_plan_says_has_no_problem() -> None:
    assert transfer().verify(split_entries(), AFTER_SPLIT) == []


def test_a_whole_transfer_is_one_location_entry_on_the_item() -> None:
    applied = transfer("5")
    now = {(1, SOURCE): [item(58, "1")], (1, DESTINATION): [item(80, "3"), item(57, "5")]}
    assert applied.verify([entry(700, 57, 20, location=DESTINATION, quantity=5.0)], now) == []
    assert applied.verify([entry(700, 57, 20, location=DESTINATION + 1, quantity=5.0)], now) == [
        "stock item 57: history entry 700 type 20, the plan says type 20 (location 11, quantity 5)"
    ]


def test_a_short_move_fails_both_checks() -> None:
    """Another writer took 4 of 5 between the snapshot and the write: InvenTree moved the 1 left."""
    applied = transfer()
    now = {(1, SOURCE): [item(58, "1")], (1, DESTINATION): [item(80, "3"), item(57, "1")]}
    assert applied.verify([entry(700, 57, 20, location=DESTINATION, quantity=1.0)], now) == [
        "stock item 57: history entry 700 type 20, the plan says type 42 (removed 2, quantity 3)",
        "part 1 at location 10 holds 1, the plan says 4",
        "part 1 at location 11 holds 4, the plan says 5",
    ]


@pytest.mark.parametrize(
    ("entries", "problem"),
    [
        ([], "stock item 57: history none, the plan says type 42 (removed 2, quantity 3)"),
        (split_entries()[:1], "stock item 90: history none, the plan says type 40 (stockitem 57, quantity 2)"),
        (split_entries(removed=2.5), "stock item 57: history entry 700 type 42, the plan says type 42"),
        (
            [*split_entries(), entry(702, 58, 12, removed=1.0, quantity=0.0)],
            "history entries 702 on stock item 58, which the request does not write",
        ),
        (
            [*split_entries(), entry(702, 57, 42, removed=2.0, quantity=3.0, stockitem=NEW)],
            "stock item 57: history entry 700 type 42, entry 702 type 42",
        ),
        (
            [*split_entries(), entry(702, 57, 12, marker=Marker(MARKER.marker_id, AttemptKind.APPLY, 2), removed=1.0)],
            "history entries 702 carry another attempt of this request",
        ),
        (
            [entry(700, 57, 42, removed=2.0, quantity=3.0, stockitem=57)],
            "the split entry 700 of stock item 57 names no new stock item",
        ),
        (
            [entry(700, 57, 42, removed=True, quantity=3.0, stockitem=NEW), split_entries()[1]],
            "stock item 57: history entry 700 type 42",
        ),
    ],
    ids=["none", "no-child-entry", "other-delta", "foreign-item", "twice", "other-attempt", "no-child", "bool"],
)
def test_entries_that_are_not_the_plans_are_problems(entries: list[MarkedEntry], problem: str) -> None:
    problems = transfer().verify(entries, AFTER_SPLIT)
    assert any(found.startswith(problem) for found in problems), problems


@pytest.mark.parametrize(
    ("movement_type", "deltas", "after"),
    [
        (MovementType.RECEIVE, {"added": 2.0, "quantity": 7.0}, "7"),
        (MovementType.CONSUME, {"removed": 2.0, "quantity": 3.0}, "3"),
        (MovementType.COUNT, {"quantity": 2.0}, "2"),
    ],
)
def test_an_add_a_remove_and_a_count_are_one_entry_each(
    movement_type: MovementType, deltas: dict[str, float], after: str
) -> None:
    """A remove takes from item 57 beside item 58; an add or a count acts on 57, the one item there."""
    place = SOURCE if movement_type is MovementType.CONSUME else DESTINATION
    beside = [item(58, "1")] if movement_type is MovementType.CONSUME else []
    applied = Applied(view(movement_type), {(1, place): [item(57, "5"), *beside]})
    kind = {MovementType.RECEIVE: 11, MovementType.CONSUME: 12, MovementType.COUNT: 10}[movement_type]
    assert applied.verify([MarkedEntry(700, 57, MARKER, deltas, kind)], {(1, place): [item(57, after), *beside]}) == []
    unchanged = {(1, place): [item(57, "5"), *beside]}
    total = 5 + len(beside)
    assert applied.verify([MarkedEntry(700, 57, MARKER, deltas, kind)], unchanged) == [
        f"part 1 at location {place} holds {total}, the plan says {int(after) + len(beside)}"
    ]


def consume_all() -> Applied:
    return Applied(
        view(MovementType.CONSUME, stock=(picked((57, "5")),), lines=(("Disc", "5"),)), {(1, SOURCE): [item(57, "5")]}
    )


def test_a_removal_that_deleted_its_item_leaves_an_entry_of_no_item() -> None:
    applied, now = consume_all(), Read({(1, SOURCE): []})
    deleted = entry(700, None, 12, removed=5.0, quantity=0.0)
    assert applied.verify([deleted], now, gone=(57,)) == []
    assert applied.verify([], now, gone=(57,)) == [
        "no history entry type 12 (removed 5, quantity 0) of a deleted stock item"
    ]
    assert applied.verify([entry(700, 57, 12, removed=5.0, quantity=0.0)], now, gone=(57,))[0].startswith(
        "stock item 57 is gone, yet history entries 700 name it"
    )


def test_below_the_version_that_finds_it_the_entry_of_a_deleted_item_may_be_missing() -> None:
    applied, now = consume_all(), Read({(1, SOURCE): []})
    older = DEPLETING_MARKER_FOUND_FROM - 1
    assert applied.verify([], now, gone=(57,), api_version=older) == []
    assert applied.verify([], now, api_version=older) == [
        "stock item 57: history none, the plan says type 12 (removed 5, quantity 0)"
    ]  # not gone: the item kept its entry, which is found


def test_only_a_removal_of_all_an_item_held_may_leave_it_gone() -> None:
    problems = transfer().verify(split_entries(), AFTER_SPLIT, gone=(57,))
    assert problems[0] == "stock item 57 is gone, and only a removal of all it held or a count to 0 may delete it"


def test_inputs_that_do_not_belong_together_are_a_caller_error() -> None:
    applied = transfer()
    other = WriteRequest(applied.request.operation, applied.request.path, {**applied.request.body, "notes": "x"})
    with pytest.raises(ValueError, match="this movement and marker"):
        verify(applied.view, other, MARKER, applied.snapshot, [], AFTER_SPLIT, gone=(), api_version=530)
    with pytest.raises(ValueError, match="snapshot is not of this request"):
        verify(applied.view, applied.request, MARKER, Snapshot({}, {}), [], AFTER_SPLIT, gone=(), api_version=530)
    with pytest.raises(ValueError, match="was not read"):
        applied.verify(split_entries(), {(1, SOURCE): []})
    with pytest.raises(ValueError, match="gone names"):
        applied.verify(split_entries(), AFTER_SPLIT, gone=(58,))


def test_a_count_to_zero_that_deleted_its_item_leaves_an_entry_of_no_item() -> None:
    applied = Applied(
        view(MovementType.COUNT, stock=(picked((57, "0")),), lines=(("Disc", "0"),)),
        {(1, DESTINATION): [item(57, "5")]},
    )
    now = Read({(1, DESTINATION): []})
    assert applied.verify([entry(700, None, 10, quantity=0.0)], now, gone=(57,)) == []
    assert applied.verify([entry(700, None, 12, removed=5.0, quantity=0.0)], now, gone=(57,))[0].startswith(
        "no history entry type 10 (quantity 0) of a deleted stock item"
    )
    assert applied.verify([], now, gone=(57,), api_version=DEPLETING_MARKER_FOUND_FROM - 1) == []
