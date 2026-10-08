"""The undo of a write, built from what the apply's history entries show (design section 7, "Undo")."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from views import DESTINATION, SOURCE, item, picked, view

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import MovementType, Operation
from stockthread.core.resolve import StockItem
from stockthread.inventree import MarkedEntry
from stockthread.inverse import (
    NoUndo,
    Reversal,
    UndoSnapshot,
    check_undo,
    reversals,
    undo_changes,
    undo_request,
    undo_snapshot,
    verify_undo,
)
from stockthread.plan import plan_requests
from stockthread.precondition import Snapshot, check
from stockthread.slack.replies import MovementView

ID = new_marker_id()
APPLY = Marker(ID, AttemptKind.APPLY, 1)
UNDO = Marker(ID, AttemptKind.UNDO, 1)
NEW = 90
"""The stock item a partial transfer creates."""
Read = dict[tuple[int, int], list[StockItem]]


class Applied:
    """A movement planned and checked as the apply protocol does: its request with APPLY, and its snapshot. Each
    stock item's history before the write is one entry, pk ten times the item's."""

    def __init__(self, planned: MovementView, before: Read) -> None:
        self.view = planned
        (self.request,) = plan_requests([planned], [APPLY], user="U0SAM0001")
        history = {stock.pk: [stock.pk * 10] for items in before.values() for stock in items}
        snapshot = check(planned, self.request, before, history=history)
        assert isinstance(snapshot, Snapshot)
        self.snapshot = snapshot

    def reversals(self, entries: list[MarkedEntry]) -> list[Reversal] | NoUndo:
        return reversals(self.view, self.request, APPLY, self.snapshot, entries)

    def check(self, found: list[Reversal], now: Read, entries: list[MarkedEntry], **later: list[int]) -> NoUndo | None:
        """`check_undo` with each reversed item's history: its old entry, the apply's entries on it, and `later`."""
        history = {
            pk: [*([pk * 10] if pk != NEW else []), *(e.pk for e in entries if e.item == pk)]
            for r in found
            for pk in r.history_items
        }
        for pk, extra in later.items():
            history[int(pk.removeprefix("on_"))] += extra
        return check_undo(self.view, found, APPLY, self.snapshot, now, entries=entries, history=history)


def entry(pk: int, on: int | None, kind: int, *, marker: Marker = APPLY, **deltas: Any) -> MarkedEntry:
    return MarkedEntry(pk, on, marker, deltas, kind)


def transfer(quantity: str) -> Applied:
    """Item 57 holds 5 at the source (with item 58, 1); item 80 holds 3 at the destination."""
    before = {(1, SOURCE): [item(57, "5"), item(58, "1")], (1, DESTINATION): [item(80, "3")]}
    return Applied(view(lines=(("Disc 40", quantity),), stock=(picked((57, quantity)),)), before)


SPLIT = [
    entry(700, 57, 42, removed=2.0, quantity=3.0, stockitem=NEW),
    entry(701, NEW, 40, stockitem=57, quantity=2.0),
]
AFTER_SPLIT: Read = {(1, DESTINATION): [item(80, "3"), item(NEW, "2")]}
WHOLE = [entry(700, 57, 20, location=DESTINATION, quantity=5.0)]
AFTER_WHOLE: Read = {(1, DESTINATION): [item(57, "5"), item(80, "3")]}


def test_a_partial_transfer_is_undone_by_moving_the_new_item_back_to_the_source() -> None:
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert found == [Reversal(NEW, Decimal(2), Decimal(2), split_from=57, split_entry=700)]
    assert applied.check(found, AFTER_SPLIT, SPLIT) is None
    request = undo_request(applied.view, found, UNDO, user="U0DAN0001")
    assert request.operation is Operation.TRANSFER
    assert request.body == {
        "items": [{"pk": NEW, "quantity": "2", "merge": False}],
        "notes": f"{UNDO} U0DAN0001",
        "location": SOURCE,
    }


def test_a_whole_transfer_is_undone_by_moving_the_item_back() -> None:
    applied = transfer("5")
    found = applied.reversals(WHOLE)
    assert found == [Reversal(57, Decimal(5), Decimal(5))]
    assert applied.check(found, AFTER_WHOLE, WHOLE) is None
    assert applied.check(found, {(1, DESTINATION): [item(57, "5", allocated="1")]}, WHOLE) == NoUndo(
        "stock item 57 is not available (status 10, allocated 1)"
    )
    request = undo_request(applied.view, found, UNDO, user="U0DAN0001")
    assert request.body["items"] == [{"pk": 57, "quantity": "5", "merge": False}]
    assert request.body["location"] == SOURCE


@pytest.mark.parametrize(
    ("movement_type", "kind", "deltas", "undo", "quantity", "after"),
    [
        (MovementType.RECEIVE, 11, {"added": 2.0, "quantity": 7.0}, Operation.REMOVE, "2", "7"),
        (MovementType.CONSUME, 12, {"removed": 2.0, "quantity": 3.0}, Operation.ADD, "2", "3"),
        (MovementType.COUNT, 10, {"quantity": 2.0}, Operation.COUNT, "5", "2"),
    ],
)
def test_an_add_a_remove_and_a_count_are_undone_on_their_item(
    movement_type: MovementType, kind: int, deltas: dict[str, Any], undo: Operation, quantity: str, after: str
) -> None:
    """Item 57 held 5: the add is taken out again, the removal added back, the count set back to 5."""
    place = SOURCE if movement_type is MovementType.CONSUME else DESTINATION
    applied = Applied(view(movement_type), {(1, place): [item(57, "5")]})
    entries = [entry(700, 57, kind, **deltas)]
    found = applied.reversals(entries)
    assert isinstance(found, list)
    assert applied.check(found, {(1, place): [item(57, after)]}, entries) is None
    request = undo_request(applied.view, found, UNDO, user="U0DAN0001")
    assert request.operation is undo
    assert request.body == {"items": [{"pk": 57, "quantity": quantity}], "notes": f"{UNDO} U0DAN0001"}


def test_an_undo_reverses_what_the_history_shows_not_what_the_plan_said() -> None:
    """A verify_failed apply that moved 1, not the planned 2: the undo moves back the 1 that moved."""
    applied = transfer("2")
    short = [entry(700, 57, 42, removed=1.0, quantity=4.0, stockitem=NEW), entry(701, NEW, 40, stockitem=57)]
    assert applied.reversals(short) == [Reversal(NEW, Decimal(1), Decimal(1), split_from=57, split_entry=700)]


@pytest.mark.parametrize(
    ("entries", "why"),
    [
        ([], "stock item 57 has 0 history entries of the apply, not 1"),
        ([*SPLIT, entry(702, 57, 42, removed=2.0, stockitem=91)], "stock item 57 has 2 history entries"),
        (
            [entry(700, 57, 42, removed=2.0, stockitem=NEW, marker=Marker(ID, AttemptKind.APPLY, 2))],
            "entries 700 carry",
        ),
        ([*SPLIT, entry(702, NEW, 20, marker=UNDO)], "history entries 702 carry another attempt"),
        ([entry(700, 57, 42, removed=0.000001, stockitem=NEW)], "entry 700 (type 42)"),
        ([entry(700, None, 42, removed=2.0, stockitem=NEW)], "has 0"),
        ([entry(700, 57, 12, removed=2.0)], "history entry 700 (type 12) is not one the undo reverses"),
        ([entry(700, 57, 42, removed=2.0, stockitem="90")], "entry 700 (type 42)"),
        ([entry(700, 57, 42, removed=2.0, stockitem=57)], "entry 700 (type 42)"),
        ([entry(700, 57, 42, removed=0.0, stockitem=NEW)], "entry 700 (type 42)"),
        ([entry(700, 57, 42, removed="lots", stockitem=NEW)], "entry 700 (type 42)"),
        ([entry(700, 57, 20, location=SOURCE, quantity=2.0)], "entry 700 (type 20)"),
        ([entry(700, 57, 20, location=DESTINATION)], "entry 700 (type 20)"),
    ],
)
def test_a_history_that_does_not_show_what_a_line_did_has_no_undo(entries: list[MarkedEntry], why: str) -> None:
    found = transfer("2").reversals(entries)
    assert isinstance(found, NoUndo)
    assert why in found.why


def test_an_item_a_removal_emptied_and_deleted_has_no_undo() -> None:
    applied = Applied(
        view(MovementType.CONSUME, stock=(picked((57, "5")),), lines=(("Disc", "5"),)), {(1, SOURCE): [item(57, "5")]}
    )
    found = applied.reversals([entry(700, None, 12, removed=5.0, quantity=0.0)])
    assert isinstance(found, NoUndo)
    assert "a person recreates it" in found.why


@pytest.mark.parametrize(
    ("movement_type", "kind", "deltas"),
    [
        (MovementType.RECEIVE, 11, {"added": 0.0}),
        (MovementType.RECEIVE, 11, {"added": 2.0}),
        (MovementType.RECEIVE, 11, {"added": 2.0, "quantity": 1.0}),
        (MovementType.RECEIVE, 12, {"removed": 2.0}),
        (MovementType.CONSUME, 12, {"added": 2.0}),
        (MovementType.COUNT, 10, {"quantity": -1.0}),
        (MovementType.COUNT, 10, {"quantity": True}),
        (MovementType.COUNT, 11, {"quantity": 2.0}),
    ],
)
def test_an_entry_of_another_shape_has_no_undo(movement_type: MovementType, kind: int, deltas: dict[str, Any]) -> None:
    place = SOURCE if movement_type is MovementType.CONSUME else DESTINATION
    applied = Applied(view(movement_type), {(1, place): [item(57, "5")]})
    assert isinstance(applied.reversals([entry(700, 57, kind, **deltas)]), NoUndo)


def test_another_writers_entry_since_the_apply_refuses_the_undo() -> None:
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    refused = applied.check(found, AFTER_SPLIT, SPLIT, on_90=[705])
    assert refused == NoUndo("stock item 90 has history entries 705 since the apply that are not this request's")


def test_a_change_to_the_split_item_between_the_snapshot_and_the_apply_refuses_the_undo() -> None:
    """Item 57 moved from the source to another location (entry 650) after the snapshot, then the apply split 2
    from it: the 2 did not come from the source, so sending them there is no undo. Entry 650 is in neither the
    snapshot nor the marker's entries: readback calls this verify_failed, a state that may be undone."""
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    refused = applied.check(found, AFTER_SPLIT, SPLIT, on_57=[650])
    assert refused == NoUndo(
        "stock item 57 has history entries 650 between the snapshot and the apply that are not this request's: "
        "the 2 split from it may not have come from the source"
    )


