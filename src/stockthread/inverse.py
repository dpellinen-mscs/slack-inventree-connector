"""The undo of a write (design section 7, "Undo"): pure, so every undo body the connector could send is built and
checked here.

An undo reverses what the stock history shows the apply did, not what the plan said: the entries that carry the
apply's marker (`InvenTreeReader.marker_entries`), one per stock item the request named (`readback`'s shapes,
measured on 1.4.3 and 1.5.6). It needs one entry per line, on that line's item, of the type its operation writes:

- a transfer of a whole item (a location entry, 20): transfer the item back to the source; it must be at the
  destination and hold what it held then;
- a transfer of part of an item (a split entry, 42, on the item): transfer the new item back to the source; it must
  be at the destination and hold the quantity, and the item it came from must have no other change between the
  snapshot and the apply (moved elsewhere first, the stock did not come from the source);
- an add (11): remove what was added; the item must hold what the add left;
- a remove (12): add back what was removed; the item must still exist where it was;
- a count (10): count back to what the snapshot held; the item must hold the count the apply set.

An item the undo takes stock from must be available (status OK, nothing allocated). No entry of the request's id
may carry another attempt (a second apply or an undo that wrote), and on every stock item the undo touches, no
history entry since the snapshot may be another than the apply's: another writer's change there is one the undo
would silently fold in. An add onto an item that held 0, or a count of one, has no undo: back at 0 the item may be
deleted (delete_on_deplete), though it existed before. A transfer back carries
`merge: false` like every transfer, so a split's new item is not merged into the item it came from: the stock is
restored per part and location, not by stock item id.

What this does not decide: whether the request may be undone at all (its state, an earlier undo that wrote,
`undo.max_age`). The store and the caller do.

A refusal names pks, entry types and quantities only: never notes or other text from InvenTree.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import StockItem, Take
from stockthread.core.write_request import WriteRequest, _check_quantity, _decimal, build_request
from stockthread.inventree import MarkedEntry
from stockthread.precondition import Pair, Snapshot, built_from, pairs, places, record
from stockthread.readback import ADDED, COUNTED, MOVED, REMOVED, SPLIT_CHILD, _number
from stockthread.slack.replies import MovementView

UNDO_OPERATION = {
    Operation.TRANSFER: Operation.TRANSFER,
    Operation.ADD: Operation.REMOVE,
    Operation.REMOVE: Operation.ADD,
    Operation.COUNT: Operation.COUNT,
}

ENTRY_TYPE = {Operation.ADD: ADDED, Operation.REMOVE: REMOVED, Operation.COUNT: COUNTED}
"""The entry an add, a remove and a count write on their item. A transfer writes a location or a split entry."""


@dataclass(frozen=True)
class Reversal:
    """What one line of the apply did, as its entry shows, and how the undo puts it back."""

    item: int
    """The stock item the undo acts on: the line's item, or the new item a split made."""
    quantity: Decimal
    """What the undo moves, removes or adds; for a count, the count it sets back."""
    holds: Decimal | None
    """What `item` must hold now: exactly what the apply left it holding (every change since writes an entry, so
    another quantity is a change nobody can account for). None for the undo of a remove: it adds to what is there."""
    split_from: int | None = None
    """For a split: the line's stock item, which the new item was taken from."""
    split_entry: int | None = None
    """For a split: the apply's split entry on `split_from`. An entry on `split_from` older than it and not in the
    snapshot is a change between the snapshot and the apply (the item moved, say): the new item may then not have
    come from the source the undo sends it back to."""

    @property
    def history_items(self) -> tuple[int, ...]:
        """The stock items whose history `check_undo` needs."""
        return (self.item,) if self.split_from is None else (self.item, self.split_from)


@dataclass(frozen=True)
class NoUndo:
    why: str
    """One line for the trail and the receipt: which stock item, which entry, what changed."""


