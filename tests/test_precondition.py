"""Step 3: the plan still holds against the stock read right before the write, and the snapshot stored with it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from stockthread.apply import depletes
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType
from stockthread.core.resolve import SourcePick, StockItem, Take
from stockthread.plan import plan_requests
from stockthread.precondition import Snapshot, Stale, pairs
from stockthread.precondition import check as check_with
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


def check(view: MovementView, read: Any, **given: Any) -> Snapshot | Stale:
    """`precondition.check` with the request `plan_requests` builds from `view`."""
    (request,) = plan_requests([view], [Marker(new_marker_id(), AttemptKind.APPLY, 1)], user="U0SAM0001")
    return check_with(view, request, read, **given)


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
    assert check(target, {(1, DESTINATION): [item(70, "1", serial="SN-1")]}) == Stale(
        "line 1: the stock at location 11 is no longer one target stock item"
    )
    assert check(target, {(1, DESTINATION): [item(70, "1", serial="SN-1")]}, chosen={0}) == Stale(
        "stock item 70 is serialized now"
    )


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
    (request,) = plan_requests([view()], [Marker(new_marker_id(), AttemptKind.APPLY, 1)], user="U0SAM0001")
    read = {(1, SOURCE): [item(57, "5")], (1, DESTINATION): []}
    with pytest.raises(ValueError, match="part and the stock of every line"):
        check_with(view(stock=(SourcePick(()),)), request, read)
    with pytest.raises(ValueError, match="stock of every line"):
        check_with(view(stock=()), request, read)
    with pytest.raises(ValueError, match="not built from this movement"):
        check_with(view(lines=(("Disc 40", "1"),), stock=(picked((57, "1")),)), request, read)
    with pytest.raises(ValueError, match="not built from this movement"):
        check_with(view(MovementType.CONSUME), request, read)
    with pytest.raises(ValueError, match="the place it takes from"):
        check_with(view(destination=Match(MatchStatus.MATCHED, MatchRule.EXACT, SOURCE)), request, read)


@pytest.mark.parametrize("movement_type", [MovementType.RECEIVE, MovementType.COUNT])
def test_an_add_or_a_count_with_a_second_item_there_now_is_stale_unless_a_person_chose(
    movement_type: MovementType,
) -> None:
    """A count of 5 into item 70 while item 71 arrived: the place would hold 9 where the person counted 5."""
    target = view(movement_type, lines=(("Disc 40", "5"),), stock=(picked((70, "5")),))
    read = {(1, DESTINATION): [item(70, "3"), item(71, "4")]}
    assert check(target, read) == Stale("line 1: the stock at location 11 is no longer one target stock item")
    assert isinstance(check(target, read, chosen={0}), Snapshot)


def test_a_serialized_item_is_never_removed_and_moved_whole_only() -> None:
    read = {(1, SOURCE): [item(57, "1", serial="SN-1")], (1, DESTINATION): []}
    assert check(view(MovementType.CONSUME, stock=(picked((57, "1")),), lines=(("ONT", "1"),)), read) == Stale(
        "stock item 57 is serialized now, and is moved whole or not at all"
    )
    assert isinstance(check(view(stock=(picked((57, "1")),), lines=(("ONT", "1"),)), read), Snapshot)


def test_serialized_stock_taken_with_no_serial_named_must_still_be_all_of_it() -> None:
    planned = view(stock=(picked((57, "1"), (58, "1")),), lines=(("UPS", "2"),))
    a, b = item(57, "1", serial="A"), item(58, "1", serial="B")
    assert isinstance(check(planned, {(1, SOURCE): [a, b], (1, DESTINATION): []}), Snapshot)
    arrived = {(1, SOURCE): [a, b, item(59, "1", serial="C")], (1, DESTINATION): []}
    assert check(planned, arrived) == Stale(
        "line 1: the serialized stock there changed, and the line names no serial numbers"
    )
    assert isinstance(check(planned, arrived, chosen={0}), Snapshot)
    mixed = {(1, SOURCE): [a, b, item(60, "4")], (1, DESTINATION): []}
    assert check(planned, mixed) == Stale("line 1: serialized and unserialized stock is mixed there now")


def test_serialized_stock_arriving_beside_an_unserialized_pick_makes_the_source_mixed() -> None:
    """The plan takes unserialized item 57; serialized item 58 arrives before the write. `pick_source` would now
    answer DETAIL_MIXED, and a choice does not settle that (`_chosen`), so the plan is stale (review of #113)."""
    planned = view(stock=(picked((57, "2")),))
    arrived = {(1, SOURCE): [item(57, "5"), item(58, "1", serial="SN-1")], (1, DESTINATION): []}
    assert check(planned, arrived) == Stale("line 1: serialized and unserialized stock is mixed there now")
    assert check(planned, arrived, chosen={0}) == Stale("line 1: serialized and unserialized stock is mixed there now")
    consume = view(MovementType.CONSUME, stock=(picked((57, "2")),))
    assert check(consume, arrived) == Stale("line 1: serialized and unserialized stock is mixed there now")
    unavailable = {(1, SOURCE): [item(57, "5"), item(58, "1", status=65, serial="SN-1")], (1, DESTINATION): []}
    assert isinstance(check(planned, unavailable), Snapshot)


def test_a_line_with_no_serial_named_takes_what_the_lines_before_it_left() -> None:
    """ "1 ONT SN A" and "1 ONT": the resolver gives line 1 item 57 (A) and line 2 the one left, 58, and nothing
    changed, so the plan holds (review of #113). A third serialized item arriving still makes line 2 stale."""
    lines = (("ONT", "1"), ("ONT", "1"))
    same_part = tuple(Match(MatchStatus.MATCHED, MatchRule.EXACT, 1) for _ in lines)
    planned = view(stock=(picked((57, "1")), picked((58, "1"))), lines=lines, items=same_part)
    first = replace(planned.movement.line_items[0], serials=("A",))
    planned = replace(planned, movement=replace(planned.movement, line_items=(first, planned.movement.line_items[1])))
    a, b = item(57, "1", serial="A"), item(58, "1", serial="B")
    assert isinstance(check(planned, {(1, SOURCE): [a, b], (1, DESTINATION): []}), Snapshot)
    by_person = replace(planned, movement=replace(planned.movement, line_items=lines_without_serials(planned)))
    assert isinstance(check(by_person, {(1, SOURCE): [a, b], (1, DESTINATION): []}, chosen={0}), Snapshot)
    arrived = {(1, SOURCE): [a, b, item(59, "1", serial="C")], (1, DESTINATION): []}
    assert check(planned, arrived) == Stale(
        "line 2: the serialized stock there changed, and the line names no serial numbers"
    )


def lines_without_serials(planned: MovementView) -> tuple[LineItem, ...]:
    return tuple(replace(line, serials=()) for line in planned.movement.line_items)


def test_a_request_to_another_destination_or_with_a_location_it_does_not_take_is_a_caller_error() -> None:
    read = {(1, SOURCE): [item(57, "5")], (1, DESTINATION): []}
    (request,) = plan_requests([view()], [Marker(new_marker_id(), AttemptKind.APPLY, 1)], user="U0SAM0001")
    elsewhere = replace(request, body={**request.body, "location": DESTINATION + 1})
    with pytest.raises(ValueError, match="not built from this movement"):
        check_with(view(), elsewhere, read)
    consume = view(MovementType.CONSUME)
    (removal,) = plan_requests([consume], [Marker(new_marker_id(), AttemptKind.APPLY, 1)], user="U0SAM0001")
    assert isinstance(check_with(consume, removal, read), Snapshot)
    with pytest.raises(ValueError, match="not built from this movement"):
        check_with(consume, replace(removal, body={**removal.body, "location": SOURCE}), read)