def test_a_change_to_the_split_item_after_the_apply_does_not_refuse_the_undo() -> None:
    """The undo moves the new item back and leaves item 57 alone: someone taking from 57 since is no reason."""
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    assert applied.check(found, AFTER_SPLIT, SPLIT, on_57=[705]) is None


def test_an_entry_of_another_attempt_found_now_refuses_the_undo() -> None:
    """An apply that wrote late, or an undo that wrote: the stock holds what the undo would not account for."""
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    for other in (Marker(ID, AttemptKind.APPLY, 2), UNDO):
        late = [*SPLIT, entry(705, NEW, 20, marker=other)]
        refused = applied.check(found, AFTER_SPLIT, late, on_90=[705])
        assert refused == NoUndo("history entries 705 carry another attempt of this request than the apply undone")


@pytest.mark.parametrize(
    ("now", "why"),
    [
        ({(1, DESTINATION): [item(80, "3")]}, "stock item 90 is no longer at location"),
        ({(1, SOURCE): [item(NEW, "2")], (1, DESTINATION): [item(80, "3")]}, "no longer at location"),
        ({(1, DESTINATION): [item(NEW, "1.5")]}, "stock item 90 holds 1.5, not the 2 the apply left"),
        ({(1, DESTINATION): [item(NEW, "4")]}, "stock item 90 holds 4, not the 2 the apply left"),
        ({(1, DESTINATION): [item(NEW, "2", allocated="2")]}, "stock item 90 is not available"),
        ({(1, DESTINATION): [item(NEW, "2", status=50)]}, "stock item 90 is not available (status 50"),
        ({(2, DESTINATION): [item(NEW, "2")]}, "no longer at location"),
    ],
)
def test_a_transfer_whose_stock_moved_on_has_no_undo(now: Read, why: str) -> None:
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    refused = applied.check(found, now, SPLIT)
    assert refused is not None
    assert why in refused.why