def reversals(
    view: MovementView,
    request: WriteRequest,
    marker: Marker,
    snapshot: Snapshot,
    entries: Sequence[MarkedEntry],
) -> list[Reversal] | NoUndo:
    """How to put back each line of `request` (built from `view`, sent with the apply marker `marker`, `snapshot`
    taken before it), from `entries`: every entry of the marker's id. NoUndo when any entry carries another attempt
    of the request (a second apply that also wrote, or an undo that did), or the history does not show what a line
    did in a shape the undo can reverse. Raises ValueError when the inputs do not belong together."""
    operation, _, destination = places(view)
    if marker.kind is not AttemptKind.APPLY:
        raise ValueError("an undo reverses an apply: the marker is an apply marker")
    if not built_from(view, request) or not str(request.body["notes"]).startswith(str(marker)):
        raise ValueError("the request was not built from this movement and marker")
    lines = [int(line["pk"]) for line in request.body["items"]]
    if not set(lines) <= snapshot.stock_items.keys():
        raise ValueError("the snapshot is not of this request")
    if others := _others(marker, entries):
        return others
    found: list[Reversal] = []
    for pk in lines:
        on = [entry for entry in entries if entry.item == pk]
        if len(on) != 1:
            return NoUndo(
                f"stock item {pk} has {len(on)} history entries of the apply, not 1: the history does not "
                "show what it did (an item a removal or a count emptied is deleted: a person recreates it)"
            )
        reversal = _reversal(operation, destination, pk, on[0], snapshot.stock_items[pk])
        if isinstance(reversal, NoUndo):
            return reversal
        found.append(reversal)
    if len({reversal.item for reversal in found}) != len(found):
        return NoUndo("two lines of the apply name the same stock item")
    return found


def _others(marker: Marker, entries: Sequence[MarkedEntry]) -> NoUndo | None:
    """NoUndo when an entry of the request's id carries another attempt than the apply `marker`: the stock then
    holds a write the undo would not reverse, or one that already reversed it. A refused attempt writes no entry."""
    others = sorted(entry.pk for entry in entries if entry.marker != marker)
    if others:
        listed = ", ".join(str(pk) for pk in others)
        return NoUndo(f"history entries {listed} carry another attempt of this request than the apply undone")
    return None


def _reversal(
    operation: Operation, destination: int | None, pk: int, entry: MarkedEntry, before: Decimal
) -> Reversal | NoUndo:
    found = _read(operation, destination, pk, entry, before)
    if isinstance(found, Reversal):
        try:
            _check_quantity(found.quantity, zero_allowed=operation is Operation.COUNT)
        except ValueError:
            return _shape(pk, entry)
    return found


def _read(
    operation: Operation, destination: int | None, pk: int, entry: MarkedEntry, before: Decimal
) -> Reversal | NoUndo:
    if operation is Operation.TRANSFER:
        if entry.tracking_type == MOVED:
            held = _number(entry.deltas.get("quantity"))
            if entry.deltas.get("location") != destination or held is None or held <= 0:
                return _shape(pk, entry)
            return Reversal(pk, held, held)
        if entry.tracking_type == SPLIT_CHILD:
            child, moved = entry.deltas.get("stockitem"), _number(entry.deltas.get("removed"))
            if type(child) is not int or child < 1 or child == pk or moved is None or moved <= 0:
                return _shape(pk, entry)
            return Reversal(child, moved, moved, split_from=pk, split_entry=entry.pk)
        return _shape(pk, entry)
    if entry.tracking_type != ENTRY_TYPE[operation]:
        return _shape(pk, entry)
    after = _number(entry.deltas.get("quantity"))
    if operation is Operation.COUNT:
        if after is None or after < 0:
            return _shape(pk, entry)
        if before == 0:
            return _emptied(pk)
        return Reversal(pk, before, after)
    changed = _number(entry.deltas.get("added" if operation is Operation.ADD else "removed"))
    if changed is None or changed <= 0:
        return _shape(pk, entry)
    if operation is Operation.REMOVE:
        return Reversal(pk, changed, None)
    if after is None or after < changed:
        return _shape(pk, entry)
    if after == changed:
        return _emptied(pk)
    return Reversal(pk, changed, after)


