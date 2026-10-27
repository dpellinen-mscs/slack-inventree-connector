"""Step 3 of the apply protocol, the snapshot and the precondition (design section 7, "Apply protocol").

Pure: the caller reads the stock items of every (part, location) pair the movement touches (`pairs`), with
`InvenTreeReader.stock_items`, right before the write, and this module says whether the plan still holds against
them. If it does, the result is the snapshot stored with the attempt (`Store.begin_attempt`): what each stock item
the request names held, and the total held at each pair. If it does not, the result says why, and nothing may be
written: the request goes back to `planned` and the post to review.

The plan holds when every stock item a line names is still where the plan found it, and

- for a transfer or a remove: the item is still available (status OK, nothing allocated) and holds at least what
  the lines of the movement take from it together. A serialized item is never removed, and is transferred whole
  only (design section 5, "Serialized stock"). A line that names serial numbers takes only items that carry one of
  them; a line that names none and takes serialized items still takes every available serialized item of the pair.
  Whatever a line takes, and whoever chose it, the pair is not mixed serialized and unserialized now
  (`core.resolve.pick_source`, `core.resolve._chosen`);
- for an add or a count: the item is still the one target of the pair, not serialized (`core.resolve.target_item`),
  and an add leaves it under `QUANTITY_LIMIT`: the server checks the line, not the total it makes.

A line a person named the stock items for (`chosen`, design section 4, "The stock question") keeps the person's
choice: its items must still be there and fit as above, but it is not held to the rules a pick without a person
follows (every serialized item, the one target). Not built yet: the comparison with the stock the plan was built on.

The snapshot is read again after the write: the read-back compares the totals with the snapshot plus the plan, so it
must read them with the same reader call, and a missing marker of a removal is excused only when the snapshot shows
the removal taking the whole of each item (`apply.depletes`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from stockthread.core.model import Match, Operation, operation_for
from stockthread.core.resolve import StockItem, target_item
from stockthread.core.validate import counts_pieces, in_stocking_unit
from stockthread.core.write_request import QUANTITY_LIMIT, WriteRequest, _decimal
from stockthread.slack.replies import MovementView

Pair = tuple[int, int]
"""(part, location)."""


@dataclass(frozen=True)
class Snapshot:
    stock_items: Mapping[int, Decimal]
    """What each stock item the request names held, by pk."""
    totals: Mapping[Pair, Decimal]
    """What each (part, location) pair the movement touches held: the quantities of the rows
    `InvenTreeReader.stock_items` gives for it, added up."""
    history: Mapping[int, frozenset[int]] = field(default_factory=dict)
    """The history entry pks of each stock item the request names (`InvenTreeReader.item_entries`): the read-back's
    third check finds the entries another writer added."""

    def as_json(self) -> dict[str, Any]:
        """The stored form: `{"stock_items": {"<pk>": "<quantity>"}, "totals": {"<part>@<location>": "<quantity>"},
        "history": {"<pk>": [<entry pk>, ...]}}`, quantities as decimal strings (`apply.depletes` reads
        `stock_items`)."""
        return {
            "stock_items": {str(pk): _decimal(quantity) for pk, quantity in sorted(self.stock_items.items())},
            "totals": {f"{part}@{place}": _decimal(total) for (part, place), total in sorted(self.totals.items())},
            "history": {str(pk): sorted(entries) for pk, entries in sorted(self.history.items())},
        }

    @classmethod
    def from_json(cls, stored: Any) -> Snapshot:
        """The snapshot `as_json` stored. ValueError for anything else: recovery must not compare with a guess."""
        if not isinstance(stored, dict) or set(stored) != {"stock_items", "totals", "history"}:
            raise ValueError("a stored snapshot has stock_items, totals and history")
        stock, totals, history = stored["stock_items"], stored["totals"], stored["history"]
        if not all(isinstance(part, dict) for part in (stock, totals, history)):
            raise ValueError("a stored snapshot's parts are objects")
        pairs: dict[Pair, Decimal] = {}
        for name, total in totals.items():
            part, at, place = name.partition("@")
            pairs[(_pk(part), _pk(place))] = _quantity(total)
            if not at:
                raise ValueError(f"a stored total is not of a part at a location: {name!r}")
        entries: dict[int, frozenset[int]] = {}
        for pk, found in history.items():
            if not isinstance(found, list) or not all(type(entry) is int for entry in found):
                raise ValueError("a stored history is a list of entry pks")
            entries[_pk(pk)] = frozenset(found)
        return cls({_pk(pk): _quantity(held) for pk, held in stock.items()}, pairs, entries)


@dataclass(frozen=True)
class Stale:
    why: str
    """One line for the trail and the log: which line, which stock item, what changed. No message text."""


def pairs(view: MovementView) -> list[Pair]:
    """The (part, location) pairs to read for `view`: each line's part where the movement acts (its source for a
    transfer or a remove, its destination for an add or a count), and at a transfer's destination too."""
    _, place, destination = places(view)
    found: list[Pair] = []
    for match in view.items:
        part = record(match)
        if part is None:
            raise ValueError("every line of a decided movement has its part")
        for at in (place, destination):
            if at is not None and (part, at) not in found:
                found.append((part, at))
    return found