@pytest.mark.parametrize(
    ("movement_type", "kind", "deltas", "after", "why"),
    [
        (MovementType.RECEIVE, 11, {"added": 2.0, "quantity": 7.0}, "1", "holds 1, not the 7 the apply left"),
        (MovementType.RECEIVE, 11, {"added": 2.0, "quantity": 7.0}, "9", "holds 9, not the 7 the apply left"),
        (MovementType.COUNT, 10, {"quantity": 2.0}, "3", "holds 3, not the 2 the apply left"),
        (MovementType.CONSUME, 12, {"removed": 2.0}, None, "stock item 57 is no longer at location"),
    ],
)
def test_an_add_a_remove_or_a_count_whose_item_moved_on_has_no_undo(
    movement_type: MovementType, kind: int, deltas: dict[str, Any], after: str | None, why: str
) -> None:
    place = SOURCE if movement_type is MovementType.CONSUME else DESTINATION
    applied = Applied(view(movement_type), {(1, place): [item(57, "5")]})
    entries = [entry(700, 57, kind, **deltas)]
    found = applied.reversals(entries)
    assert isinstance(found, list)
    now: Read = {(1, place): [] if after is None else [item(57, after)]}
    refused = applied.check(found, now, entries)
    assert refused is not None
    assert why in refused.why


