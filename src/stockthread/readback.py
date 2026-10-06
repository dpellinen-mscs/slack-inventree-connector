"""Step 5 of the apply protocol, the read-back (design section 7, "Apply protocol").

Pure: after a write InvenTree accepted, the caller searches the history entries of the request's marker id
(`InvenTreeReader.marker_entries`), reads the stock items of every pair of `precondition.pairs` again with the call
the snapshot used (`InvenTreeReader.stock_items`), and finds which stock items the request names answer 404 now
(`gone`). This module says what does not agree with the plan; nothing means the first two checks of step 5 hold:

1. The entries that carry the attempt's marker are the ones the request writes, with the plan's deltas, one per
   stock item (measured on 1.4.3 and 1.5.6, `test_char_history_deltas`):
   - a transfer of part of an item: a split entry (42) on the item, `removed` the quantity, `quantity` what is left
     and `stockitem` the new item; and an entry (40) on the new item, `stockitem` the item and `quantity` the
     quantity. A transfer of the whole item: a location entry (20) on it, `location` the destination and `quantity`
     what it holds;
   - an add (11): `added` the quantity and `quantity` what the item holds after; a remove (12): `removed` and
     `quantity` likewise; a count (10): `quantity` the count.
   A remove of all an item held, or a count to 0, deletes an item marked delete_on_deplete; its entry stays, with
   no item. Below API `DEPLETING_MARKER_FOUND_FROM` the search does not find that entry, so there it may be missing.
   Only such a write may leave an item gone (`empties`).
2. The total at each pair is the snapshot's total plus the plan: a transfer or a remove takes its quantity from the
   source, a transfer puts it at the destination, an add puts it at its place, a count makes the item hold the count.

Not built yet: the third check (entries without the marker on the same stock items), which needs the snapshot to
hold each item's history entry ids.

A problem line names pks, entry types and quantities only: never notes or other text from InvenTree.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal, InvalidOperation

from stockthread.core.marker import Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import StockItem
from stockthread.core.write_request import WriteRequest, _decimal
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, MarkedEntry
from stockthread.precondition import Pair, Snapshot, pairs, places, record
from stockthread.slack.replies import MovementView

COUNTED, ADDED, REMOVED, MOVED, SPLIT_FROM, SPLIT_CHILD = 10, 11, 12, 20, 40, 42
"""InvenTree's stock history entry types that the four stock requests write."""

Wanted = tuple[int, Mapping[str, int | Decimal]]
"""An entry the request writes: its type, and the deltas it must carry (an int is a pk, a Decimal a quantity)."""


def verify(
    view: MovementView,
    request: WriteRequest,
    marker: Marker,
    snapshot: Snapshot,
    entries: Sequence[MarkedEntry],
    read: Mapping[Pair, Sequence[StockItem]],
    *,
    gone: Collection[int],
    api_version: int,
) -> tuple[str, ...]:
    """What does not agree with the plan after `request` (built from `view`, sent with `marker`, accepted), or
    nothing. `snapshot`: step 3's; `entries`: every entry of the marker's id; `read`: the stock items of every pair of
    `pairs(view)`, read now; `gone`: the request's stock items that answer 404 now. Raises ValueError when the inputs
    do not belong together."""
    operation, place, destination = places(view)
    lines = {int(line["pk"]): Decimal(line["quantity"]) for line in request.body["items"]}
    if request.operation is not operation or not str(request.body["notes"]).startswith(str(marker)):
        raise ValueError("the request was not built from this movement and marker")
    if not lines.keys() <= snapshot.stock_items.keys() or not set(pairs(view)) <= snapshot.totals.keys():
        raise ValueError("the snapshot is not of this request")
    if not set(gone) <= lines.keys():
        raise ValueError("gone names a stock item the request does not")
    problems = [
        f"stock item {pk} is gone, and only a removal of all it held or a count to 0 may delete it"
        for pk in sorted(gone)
        if not empties(operation, lines[pk], snapshot.stock_items[pk])
    ]
    others = [entry.pk for entry in entries if entry.marker != marker]
    if others:
        problems.append(f"history entries {_pks(others)} carry another attempt of this request")
    mine = [entry for entry in entries if entry.marker == marker]
    problems += _entries(operation, destination, lines, snapshot, mine, gone, api_version)
    problems += _totals(view, operation, place, destination, lines, snapshot, read)
    return tuple(problems)


def _entries(
    operation: Operation,
    destination: int | None,
    lines: Mapping[int, Decimal],
    snapshot: Snapshot,
    entries: Sequence[MarkedEntry],
    gone: Collection[int],
    api_version: int,
) -> list[str]:
    by_item: dict[int | None, list[MarkedEntry]] = {}
    for entry in entries:
        by_item.setdefault(entry.item, []).append(entry)
    problems: list[str] = []
    of_deleted: list[Wanted] = []
    for pk, quantity in lines.items():
        before = snapshot.stock_items[pk]
        found = by_item.pop(pk, [])
        if pk in gone:
            if found:
                problems.append(f"stock item {pk} is gone, yet history entries {_pks(found)} name it")
            of_deleted.append(_wanted(operation, destination, quantity, before))
        elif operation is Operation.TRANSFER and quantity < before:
            problems += _split(pk, quantity, before, found, by_item, lines)
        else:
            problems += _one(pk, found, _wanted(operation, destination, quantity, before))
    deleted = by_item.pop(None, [])
    if deleted or api_version >= DEPLETING_MARKER_FOUND_FROM:
        unmatched = list(deleted)
        for want in of_deleted:
            match = next((entry for entry in unmatched if _agrees(entry, want)), None)
            if match is None:
                problems.append(f"no history entry {_shown(want)} of a deleted stock item")
            else:
                unmatched.remove(match)
        if unmatched:
            problems.append(f"history entries {_pks(unmatched)} of no stock item, which the request does not write")
    for owner, rest in sorted(by_item.items(), key=lambda pair: pair[0] or 0):
        problems.append(f"history entries {_pks(rest)} on stock item {owner}, which the request does not write")
    return problems


