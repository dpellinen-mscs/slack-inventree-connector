"""A decided plan becomes its requests, one per movement in the post's order; anything less is refused whole."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import (
    CandidateMovement,
    LineItem,
    Match,
    MatchRule,
    MatchStatus,
    MovementType,
    Operation,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.resolve import LineStock, SourcePick, StockItem, Take, resolve_lines
from stockthread.plan import NotDecided, plan_requests
from stockthread.slack.replies import MovementView

SAM = "U0SAM0001"
LINK = "https://acme-fiber.slack.com/archives/C0ACME0001/p1700000000000100"


def matched(pk: int, rule: MatchRule = MatchRule.EXACT) -> Match:
    return Match(MatchStatus.MATCHED, rule, pk)


def picked(*pairs: tuple[int, str]) -> SourcePick:
    return SourcePick(tuple(Take(item, Decimal(quantity)) for item, quantity in pairs))


def view(
    movement_type: MovementType = MovementType.TAKE_TO_VEHICLE,
    lines: tuple[tuple[str, str], ...] = (("Disc 40", "2"),),
    stock: tuple[SourcePick | None, ...] = (picked((57, "2")),),
    **fields: Any,
) -> MovementView:
    movement = CandidateMovement(
        movement_type,
        tuple(LineItem(text, Decimal(quantity)) for text, quantity in lines),
        None if movement_type in (MovementType.RECEIVE, MovementType.COUNT) else "main stock",
        None if movement_type is MovementType.CONSUME else "my truck",
    )
    values: dict[str, Any] = {
        "movement": movement,
        "items": tuple(matched(1 + n) for n in range(len(lines))),
        "source": None if movement.source_text is None else matched(10),
        "destination": None if movement.destination_text is None else matched(11),
        "reasons": (),
        "stock": stock,
    }
    values.update(fields)
    return MovementView(**values)


def markers(count: int) -> list[Marker]:
    return [Marker(new_marker_id(), AttemptKind.APPLY, 1) for _ in range(count)]


def test_each_movement_becomes_one_request_in_the_post_order_with_its_own_marker() -> None:
    views = [
        view(lines=(("Disc 40", "2"), ("UPS", "1.5")), stock=(picked((57, "2")), picked((41, "1"), (58, "0.5")))),
        view(MovementType.CONSUME, stock=(picked((60, "2")),)),
        view(MovementType.RECEIVE, stock=(picked((70, "2")),)),
        view(MovementType.COUNT, stock=(picked((71, "2")),)),
    ]
    given = markers(4)
    requests = plan_requests(views, given, user=SAM, link=LINK)
    assert [request.operation for request in requests] == [
        Operation.TRANSFER,
        Operation.REMOVE,
        Operation.ADD,
        Operation.COUNT,
    ]
    transfer = requests[0].body
    assert transfer["location"] == 11
    assert transfer["items"] == [
        {"pk": 57, "quantity": "2", "merge": False},
        {"pk": 41, "quantity": "1", "merge": False},
        {"pk": 58, "quantity": "0.5", "merge": False},
    ]
    assert requests[1].body["items"] == [{"pk": 60, "quantity": "2"}]
    assert "location" not in requests[1].body
    assert requests[2].body["items"] == [{"pk": 70, "quantity": "2"}]
    assert requests[3].body["items"] == [{"pk": 71, "quantity": "2"}]
    for request, marker in zip(requests, given, strict=True):
        assert request.body["notes"] == f"{marker} {SAM} {LINK}"


def test_lines_of_one_movement_on_the_same_stock_item_are_one_line_of_its_request() -> None:
    two = view(lines=(("Disc 40", "2"), ("Disc 40", "3")), stock=(picked((57, "2")), picked((57, "3"))))
    (request,) = plan_requests([two], markers(1), user=SAM)
    assert request.body["items"] == [{"pk": 57, "quantity": "5", "merge": False}]


def test_a_count_of_one_stock_item_by_two_lines_is_refused() -> None:
    two = view(
        MovementType.COUNT, lines=(("Disc 40", "2"), ("Disc 40", "3")), stock=(picked((71, "2")), picked((71, "3")))
    )
    with pytest.raises(NotDecided, match="count the same"):
        plan_requests([two], markers(1), user=SAM)


REASON = ReviewReason(ReasonCode.STOCK_CHOICE, ReasonSource.RULE, "line 1: several stock items differ")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reasons": (REASON,)}, "review reason"),
        ({"items": (Match(MatchStatus.AMBIGUOUS, candidates=(1, 2)),)}, "review reason"),
        ({"items": (matched(1, MatchRule.FUZZY),)}, "review reason"),
        ({"destination": matched(11, MatchRule.WORDS)}, "review reason"),
        ({"destination": matched(10)}, "place it takes from"),
        ({"stock": ()}, "stock of every line"),
        ({"stock": (None,)}, "no decided stock"),
        ({"stock": (SourcePick(()),)}, "no decided stock"),
        ({"stock": (SourcePick((Take(57, Decimal(2)),), (REASON,)),)}, "no decided stock"),
        ({"stock": (picked((57, "1")),)}, "add up"),
        ({"stock": (picked((57, "2"), (58, "1")),)}, "add up"),
    ],
)
def test_a_movement_that_is_not_decided_refuses_the_whole_plan(change: dict[str, Any], message: str) -> None:
    views = [view(), replace(view(), **change)]
    with pytest.raises(NotDecided, match=f"movement 2: .*{message}"):
        plan_requests(views, markers(2), user=SAM)


@pytest.mark.parametrize(("unit", "pack_size"), [("ft", None), ("m", None), ("box", None), (None, Decimal(12))])
def test_a_line_that_is_not_a_count_is_refused_even_if_the_gate_missed_it(
    monkeypatch: pytest.MonkeyPatch, unit: str | None, pack_size: Decimal | None
) -> None:
    """A request carries the bare quantity in the part's stocking unit: "50 ft" must never be sent as 50."""
    monkeypatch.setattr("stockthread.plan.gate_reasons", lambda _view: set())
    given = view()
    line = replace(given.movement.line_items[0], unit=unit, pack_size=pack_size)
    given = replace(given, movement=replace(given.movement, line_items=(line,)))
    with pytest.raises(NotDecided, match="line 1 is not a count"):
        plan_requests([given], markers(1), user=SAM)
    each = replace(given, movement=replace(given.movement, line_items=(replace(line, unit="each", pack_size=None),)))
    assert len(plan_requests([each], markers(1), user=SAM)) == 1