def test_a_removal_is_added_back_to_an_item_at_zero() -> None:
    """An item a removal emptied that was not deleted is still read (`stock_items` reads empty ones)."""
    applied = Applied(
        view(MovementType.CONSUME, stock=(picked((57, "5")),), lines=(("Disc", "5"),)), {(1, SOURCE): [item(57, "5")]}
    )
    entries = [entry(700, 57, 12, removed=5.0, quantity=0.0)]
    found = applied.reversals(entries)
    assert found == [Reversal(57, Decimal(5), None)]
    assert applied.check(found, {(1, SOURCE): [item(57, "0")]}, entries) is None


def test_inputs_that_do_not_belong_together_are_a_caller_error() -> None:
    applied = transfer("2")
    with pytest.raises(ValueError, match="movement and marker"):
        reversals(applied.view, applied.request, Marker(new_marker_id(), AttemptKind.APPLY, 1), applied.snapshot, SPLIT)
    with pytest.raises(ValueError, match="snapshot"):
        reversals(applied.view, applied.request, APPLY, Snapshot({}, {}), SPLIT)
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    with pytest.raises(ValueError, match="no history read for stock item 90"):
        check_undo(applied.view, found, APPLY, applied.snapshot, AFTER_SPLIT, entries=SPLIT, history={})
    with pytest.raises(ValueError, match="no history read for stock item 57"):
        check_undo(applied.view, found, APPLY, applied.snapshot, AFTER_SPLIT, entries=SPLIT, history={NEW: [701]})
    with pytest.raises(ValueError, match="apply marker"):
        reversals(applied.view, applied.request, UNDO, applied.snapshot, SPLIT)
    with pytest.raises(ValueError, match="undo marker"):
        undo_request(applied.view, found, APPLY, user="U0DAN0001")