def check(
    view: MovementView,
    request: WriteRequest,
    read: Mapping[Pair, Sequence[StockItem]],
    *,
    history: Mapping[int, Collection[int]],
    chosen: Collection[int] = (),
    units: Mapping[int, str] | None = None,
) -> Snapshot | Stale:
    """The snapshot of `view`'s decided plan against `read` (the stock items of every pair of `pairs(view)`, read
    now), or why the plan no longer holds. `request` is the request built from `view` (`plan.plan_requests`): its
    lines must be the view's takes, merged by item, or this raises ValueError. `history`: the history entry pks of
    each stock item the request names, read before `read` (an entry written between the two reads is then one the
    read-back reports, never one it misses). `chosen`: the lines (from 0) a person named the stock items for.
    `units`: the units of each part a line carries as a length in its own unit (`MovementView.units`), read now: a
    part stocked in another unit since the plan makes it stale, as its request would write the length as a number
    of that unit."""
    operation, place, _ = places(view)
    if len(view.stock) != len(view.items) or not view.items:
        raise ValueError("a decided movement has the stock of every line")
    totals: dict[Pair, Decimal] = {}
    for pair in pairs(view):
        if pair not in read:
            raise ValueError(f"the stock of part {pair[0]} at location {pair[1]} was not read")
        totals[pair] = sum((item.quantity for item in read[pair]), Decimal(0))
    taken: dict[int, Decimal] = {}
    held: dict[int, StockItem] = {}
    for line, (match, pick) in enumerate(zip(view.items, view.stock, strict=True)):
        part = record(match)
        if part is None or pick is None or not pick.decided:
            raise ValueError("a decided movement has the part and the stock of every line")
        unit = view.movement.line_items[line].unit
        stocked = view.units[line] if view.units else None
        if stocked is None and not counts_pieces(unit):
            raise ValueError("a decided movement counts every line, or has it in its part's own unit")
        if stocked is not None:
            if units is None or part not in units:
                raise ValueError(f"the units of part {part} were not read")
            if not in_stocking_unit(unit, units[part]):
                return Stale(f"line {line + 1}: part {part} is not stocked in the line's unit now")
        stock = read[(part, place)]
        now = {item.pk: item for item in stock}
        left = [
            replace(item, quantity=max(item.quantity - taken.get(item.pk, Decimal(0)), Decimal(0))) for item in stock
        ]  # what the lines before this one left, as `resolve_lines` picks from it
        for take in pick.takes:
            item = now.get(take.item)
            if item is None:
                return Stale(f"line {line + 1}: stock item {take.item} is no longer at location {place}")
            if operation is Operation.COUNT and take.item in taken:
                raise ValueError("two lines count the same stock item")
            held[item.pk] = item
            taken[item.pk] = taken.get(item.pk, Decimal(0)) + take.quantity
        if why := _line_stale(
            operation, view, line, stock, left, [now[take.item] for take in pick.takes], line in chosen
        ):
            return Stale(f"line {line + 1}: {why}")
    if not built_from(view, request):
        raise ValueError("the request was not built from this movement")
    for pk, quantity in taken.items():
        item = held[pk]
        if operation in (Operation.TRANSFER, Operation.REMOVE):
            if not item.available:
                return Stale(f"stock item {pk} is not available now (status, allocation or empty)")
            if item.quantity < quantity:
                return Stale(f"stock item {pk} holds {_decimal(item.quantity)} now, less than {_decimal(quantity)}")
            if item.serial and (operation is Operation.REMOVE or quantity != item.quantity):
                return Stale(f"stock item {pk} is serialized now, and is moved whole or not at all")
        elif item.serial:
            return Stale(f"stock item {pk} is serialized now")
        elif operation is Operation.ADD and item.quantity + quantity >= QUANTITY_LIMIT:
            total = _decimal(item.quantity + quantity)
            return Stale(f"stock item {pk} would hold {total} after the add, more than InvenTree stores")
    if not held.keys() <= history.keys():
        raise ValueError("the history of a stock item the request names was not read")
    return Snapshot(
        {pk: item.quantity for pk, item in held.items()}, totals, {pk: frozenset(history[pk]) for pk in held}
    )


