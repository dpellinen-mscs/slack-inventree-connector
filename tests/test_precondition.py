"""Step 3: the plan still holds against the stock read right before the write, and the snapshot stored with it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from stockthread.apply import depletes
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType
from stockthread.core.resolve import SourcePick, StockItem, Take
from stockthread.precondition import Snapshot, Stale, check, pairs
from stockthread.slack.replies import MovementView

SOURCE, DESTINATION = 10, 11
"""Where `view` takes from and puts to; its lines are parts 1, 2, ..."""


def picked(*pairs: tuple[int, str]) -> SourcePick:
    return SourcePick(tuple(Take(item, Decimal(quantity)) for item, quantity in pairs))


def view(
    movement_type: MovementType = MovementType.TAKE_TO_VEHICLE,
    lines: tuple[tuple[str, str], ...] = (("Disc 40", "2"),),
    stock: tuple[SourcePick | None, ...] = (picked((57, "2")),),
    **fields: Any,
) -> MovementView:
    """As `tests/test_plan.py` builds it: line n is part n, the source is location 10, the destination 11."""
    movement = CandidateMovement(
        movement_type,
        tuple(LineItem(text, Decimal(quantity)) for text, quantity in lines),
        None if movement_type in (MovementType.RECEIVE, MovementType.COUNT) else "main stock",
        None if movement_type is MovementType.CONSUME else "my truck",
    )
    values: dict[str, Any] = {
        "movement": movement,
        "items": tuple(Match(MatchStatus.MATCHED, MatchRule.EXACT, 1 + n) for n in range(len(lines))),
        "source": None if movement.source_text is None else Match(MatchStatus.MATCHED, MatchRule.EXACT, SOURCE),
        "destination": (
            None if movement.destination_text is None else Match(MatchStatus.MATCHED, MatchRule.EXACT, DESTINATION)
        ),
        "reasons": (),
        "stock": stock,
    }
    values.update(fields)
    return MovementView(**values)


def item(pk: int, quantity: str, status: int = 10, allocated: str = "0", serial: str | None = None) -> StockItem:
    return StockItem(pk, Decimal(quantity), status, Decimal(allocated), serial)


def test_a_transfer_that_still_holds_gives_its_snapshot() -> None:
    read = {(1, SOURCE): [item(57, "5"), item(58, "1")], (1, DESTINATION): [item(80, "3")]}
    snapshot = check(view(), read)
    assert snapshot == Snapshot({57: Decimal(5)}, {(1, SOURCE): Decimal(6), (1, DESTINATION): Decimal(3)})
    assert snapshot.as_json() == {"stock_items": {"57": "5"}, "totals": {"1@10": "6", "1@11": "3"}}


def test_the_stored_snapshot_is_what_the_depletion_excuse_reads() -> None:
    snapshot = check(view(MovementType.CONSUME), {(1, SOURCE): [item(57, "2")]})
    assert isinstance(snapshot, Snapshot)
    assert depletes([(57, Decimal(2))], snapshot.as_json())
    assert not depletes([(57, Decimal(1))], snapshot.as_json())


def test_the_pairs_to_read_are_where_each_operation_acts() -> None:
    two_lines = view(lines=(("Disc 40", "2"), ("UPS", "1")), stock=(picked((57, "2")), picked((60, "1"))))
    assert pairs(two_lines) == [(1, SOURCE), (1, DESTINATION), (2, SOURCE), (2, DESTINATION)]
    assert pairs(view(MovementType.CONSUME)) == [(1, SOURCE)]
    assert pairs(view(MovementType.RECEIVE)) == [(1, DESTINATION)]
    assert pairs(view(MovementType.COUNT)) == [(1, DESTINATION)]


def test_an_item_no_longer_where_the_plan_found_it_is_stale() -> None:
    read = {(1, SOURCE): [item(58, "5")], (1, DESTINATION): [item(57, "2")]}  # moved to the truck already
    assert check(view(), read) == Stale("line 1: stock item 57 is no longer at location 10")


@pytest.mark.parametrize(
    ("now", "why"),
    [
        (item(57, "1"), "stock item 57 holds 1 now, less than 2"),
        (item(57, "5", status=50), "stock item 57 is not available now (status, allocation or empty)"),
        (item(57, "5", allocated="1"), "stock item 57 is not available now (status, allocation or empty)"),
        (item(57, "0"), "stock item 57 is not available now (status, allocation or empty)"),
    ],
)
def test_a_source_that_changed_is_stale(now: StockItem, why: str) -> None:
    for movement_type in (MovementType.TAKE_TO_VEHICLE, MovementType.CONSUME):
        assert check(view(movement_type), {(1, SOURCE): [now], (1, DESTINATION): []}) == Stale(why)


def test_lines_sharing_an_item_must_fit_in_it_together() -> None:
    shared = view(lines=(("Disc 40", "2"), ("Disc 40", "2")), stock=(picked((57, "2")), picked((57, "2"))))
    shared = replace(shared, items=(shared.items[0], shared.items[0]))
    read = {(1, SOURCE): [item(57, "3")], (1, DESTINATION): []}
    assert check(shared, read) == Stale("stock item 57 holds 3 now, less than 4")
    read = {(1, SOURCE): [item(57, "4")], (1, DESTINATION): []}
    assert check(shared, read) == Snapshot({57: Decimal(4)}, {(1, SOURCE): Decimal(4), (1, DESTINATION): Decimal(0)})


@pytest.mark.parametrize("movement_type", [MovementType.RECEIVE, MovementType.COUNT])
def test_an_add_or_a_count_needs_its_target_there_even_empty_and_not_serialized(movement_type: MovementType) -> None:
    target = view(movement_type, stock=(picked((70, "2")),))
    assert check(target, {(1, DESTINATION): [item(70, "0", status=50)]}) == Snapshot(
        {70: Decimal(0)}, {(1, DESTINATION): Decimal(0)}
    )
    assert check(target, {(1, DESTINATION): [item(71, "4")]}) == Stale(
        "line 1: stock item 70 is no longer at location 11"
    )
    assert check(target, {(1, DESTINATION): [item(70, "1", serial="SN-1")]}) == Stale("stock item 70 is serialized now")


def test_an_item_without_a_serial_number_the_line_names_is_stale() -> None:
    serialized = view(stock=(picked((57, "1")),), lines=(("ONT 100", "1"),))
    line = replace(serialized.movement.line_items[0], serials=("SN-7",))
    serialized = replace(serialized, movement=replace(serialized.movement, line_items=(line,)))
    read: dict[tuple[int, int], list[StockItem]] = {(1, DESTINATION): []}
    assert check(serialized, {**read, (1, SOURCE): [item(57, "1", serial="SN-8")]}) == Stale(
        "line 1: stock item 57 no longer has a serial number the line names"
    )
    assert isinstance(check(serialized, {**read, (1, SOURCE): [item(57, "1", serial="SN-7")]}), Snapshot)


def test_a_plan_that_is_not_decided_or_a_pair_not_read_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="was not read"):
        check(view(), {(1, SOURCE): [item(57, "5")]})
    undecided = view(stock=(SourcePick(()),))
    with pytest.raises(ValueError, match="part and the stock of every line"):
        check(undecided, {(1, SOURCE): [], (1, DESTINATION): []})
    with pytest.raises(ValueError, match="stock of every line"):
        check(view(stock=()), {(1, SOURCE): [], (1, DESTINATION): []})