@pytest.mark.parametrize(
    ("movement_type", "kind", "deltas"),
    [(MovementType.RECEIVE, 11, {"added": 2.0, "quantity": 2.0}), (MovementType.COUNT, 10, {"quantity": 2.0})],
)
def test_an_item_that_held_nothing_before_is_not_emptied_by_an_undo(
    movement_type: MovementType, kind: int, deltas: dict[str, Any]
) -> None:
    """Back at 0, an item marked delete_on_deplete is deleted, though it existed before the apply."""
    applied = Applied(view(movement_type), {(1, DESTINATION): [item(57, "0")]})
    assert applied.reversals([entry(700, 57, kind, **deltas)]) == NoUndo(
        "stock item 57 held 0 before the apply: the undo would empty it, which may delete it"
    )


BEFORE_UNDO: Read = {(1, SOURCE): [item(57, "3"), item(58, "1")], (1, DESTINATION): [item(80, "3"), item(NEW, "2")]}
AFTER_UNDO: Read = {(1, SOURCE): [item(57, "3"), item(58, "1"), item(NEW, "2")], (1, DESTINATION): [item(80, "3")]}


def split_undone(undo_entry: MarkedEntry, now: Read = AFTER_UNDO) -> list[str]:
    """`verify_undo` of the partial transfer's undo: item 90 (2) moved back from the destination to the source."""
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    history = {57: [570, 700], NEW: [701]}
    before = undo_snapshot(applied.view, found, BEFORE_UNDO, history)
    entries = [*SPLIT, undo_entry]
    later = {NEW: [701, undo_entry.pk]}
    return verify_undo(applied.view, found, UNDO, before, entries, now, history=later)


def test_the_undo_of_a_split_reads_back_as_built() -> None:
    assert split_undone(entry(800, NEW, 20, marker=UNDO, location=SOURCE, quantity=2.0)) == []


@pytest.mark.parametrize(
    ("undo_entry", "why"),
    [
        (entry(800, NEW, 42, marker=UNDO, location=SOURCE, quantity=2.0), "entry 800 on stock item 90 is of type 42"),
        (entry(800, NEW, 20, marker=UNDO, location=DESTINATION, quantity=2.0), "does not show location"),
        (entry(800, NEW, 20, marker=UNDO, location=SOURCE, quantity=1.0), "does not show quantity 2"),
        (entry(800, 57, 20, marker=UNDO, location=SOURCE, quantity=2.0), "stock item 90 has 0 history entries"),
    ],
)
def test_an_undo_entry_of_another_shape_is_a_read_back_problem(undo_entry: MarkedEntry, why: str) -> None:
    assert any(why in problem for problem in split_undone(undo_entry))


def test_an_undo_that_left_the_stock_elsewhere_is_a_read_back_problem() -> None:
    stayed: Read = {(1, SOURCE): [item(57, "3"), item(58, "1")], (1, DESTINATION): [item(80, "3"), item(NEW, "2")]}
    problems = split_undone(entry(800, NEW, 20, marker=UNDO, location=SOURCE, quantity=2.0), stayed)
    assert problems == [
        f"stock item 90 is not at location {SOURCE}",
        f"part 1 at location {SOURCE} holds 4, the undo should leave 6",
        f"part 1 at location {DESTINATION} holds 5, the undo should leave 3",
    ]