def built_from(view: MovementView, request: WriteRequest) -> bool:
    """Whether `request` is what `plan.plan_requests` builds from `view`: its operation, one line per stock item the
    view's lines take, with what they take together, and a transfer's destination (no location for the others)."""
    operation, _, destination = places(view)
    taken: dict[int, Decimal] = {}
    for pick in view.stock:
        for take in () if pick is None else pick.takes:
            taken[take.item] = taken.get(take.item, Decimal(0)) + take.quantity
    lines = request.body["items"]
    return (
        request.operation is operation
        and len(lines) == len(taken)
        and {int(line["pk"]): Decimal(line["quantity"]) for line in lines} == taken
        and request.body.get("location") == destination
    )


def _line_stale(
    operation: Operation,
    view: MovementView,
    line: int,
    stock: Sequence[StockItem],
    left: Sequence[StockItem],
    items: Sequence[StockItem],
    chosen: bool,
) -> str | None:
    """Why one line's items no longer fit the rule that picked them, or None. `stock`: the line's stock as read;
    `left`: what the lines before it left of that."""
    serials = view.movement.line_items[line].serials
    if operation in (Operation.TRANSFER, Operation.REMOVE):
        pool = [item for item in left if item.available]
        if any(item.serial for item in pool) and not all(item.serial for item in pool):
            return "serialized and unserialized stock is mixed there now"  # whatever the line takes, or who chose
    if serials:
        missing = next((item for item in items if item.serial not in serials), None)
        return None if missing is None else f"stock item {missing.pk} no longer has a serial number the line names"
    if chosen:
        return None
    if operation in (Operation.ADD, Operation.COUNT):
        pk, reasons = target_item(stock)
        if reasons or [pk] != [item.pk for item in items]:
            return f"the stock at location {places(view)[1]} is no longer one target stock item"
        return None
    if any(item.serial for item in items) and {item.pk for item in pool} != {item.pk for item in items}:
        return "the serialized stock there changed, and the line names no serial numbers"
    return None


def places(view: MovementView) -> tuple[Operation, int, int | None]:
    """The operation, where it acts, and a transfer's destination (None for the other operations)."""
    operation = operation_for(view.movement.movement_type)
    if operation is None:
        raise ValueError("the movement type becomes no operation")
    source, destination = record(view.source), record(view.destination)
    if operation in (Operation.TRANSFER, Operation.REMOVE):
        if source is None or (operation is Operation.TRANSFER and destination is None):
            raise ValueError(f"a {operation.value} needs its places")
        if operation is Operation.TRANSFER and destination == source:
            raise ValueError("a transfer to the place it takes from")
        return operation, source, destination if operation is Operation.TRANSFER else None
    if destination is None:
        raise ValueError(f"a {operation.value} needs the place it acts at")
    return operation, destination, None


def record(match: Match | None) -> int | None:
    """The InvenTree pk a match names, when it may authorize a write."""
    return match.record if match is not None and match.can_authorize_a_write else None


def _pk(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise ValueError(f"not a pk: {text!r}")
    return int(text)


def _quantity(text: object) -> Decimal:
    if not isinstance(text, str):
        raise ValueError("a stored quantity is a decimal string")
    try:
        quantity = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a quantity: {text!r}") from None
    if not quantity.is_finite() or quantity < 0:
        raise ValueError(f"not a quantity: {text!r}")
    return quantity