def empties(operation: Operation, quantity: Decimal, before: Decimal) -> bool:
    """Whether a line leaves its item at 0, which deletes an item marked delete_on_deplete: a remove of all it held,
    or a count to 0 (measured: `test_char_a_write_that_empties_an_item`)."""
    return (operation is Operation.REMOVE and quantity == before) or (operation is Operation.COUNT and quantity == 0)


def _wanted(operation: Operation, destination: int | None, quantity: Decimal, before: Decimal) -> Wanted:
    if operation is Operation.TRANSFER:
        if destination is None:
            raise ValueError("a transfer needs its destination")
        return MOVED, {"location": destination, "quantity": before}
    if operation is Operation.ADD:
        return ADDED, {"added": quantity, "quantity": before + quantity}
    if operation is Operation.REMOVE:
        return REMOVED, {"removed": quantity, "quantity": before - quantity}
    return COUNTED, {"quantity": quantity}


def _split(
    pk: int,
    quantity: Decimal,
    before: Decimal,
    found: Sequence[MarkedEntry],
    by_item: dict[int | None, list[MarkedEntry]],
    lines: Mapping[int, Decimal],
) -> list[str]:
    """A transfer of part of item `pk`: its split entry, and the entry on the new item that entry names."""
    problems = _one(pk, found, (SPLIT_CHILD, {"removed": quantity, "quantity": before - quantity}))
    if problems:
        return problems
    child = found[0].deltas.get("stockitem")
    if type(child) is not int or child in lines:
        return [f"the split entry {found[0].pk} of stock item {pk} names no new stock item"]
    return _one(child, by_item.pop(child, []), (SPLIT_FROM, {"stockitem": pk, "quantity": quantity}))


def _one(pk: int, found: Sequence[MarkedEntry], want: Wanted) -> list[str]:
    if len(found) == 1 and _agrees(found[0], want):
        return []
    seen = ", ".join(_seen(entry) for entry in found) or "none"
    return [f"stock item {pk}: history {seen}, the plan says {_shown(want)}"]


def _agrees(entry: MarkedEntry, want: Wanted) -> bool:
    kind, deltas = want
    return entry.tracking_type == kind and all(_same(entry.deltas.get(key), value) for key, value in deltas.items())


def _same(got: object, want: int | Decimal) -> bool:
    if isinstance(want, Decimal):
        return _number(got) == want
    return type(got) is int and got == want


def _number(value: object) -> Decimal | None:
    """A quantity in an entry's deltas: InvenTree writes it as a JSON number."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _totals(
    view: MovementView,
    operation: Operation,
    place: int,
    destination: int | None,
    lines: Mapping[int, Decimal],
    snapshot: Snapshot,
    read: Mapping[Pair, Sequence[StockItem]],
) -> list[str]:
    parts: dict[int, int] = {}
    for match, pick in zip(view.items, view.stock, strict=True):
        part = record(match)
        if part is None or pick is None:
            raise ValueError("a decided movement has the part and the stock of every line")
        parts.update((take.item, part) for take in pick.takes)
    expected = dict(snapshot.totals)
    for pk, quantity in lines.items():
        part = parts[pk]
        if operation is Operation.COUNT:
            expected[(part, place)] += quantity - snapshot.stock_items[pk]
        elif operation is Operation.ADD:
            expected[(part, place)] += quantity
        else:
            expected[(part, place)] -= quantity
            if destination is not None:
                expected[(part, destination)] += quantity
    problems: list[str] = []
    for pair in pairs(view):
        if pair not in read:
            raise ValueError(f"the stock of part {pair[0]} at location {pair[1]} was not read")
        now = sum((item.quantity for item in read[pair]), Decimal(0))
        if now != expected[pair]:
            problems.append(
                f"part {pair[0]} at location {pair[1]} holds {_decimal(now)}, the plan says {_decimal(expected[pair])}"
            )
    return problems


def _seen(entry: MarkedEntry) -> str:
    return f"entry {entry.pk} type {entry.tracking_type}"


def _shown(want: Wanted) -> str:
    kind, deltas = want
    values = ", ".join(
        f"{key} {_decimal(value) if isinstance(value, Decimal) else value}" for key, value in deltas.items()
    )
    return f"type {kind} ({values})"


def _pks(entries: Sequence[MarkedEntry] | Sequence[int]) -> str:
    return ", ".join(str(entry if isinstance(entry, int) else entry.pk) for entry in entries)
