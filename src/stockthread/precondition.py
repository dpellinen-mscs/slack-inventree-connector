"""Step 3 of the apply protocol, the snapshot and the precondition (design section 7, "Apply protocol").

Pure: the caller reads the stock items of every (part, location) pair the movement touches (`pairs`), right before
the write, and this module says whether the plan still holds against them. If it does, the result is the snapshot
stored with the attempt (`Store.begin_attempt`): what each stock item the request names held, and the total held at
each pair. If it does not, the result says why, and nothing may be written: the request goes back to `planned` and
the post to review.

The plan holds when every stock item a line names is still where the plan found it (with a serial number the line
names, when it names any), and

- for a transfer or a remove: still available (status OK, nothing allocated) and holding at least what the lines
  of the movement take from it;
- for an add or a count: still there and not serialized (`core.resolve.target_item`).

The snapshot is read again after the write: the read-back compares the totals with the snapshot plus the plan, and a
missing marker of a removal is excused only when the snapshot shows the removal taking the whole of each item
(`apply.depletes`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockthread.core.model import Match, Operation, operation_for
from stockthread.core.resolve import StockItem
from stockthread.core.write_request import _decimal
from stockthread.slack.replies import MovementView

Pair = tuple[int, int]
"""(part, location)."""


@dataclass(frozen=True)
class Snapshot:
    stock_items: Mapping[int, Decimal]
    """What each stock item the request names held, by pk."""
    totals: Mapping[Pair, Decimal]
    """What each (part, location) pair the movement touches held, its stock items added up."""

    def as_json(self) -> dict[str, Any]:
        """The stored form: `{"stock_items": {"<pk>": "<quantity>"}, "totals": {"<part>@<location>": "<quantity>"}}`,
        quantities as decimal strings (`apply.depletes` reads `stock_items`)."""
        return {
            "stock_items": {str(pk): _decimal(quantity) for pk, quantity in sorted(self.stock_items.items())},
            "totals": {f"{part}@{place}": _decimal(total) for (part, place), total in sorted(self.totals.items())},
        }


@dataclass(frozen=True)
class Stale:
    why: str
    """One line for the trail and the log: which line, which stock item, what changed. No message text."""


def pairs(view: MovementView) -> list[Pair]:
    """The (part, location) pairs to read for `view`: each line's part where the movement acts (its source for a
    transfer or a remove, its destination for an add or a count), and at a transfer's destination too."""
    _, place, destination = _places(view)
    found: list[Pair] = []
    for match in view.items:
        part = _record(match)
        if part is None:
            raise ValueError("every line of a decided movement has its part")
        for at in (place, destination):
            if at is not None and (part, at) not in found:
                found.append((part, at))
    return found


def check(view: MovementView, read: Mapping[Pair, Sequence[StockItem]]) -> Snapshot | Stale:
    """The snapshot of `view`'s decided plan against `read` (the stock items of every pair of `pairs(view)`, read
    now), or why the plan no longer holds."""
    operation, place, _ = _places(view)
    if len(view.stock) != len(view.items) or not view.items:
        raise ValueError("a decided movement has the stock of every line")
    totals: dict[Pair, Decimal] = {}
    for pair in pairs(view):
        if pair not in read:
            raise ValueError(f"the stock of part {pair[0]} at location {pair[1]} was not read")
        totals[pair] = sum((item.quantity for item in read[pair]), Decimal(0))
    taken: dict[int, Decimal] = {}
    held: dict[int, StockItem] = {}
    for line, (match, pick) in enumerate(zip(view.items, view.stock, strict=True), start=1):
        part = _record(match)
        if part is None or pick is None or not pick.decided:
            raise ValueError("a decided movement has the part and the stock of every line")
        now = {item.pk: item for item in read[(part, place)]}
        for take in pick.takes:
            item = now.get(take.item)
            if item is None:
                return Stale(f"line {line}: stock item {take.item} is no longer at location {place}")
            serials = view.movement.line_items[line - 1].serials
            if serials and item.serial not in serials:
                return Stale(f"line {line}: stock item {take.item} no longer has a serial number the line names")
            held[item.pk] = item
            taken[item.pk] = taken.get(item.pk, Decimal(0)) + take.quantity
    for pk, quantity in taken.items():
        item = held[pk]
        if operation in (Operation.TRANSFER, Operation.REMOVE):
            if not item.available:
                return Stale(f"stock item {pk} is not available now (status, allocation or empty)")
            if item.quantity < quantity:
                return Stale(f"stock item {pk} holds {_decimal(item.quantity)} now, less than {_decimal(quantity)}")
        elif item.serial:
            return Stale(f"stock item {pk} is serialized now")
    return Snapshot({pk: item.quantity for pk, item in held.items()}, totals)


def _places(view: MovementView) -> tuple[Operation, int, int | None]:
    """The operation, where it acts, and a transfer's destination (None for the other operations)."""
    operation = operation_for(view.movement.movement_type)
    if operation is None:
        raise ValueError("the movement type becomes no operation")
    source, destination = _record(view.source), _record(view.destination)
    if operation in (Operation.TRANSFER, Operation.REMOVE):
        if source is None or (operation is Operation.TRANSFER and destination is None):
            raise ValueError(f"a {operation.value} needs its places")
        return operation, source, destination if operation is Operation.TRANSFER else None
    if destination is None:
        raise ValueError(f"a {operation.value} needs the place it acts at")
    return operation, destination, None


def _record(match: Match | None) -> int | None:
    return match.record if match is not None and match.can_authorize_a_write else None