def test_an_unclear_movement_or_an_empty_plan_is_refused() -> None:
    with pytest.raises(NotDecided, match="review reason"):
        plan_requests([view(MovementType.UNCLEAR)], markers(1), user=SAM)
    with pytest.raises(NotDecided, match="at least one"):
        plan_requests([], [], user=SAM)


def test_an_add_split_over_two_stock_items_is_refused() -> None:
    split = view(MovementType.RECEIVE, stock=(picked((70, "1"), (72, "1")),))
    with pytest.raises(NotDecided, match="one stock item"):
        plan_requests([split], markers(1), user=SAM)


def test_markers_must_be_one_apply_marker_per_movement_each_its_own() -> None:
    one = markers(1)[0]
    with pytest.raises(ValueError, match="one marker per movement"):
        plan_requests([view(), view()], [one], user=SAM)
    with pytest.raises(ValueError, match="its own marker id"):
        plan_requests([view(), view()], [one, replace(one, attempt=2)], user=SAM)
    with pytest.raises(ValueError, match="apply markers"):
        plan_requests([view()], [replace(one, kind=AttemptKind.UNDO)], user=SAM)


def test_a_remove_without_its_source_is_refused() -> None:
    with pytest.raises(NotDecided, match="review reason"):
        plan_requests([view(MovementType.CONSUME, source=None)], markers(1), user=SAM)


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (view(MovementType.CONSUME, source=None), "a remove needs its source"),
        (view(source=None), "a transfer needs its source and its destination"),
        (view(destination=None), "a transfer needs its source and its destination"),
        (view(MovementType.RECEIVE, destination=None), "a add needs the place it acts at"),
    ],
)
def test_places_are_checked_again_even_if_the_gate_missed_them(
    monkeypatch: pytest.MonkeyPatch, given: MovementView, message: str
) -> None:
    """plan.py does not rely on gate_reasons alone for the places a write needs."""
    monkeypatch.setattr("stockthread.plan.gate_reasons", lambda _view: set())
    with pytest.raises(NotDecided, match=message):
        plan_requests([given], markers(1), user=SAM)


def test_a_count_to_zero_is_sent_as_zero() -> None:
    (request,) = plan_requests(
        [view(MovementType.COUNT, lines=(("Disc 40", "0"),), stock=(picked((71, "0")),))], markers(1), user=SAM
    )
    assert request.body["items"] == [{"pk": 71, "quantity": "0"}]


def test_a_consume_of_serialized_stock_as_the_rule_resolves_it_is_never_a_request() -> None:
    """InvenTree answers a remove on a serialized item with success and changes nothing (design section 5)."""
    stock = [StockItem(80, Decimal(1), 10, Decimal(0), "SN-1", None, None)]
    (pick,) = resolve_lines([LineStock(Operation.REMOVE, Decimal(1), stock, ("SN-1",))])
    assert pick.takes  # shown in the dry run, with its reason
    with pytest.raises(NotDecided, match="no decided stock"):
        plan_requests([view(MovementType.CONSUME, stock=(pick,))], markers(1), user=SAM)