def test_the_undo_of_a_count_reads_back_the_count_it_set_back() -> None:
    """Item 57 held 5, the count set 2, the undo counts 5 again."""
    applied = Applied(view(MovementType.COUNT), {(1, DESTINATION): [item(57, "5")]})
    entries = [entry(700, 57, 10, quantity=2.0)]
    found = applied.reversals(entries)
    assert isinstance(found, list)
    before = undo_snapshot(applied.view, found, {(1, DESTINATION): [item(57, "2")]}, {57: [570, 700]})
    counted = [*entries, entry(800, 57, 10, marker=UNDO, quantity=5.0)]
    now: Read = {(1, DESTINATION): [item(57, "5")]}
    assert verify_undo(applied.view, found, UNDO, before, counted, now, history={57: [570, 700, 800]}) == []
    short: Read = {(1, DESTINATION): [item(57, "4")]}
    problems = verify_undo(applied.view, found, UNDO, before, counted, short, history={57: [570, 700, 800]})
    assert "stock item 57 holds 4, the undo should leave 5" in problems
    wrong = [*entries, entry(800, 57, 10, marker=UNDO, quantity=4.0)]
    problems = verify_undo(applied.view, found, UNDO, before, wrong, now, history={57: [570, 700, 800]})
    assert problems == ["the undo's entry 800 on stock item 57 does not show quantity 5"]


def split_snapshot() -> tuple[list[Reversal], UndoSnapshot]:
    applied = transfer("2")
    found = applied.reversals(SPLIT)
    assert isinstance(found, list)
    return found, undo_snapshot(applied.view, found, BEFORE_UNDO, {57: [570, 700], NEW: [701]})


def test_an_undo_snapshot_is_stored_and_read_back_unchanged() -> None:
    _, before = split_snapshot()
    assert UndoSnapshot.from_json(before.as_json()) == before


@pytest.mark.parametrize(
    ("stored", "why"),
    [
        (None, "has totals, where and history"),
        ({"totals": {}, "where": {}}, "has totals, where and history"),
        ({"totals": {}, "where": {}, "history": {}, "stock_items": {}}, "has totals, where and history"),
        ({"totals": [], "where": {}, "history": {}}, "parts are objects"),
        ({"totals": {"1": "2"}, "where": {}, "history": {}}, "not of a part at a location"),
        ({"totals": {"1@10": 2}, "where": {}, "history": {}}, "a decimal string"),
        ({"totals": {"1@10": "-1"}, "where": {}, "history": {}}, "not a quantity"),
        ({"totals": {}, "where": {"57": 11}, "history": {}}, "a stored pair is a string"),
        ({"totals": {}, "where": {"x": "1@11"}, "history": {}}, "not a pk"),
        ({"totals": {}, "where": {}, "history": {"57": ["570"]}}, "a list of entry pks"),
    ],
)
def test_an_undo_snapshot_that_is_not_one_is_refused(stored: Any, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        UndoSnapshot.from_json(stored)


def test_an_undo_that_never_ran_leaves_the_stock_as_its_snapshot() -> None:
    found, before = split_snapshot()
    assert undo_changes(found, before, BEFORE_UNDO, history={NEW: [701]}) == []


def test_any_change_since_the_undo_snapshot_is_reported() -> None:
    found, before = split_snapshot()
    moved: Read = {(1, SOURCE): [item(57, "3"), item(58, "1"), item(NEW, "2")], (1, DESTINATION): [item(80, "3")]}
    assert undo_changes(found, before, moved, history={NEW: [701, 800]}) == [
        f"stock item 90 is no longer at location {DESTINATION}",
        f"part 1 at location {SOURCE} holds 6, the undo's snapshot says 4",
        f"part 1 at location {DESTINATION} holds 3, the undo's snapshot says 5",
        "stock item 90 has history entries 800 the undo's snapshot lacks",
    ]
    short: Read = {(1, SOURCE): [item(57, "3"), item(58, "1")], (1, DESTINATION): [item(80, "3"), item(NEW, "1")]}
    assert undo_changes(found, before, short, history={NEW: [701]}) == [
        "stock item 90 holds 1, not the 2 the apply left",
        f"part 1 at location {DESTINATION} holds 4, the undo's snapshot says 5",
    ]


def test_a_change_to_the_split_parent_alone_is_not_the_undo_running() -> None:
    """The undo never writes on the item a split took from: its later entries say nothing about the undo."""
    found, before = split_snapshot()
    assert undo_changes(found, before, BEFORE_UNDO, history={NEW: [701], 57: [570, 700, 900]}) == []