def _shape(pk: int, entry: MarkedEntry) -> NoUndo:
    return NoUndo(
        f"stock item {pk}: history entry {entry.pk} (type {entry.tracking_type}) is not one the undo reverses"
    )


def _emptied(pk: int) -> NoUndo:
    """An item that held 0 before the apply: undone, it would hold 0 again, and InvenTree deletes an item marked
    delete_on_deplete at 0, though it existed before."""
    return NoUndo(f"stock item {pk} held 0 before the apply: the undo would empty it, which may delete it")


def check_undo(
    view: MovementView,
    found: Sequence[Reversal],
    marker: Marker,
    snapshot: Snapshot,
    read: Mapping[Pair, Sequence[StockItem]],
    *,
    entries: Sequence[MarkedEntry],
    history: Mapping[int, Collection[int]],
) -> NoUndo | None:
    """Why the undo of `found` (`reversals` of the apply `marker`) cannot be built on the stock as it is now, or
    None. `read`: the stock items of every part of the movement where the undo acts (a transfer's destination, else
    where the movement acted), read now with `InvenTreeReader.stock_items`; `entries`: every entry of the request's
    marker id, read now (`InvenTreeReader.marker_entries`); `history`: the history entry pks of each stock item of
    `Reversal.history_items`, read now. The only entries allowed since the snapshot are the apply's own; on the item
    a split took from, only those older than the apply's split entry count (history entry pks grow with time, and
    InvenTree writes a change and its entry in one transaction): the undo leaves that item alone, so a change to it
    after the apply is no reason to refuse. An item the undo takes stock from must be available (status OK, nothing
    allocated: an allocation writes no history entry)."""
    if others := _others(marker, entries):
        return others
    operation, place, destination = places(view)
    at = destination if operation is Operation.TRANSFER else place
    takes_from = UNDO_OPERATION[operation] in (Operation.TRANSFER, Operation.REMOVE)
    parts = {record(match) for match in view.items}
    held = {item.pk: item for (part, where), items in read.items() if part in parts and where == at for item in items}
    own = {entry.pk for entry in entries}
    for reversal in found:
        if missing := [pk for pk in reversal.history_items if pk not in history]:
            raise ValueError(f"no history read for stock item {missing[0]}")
        if reversal.split_from is not None and reversal.split_entry is not None:
            parent = reversal.split_from
            new = set(history[parent]) - set(snapshot.history.get(parent, ())) - own
            if between := sorted(entry for entry in new if entry < reversal.split_entry):
                listed = ", ".join(str(entry) for entry in between)
                return NoUndo(
                    f"stock item {parent} has history entries {listed} between the snapshot and the apply that are "
                    f"not this request's: the {_decimal(reversal.quantity)} split from it may not have come from "
                    "the source"
                )
        pk = reversal.item
        later = set(history[pk]) - set(snapshot.history.get(pk, ())) - own
        if later:
            listed = ", ".join(str(entry) for entry in sorted(later))
            return NoUndo(f"stock item {pk} has history entries {listed} since the apply that are not this request's")
        if pk not in held:
            return NoUndo(f"stock item {pk} is no longer at location {at}")
        now = held[pk]
        if reversal.holds is not None and now.quantity != reversal.holds:
            return NoUndo(
                f"stock item {pk} holds {_decimal(now.quantity)}, not the {_decimal(reversal.holds)} the apply left"
            )
        if takes_from and not now.available:
            return NoUndo(
                f"stock item {pk} is not available (status {now.status}, allocated {_decimal(now.allocated)})"
            )
    return None


def undo_request(
    view: MovementView, found: Sequence[Reversal], marker: Marker, *, user: str, link: str | None = None
) -> WriteRequest:
    """The request that undoes the apply of `view` by `found`, with the undo marker `marker` and the notes of
    `write_notes(marker, user, link)`: `user` is the person who asked for the undo."""
    if marker.kind is not AttemptKind.UNDO:
        raise ValueError("an undo carries an undo marker")
    operation, place, _ = places(view)
    takes = [Take(reversal.item, reversal.quantity) for reversal in found]
    location = place if operation is Operation.TRANSFER else None
    return build_request(UNDO_OPERATION[operation], takes, marker=marker, user=user, link=link, location=location)


UNDO_ENTRY = {Operation.ADD: REMOVED, Operation.REMOVE: ADDED, Operation.COUNT: COUNTED}
"""The entry the undo of an add, a remove and a count writes on its item. The undo of a transfer moves a whole item
(the line's, or the new one a split made) back: a location entry."""


@dataclass(frozen=True)
class UndoSnapshot:
    """The stock just before the undo is sent, what its read-back compares with: an undo is checked against the stock
    it was built on, not the apply's snapshot (another item at the same place may have changed since the apply)."""

    totals: Mapping[Pair, Decimal]
    """What each pair `precondition.pairs` names held."""
    where: Mapping[int, Pair]
    """The pair each `Reversal.item` was read at."""
    history: Mapping[int, frozenset[int]]
    """The history entry pks of each stock item of `Reversal.history_items`."""

    def as_json(self) -> dict[str, Any]:
        """The stored form, the attempt's snapshot: `{"totals": {"<part>@<location>": "<quantity>"}, "where":
        {"<pk>": "<part>@<location>"}, "history": {"<pk>": [<entry pk>, ...]}}`. It has no `stock_items`: the step-2
        depletion excuse (`apply.depletes`) never applies to an undo, which `check_undo` keeps from emptying an item."""
        return {
            "totals": {_pair(pair): _decimal(total) for pair, total in sorted(self.totals.items())},
            "where": {str(pk): _pair(pair) for pk, pair in sorted(self.where.items())},
            "history": {str(pk): sorted(entries) for pk, entries in sorted(self.history.items())},
        }


def _pair(pair: Pair) -> str:
    return f"{pair[0]}@{pair[1]}"


def undo_snapshot(
    view: MovementView,
    found: Sequence[Reversal],
    read: Mapping[Pair, Sequence[StockItem]],
    history: Mapping[int, Collection[int]],
) -> UndoSnapshot:
    """The snapshot of the stock `check_undo` passed: `read` and `history` as given to it. ValueError if a pair of
    the movement or a reversed item was not read."""
    wanted = pairs(view)
    if missing := [pair for pair in wanted if pair not in read]:
        raise ValueError(f"no stock read for part {missing[0][0]} at location {missing[0][1]}")
    totals = {pair: sum((item.quantity for item in read[pair]), Decimal(0)) for pair in wanted}
    where = {item.pk: pair for pair in wanted for item in read[pair]}
    if missing_items := [reversal.item for reversal in found if reversal.item not in where]:
        raise ValueError(f"stock item {missing_items[0]} was not read")
    if missing_history := [pk for reversal in found for pk in reversal.history_items if pk not in history]:
        raise ValueError(f"no history read for stock item {missing_history[0]}")
    return UndoSnapshot(
        totals,
        {reversal.item: where[reversal.item] for reversal in found},
        {pk: frozenset(history[pk]) for reversal in found for pk in reversal.history_items},
    )


def verify_undo(
    view: MovementView,
    found: Sequence[Reversal],
    marker: Marker,
    before: UndoSnapshot,
    entries: Sequence[MarkedEntry],
    read: Mapping[Pair, Sequence[StockItem]],
    *,
    history: Mapping[int, Collection[int]],
) -> list[str]:
    """What the read-back of the undo `marker` of `found` disagrees with, or nothing. `before`: `undo_snapshot`;
    `entries`: every entry of the request's marker id, read now; `read`: the stock of `precondition.pairs(view)`, read
    now, then `history`: the entries of each `Reversal.item`, read now.

    It checks three things, as `readback` does for an apply: the undo marker is on exactly one entry per reversed
    item, of the type and with the numbers the undo writes; every reversed item is where the undo leaves it, holding
    what it should, and each total moved by exactly the reversed quantities; and no reversed item has an entry since
    `before` that is not this request's."""
    if marker.kind is not AttemptKind.UNDO:
        raise ValueError("an undo is read back with its undo marker")
    operation, place, destination = places(view)
    problems: list[str] = []
    mine = [entry for entry in entries if entry.marker == marker]
    expected_totals = dict(before.totals)
    for reversal in found:
        part, at = before.where[reversal.item]
        on = [entry for entry in mine if entry.item == reversal.item]
        if len(on) != 1:
            problems.append(f"stock item {reversal.item} has {len(on)} history entries of the undo, not 1")
        elif problem := _undo_entry(operation, place, reversal, on[0]):
            problems.append(problem)
        if operation is Operation.TRANSFER:
            if place is None or at != destination:
                raise ValueError(f"stock item {reversal.item} was not read at the transfer's destination")
            expected_totals[(part, at)] -= reversal.quantity
            expected_totals[(part, place)] += reversal.quantity
            left: tuple[Pair, Decimal | None] = ((part, place), reversal.quantity)
        elif operation is Operation.ADD:
            expected_totals[(part, at)] -= reversal.quantity
            left = ((part, at), None if reversal.holds is None else reversal.holds - reversal.quantity)
        elif operation is Operation.REMOVE:
            expected_totals[(part, at)] += reversal.quantity
            left = ((part, at), None)
        else:
            if reversal.holds is None:
                raise ValueError("the reversal of a count holds what the count set")
            expected_totals[(part, at)] += reversal.quantity - reversal.holds
            left = ((part, at), reversal.quantity)
        problems += _left(reversal.item, *left, read)
    for pair, total in sorted(expected_totals.items()):
        now = sum((item.quantity for item in read.get(pair, ())), Decimal(0))
        if pair not in read or now != total:
            problems.append(
                f"part {pair[0]} at location {pair[1]} holds {_decimal(now)}, the undo should leave {_decimal(total)}"
            )
    own = {entry.pk for entry in entries}
    for reversal in found:
        if reversal.item not in history:
            raise ValueError(f"no history read for stock item {reversal.item}")
        later = set(history[reversal.item]) - before.history.get(reversal.item, frozenset()) - own
        if later:
            listed = ", ".join(str(entry) for entry in sorted(later))
            problems.append(f"stock item {reversal.item} has history entries {listed} that are not this request's")
    return problems


def _undo_entry(operation: Operation, place: int | None, reversal: Reversal, entry: MarkedEntry) -> str | None:
    if operation is Operation.TRANSFER:
        kind, numbers = MOVED, {"location": place, "quantity": reversal.quantity}
    elif operation is Operation.COUNT:
        kind, numbers = COUNTED, {"quantity": reversal.quantity}
    else:
        kind = UNDO_ENTRY[operation]
        numbers = {"removed" if operation is Operation.ADD else "added": reversal.quantity}
    if entry.tracking_type != kind:
        return f"the undo's entry {entry.pk} on stock item {reversal.item} is of type {entry.tracking_type}, not {kind}"
    for key, want in numbers.items():
        got = entry.deltas.get(key)
        same = got == want if key == "location" else _number(got) == want
        if not same:
            return f"the undo's entry {entry.pk} on stock item {reversal.item} does not show {key} {want}"
    return None


def _left(pk: int, pair: Pair, holds: Decimal | None, read: Mapping[Pair, Sequence[StockItem]]) -> list[str]:
    now = {item.pk: item for item in read.get(pair, ())}
    if pk not in now:
        return [f"stock item {pk} is not at location {pair[1]}"]
    if holds is not None and now[pk].quantity != holds:
        return [f"stock item {pk} holds {_decimal(now[pk].quantity)}, the undo should leave {_decimal(holds)}"]
    return []
