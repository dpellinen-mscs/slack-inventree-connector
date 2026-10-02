"""Source selection: which concrete stock items a line moves (design section 5, "Stock items, not parts").

A transfer or a remove acts on stock items, not on a part. Given the stock items of the matched part in the
source location itself (`InvenTree.stock_items`, no sub-locations, in stock), this module picks the items and the
quantity taken from each, by one documented rule:

- Only items with status OK and nothing allocated to an order are considered.
- One item that covers the quantity is preferred over a split; among those, the oldest (the lowest id).
- Otherwise the quantity is split over the items oldest first.
- Serialized stock is moved as whole items, chosen by serial number. Without serials, only a pick that takes every
  serialized item is decided; anything else is for a human.
- When the items it could pick from differ in batch or supplier part, the pick is a proposal and a review reason
  says the choice matters.
- The lines of one post share the stock (`resolve_lines`): a line picks from what the lines before it left, so two
  lines never take the same unit; only a shortage that the other lines cause says so. A count acts on an item as
  it is, so a counted item that another line also acts on is for a human: the result depends on the order. So is
  an add into an item that an earlier line takes in full: InvenTree moves (or deletes) an item taken whole.
- Stock items a person chose in the thread replace the choice: the line acts on those only, and says so when they no
  longer fit. A pick a choice can settle carries what the stock question offers.

An add or a count needs a single existing stock item of the part at its location (`target_item`): v1.0 never
creates stock items.

Pure: no I/O. The rows are InvenTree's JSON and are checked here, so a malformed row raises rather than moves stock.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from stockthread.core.model import Operation, ReasonCode, ReasonSource, ReviewReason

STATUS_OK = 10
"""InvenTree's stock status code for OK (`StockStatus.OK`)."""

# Rule-written details: never words of a post.
DETAIL_NO_STOCK = "no stock item at the source"
DETAIL_TOO_FEW = "fewer at the source than the quantity"
DETAIL_BATCH = "the items differ in batch"
DETAIL_SUPPLIER_PART = "the items differ in supplier part"
DETAIL_SERIALS_NEEDED = "serialized stock: serial numbers pick the items"
DETAIL_SERIAL_NOT_FOUND = "a serial number is not in stock at the source"
DETAIL_SERIAL_COUNT = "the serial numbers do not match the quantity"
DETAIL_MIXED = "serialized and unserialized stock of the part at the source"
DETAIL_NO_TARGET = "no stock item of the part at the location"
DETAIL_SEVERAL_TARGETS = "several stock items of the part at the location"
DETAIL_SERIALIZED_TARGET = "serialized stock: an add or a count does not change it"
DETAIL_SHARED = "other lines of the post take the same stock"
DETAIL_COUNTED = "another line of the post acts on a counted stock item"
DETAIL_EMPTIED = "an earlier line of the post takes all of the stock item an add acts on"
DETAIL_CHOSEN_SHORT = "the chosen stock items no longer cover the quantity"
DETAIL_CHOSEN_GONE = "the chosen stock item is no longer at the location"
DETAIL_CHOSEN_SERIALIZED = "the chosen stock item is serialized now"
DETAIL_NONE_CHOSEN = "none of the stock items offered is the one meant"
CHOOSABLE = frozenset(
    {
        DETAIL_BATCH,
        DETAIL_SUPPLIER_PART,
        DETAIL_SEVERAL_TARGETS,
        DETAIL_CHOSEN_SHORT,
        DETAIL_CHOSEN_GONE,
        DETAIL_CHOSEN_SERIALIZED,
    }
)
"""The details of a pick that a choice of stock items in the thread can settle: a choice that matters, or a choice
that no longer holds. Any other stock reason is fixed in InvenTree or by editing the post."""
MAX_OFFER = 5
"""At most this many stock items are offered as buttons (as `questions.MAX_CHOICES`)."""
_SHORTAGE = (DETAIL_NO_STOCK, DETAIL_TOO_FEW, DETAIL_SERIAL_NOT_FOUND)


@dataclass(frozen=True)
class StockItem:
    """The fields of an InvenTree stock item that source selection reads."""

    pk: int
    quantity: Decimal
    status: int
    allocated: Decimal
    serial: str | None = None
    batch: str | None = None
    supplier_part: int | None = None

    @property
    def available(self) -> bool:
        return self.status == STATUS_OK and self.allocated == 0 and self.quantity > 0


@dataclass(frozen=True)
class Take:
    item: int
    """The stock item's primary key."""
    quantity: Decimal


@dataclass(frozen=True)
class SourcePick:
    takes: tuple[Take, ...]
    """What the line takes from each stock item, oldest first. Empty when nothing could be picked."""
    reasons: tuple[ReviewReason, ...] = ()
    """Why a human must decide. A pick with reasons is a proposal, never a write."""
    offer: tuple[StockItem, ...] = ()
    """The stock items the line could act on alone, as the lines before it left them, oldest first, at most
    MAX_OFFER: for a transfer or a remove those that cover its quantity, for an add or a count the unserialized
    items at the place. Set only when a reason is in CHOOSABLE."""
    split: tuple[Take, ...] = ()
    """For a transfer or a remove that no single item covers: the rule's split of what is left. Set only when a
    reason is in CHOOSABLE and `offer` is empty."""

    @property
    def decided(self) -> bool:
        return bool(self.takes) and not self.reasons


def stock_items(rows: Iterable[Mapping[str, Any]]) -> list[StockItem]:
    """InvenTree's stock item rows, checked. Raises ValueError on a row that lacks a field or has a wrong type."""
    return [_item(row) for row in rows]


def pick_source(quantity: Decimal, items: Sequence[StockItem], serials: Sequence[str] = ()) -> SourcePick:
    """The stock items a line of `quantity` (and `serials`, when given) takes from, by the rule above."""
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("a pick is for a quantity above zero")
    pool = sorted((item for item in items if item.available), key=lambda item: item.pk)
    if not pool:
        return SourcePick((), (_reason(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_NO_STOCK),))
    serialized = [item for item in pool if item.serial]
    if serialized and len(serialized) != len(pool):
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_MIXED),))
    if serialized:
        return _pick_serialized(quantity, pool, serials)
    if serials:
        return SourcePick((), (_reason(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_SERIAL_NOT_FOUND),))
    if sum((item.quantity for item in pool), Decimal(0)) < quantity:
        return SourcePick((), (_reason(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW),))
    single = next((item for item in pool if item.quantity >= quantity), None)
    if single is not None:
        takes: tuple[Take, ...] = (Take(single.pk, quantity),)
    else:
        left, split = quantity, []
        for item in pool:
            if left <= 0:
                break
            split.append(Take(item.pk, min(item.quantity, left)))
            left -= item.quantity
        takes = tuple(split)
    return SourcePick(takes, _differences(pool, takes))


def target_item(items: Sequence[StockItem]) -> tuple[int | None, tuple[ReviewReason, ...]]:
    """The one stock item an add or a count acts on, or why there is none. Status and allocation do not matter: a
    count or a receipt is about the item that is there."""
    if not items:
        return None, (_reason(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_NO_TARGET),)
    if len(items) > 1:
        return None, (_reason(ReasonCode.STOCK_CHOICE, DETAIL_SEVERAL_TARGETS),)
    if items[0].serial:  # InvenTree answers success and changes nothing (design section 5, "Serialized stock")
        return None, (_reason(ReasonCode.STOCK_CHOICE, DETAIL_SERIALIZED_TARGET),)
    return items[0].pk, ()


def resolve_line(
    operation: Operation, quantity: Decimal, items: Sequence[StockItem], serials: Sequence[str] = ()
) -> SourcePick:
    """What one line acts on: for a transfer or a remove the items it takes from (`pick_source`), for an add or a
    count the one item it changes, by `quantity`."""
    if operation in (Operation.TRANSFER, Operation.REMOVE):
        return pick_source(quantity, items, serials)
    pk, reasons = target_item(items)
    return SourcePick(() if pk is None else (Take(pk, quantity),), reasons)


@dataclass(frozen=True)
class LineStock:
    """One line of a post as `resolve_lines` reads it."""

    operation: Operation
    quantity: Decimal
    items: Sequence[StockItem]
    """The stock items of the line's part at its place, as read: before any line of the post."""
    serials: Sequence[str] = ()
    chosen: tuple[int, ...] | None = None
    """The stock items a person named for the line in the thread (design section 4, "The stock question"), or None
    when nobody did. Empty: "None of these"."""


def resolve_lines(lines: Sequence[LineStock]) -> list[SourcePick]:
    """What each line of a post acts on, in the post's order, the lines sharing the stock they read. A transfer or a
    remove picks from what the lines before it left. When what is left falls short and the stock as read did not,
    its reasons are those of the stock as read and of what is left, and DETAIL_SHARED; otherwise they are those of
    what is left (a choice among what is left is still a choice). An add into an item that the lines before it take
    in full gets DETAIL_EMPTIED. A line that acts on an item a count acts on, together with another line, gets
    DETAIL_COUNTED. A line with `chosen` items acts on those only (`_chosen`). A pick that a choice can settle
    carries what the stock question offers (`SourcePick.offer`, `SourcePick.split`)."""
    taken: dict[int, Decimal] = {}
    picks = []
    for line in lines:
        if line.operation in (Operation.TRANSFER, Operation.REMOVE):
            left = [
                replace(item, quantity=max(item.quantity - taken.get(item.pk, Decimal(0)), Decimal(0)))
                for item in line.items
            ]
            if line.chosen is not None:
                pick = _chosen(line, line.chosen, left)
            else:
                pick = pick_source(line.quantity, left, line.serials)
                if not pick.decided and left != list(line.items):
                    as_read = pick_source(line.quantity, line.items, line.serials)
                    if _short(pick) - _short(as_read):
                        pick = _shared(pick, as_read)
            for take in pick.takes:
                taken[take.item] = taken.get(take.item, Decimal(0)) + take.quantity
        else:
            left = list(line.items)
            if line.chosen is not None and len(line.chosen) <= 1:
                pick = _chosen(line, line.chosen, left)
            else:  # several items named for one target: not an answer an add or a count can take
                pick = resolve_line(line.operation, line.quantity, line.items, line.serials)
            whole = {item.pk: item.quantity for item in line.items}
            if line.operation is Operation.ADD and any(
                take.item in taken and taken[take.item] >= whole[take.item] for take in pick.takes
            ):
                pick = replace(pick, reasons=(*pick.reasons, _reason(ReasonCode.STOCK_CHOICE, DETAIL_EMPTIED)))
        picks.append(_offered(line, left, pick))
    counted = {
        take.item
        for line, pick in zip(lines, picks, strict=True)
        if line.operation is Operation.COUNT
        for take in pick.takes
    }
    lines_on = Counter(item for pick in picks for item in {take.item for take in pick.takes})
    return [
        replace(pick, reasons=(*pick.reasons, _reason(ReasonCode.STOCK_CHOICE, DETAIL_COUNTED)))
        if any(take.item in counted and lines_on[take.item] > 1 for take in pick.takes)
        else pick
        for pick in picks
    ]


def _chosen(line: LineStock, chosen: tuple[int, ...], left: Sequence[StockItem]) -> SourcePick:
    """What a line acts on when a person named its stock items. A transfer or a remove picks from the named items
    only, by the rule, from what the lines before it left; their differences are not asked about again (a person
    chose them), and a shortfall is DETAIL_CHOSEN_SHORT. An add or a count acts on the one named item while it is at
    the place and unserialized, whatever it holds."""
    if not chosen:
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_NONE_CHOSEN),))
    if line.operation in (Operation.TRANSFER, Operation.REMOVE):
        named = [item for item in left if item.pk in chosen]
        pick = pick_source(line.quantity, named, line.serials) if named else SourcePick(())
        if pick.takes and all(reason.detail in (DETAIL_BATCH, DETAIL_SUPPLIER_PART) for reason in pick.reasons):
            return SourcePick(pick.takes)
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SHORT),))
    item = next((item for item in left if item.pk == chosen[0]), None)
    if item is None:
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_GONE),))
    if item.serial:
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SERIALIZED),))
    return SourcePick((Take(item.pk, line.quantity),))


def _offered(line: LineStock, left: Sequence[StockItem], pick: SourcePick) -> SourcePick:
    """The pick with what the stock question offers, when a choice can settle one of its reasons."""
    if not any(reason.detail in CHOOSABLE for reason in pick.reasons):
        return pick
    if line.operation not in (Operation.TRANSFER, Operation.REMOVE):
        targets = sorted((item for item in left if not item.serial), key=lambda item: item.pk)
        return replace(pick, offer=tuple(targets[:MAX_OFFER]))
    pool = sorted((item for item in left if item.available and not item.serial), key=lambda item: item.pk)
    covering = tuple(item for item in pool if item.quantity >= line.quantity)[:MAX_OFFER]
    if covering:
        return replace(pick, offer=covering)
    rule = pick_source(line.quantity, left, line.serials)
    if rule.takes and all(reason.detail in CHOOSABLE for reason in rule.reasons):
        return replace(pick, split=rule.takes)
    return pick


def _short(pick: SourcePick) -> set[str]:
    return {reason.detail for reason in pick.reasons if reason.detail in _SHORTAGE}


def _shared(left: SourcePick, as_read: SourcePick) -> SourcePick:
    """A pick from what other lines left that falls short where the stock as read did not: the reasons the stock as
    read gives, what is left beyond a shortage (the shortage is the other lines'), and DETAIL_SHARED."""
    reasons = [*as_read.reasons, *(reason for reason in left.reasons if reason.detail not in _SHORTAGE)]
    reasons.append(_reason(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_SHARED))
    return SourcePick(left.takes, tuple(dict.fromkeys(reasons)))


def _pick_serialized(quantity: Decimal, pool: Sequence[StockItem], serials: Sequence[str]) -> SourcePick:
    if quantity != quantity.to_integral_value() or (serials and len(serials) != quantity):
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_SERIAL_COUNT),))
    if serials:
        by_serial = {item.serial: item for item in pool}
        if any(serial not in by_serial for serial in serials):
            return SourcePick((), (_reason(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_SERIAL_NOT_FOUND),))
        chosen = sorted((by_serial[serial] for serial in serials), key=lambda item: item.pk)
        return SourcePick(tuple(Take(item.pk, Decimal(1)) for item in chosen))
    if len(pool) < quantity:
        return SourcePick((), (_reason(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW),))
    if len(pool) > quantity:
        return SourcePick((), (_reason(ReasonCode.STOCK_CHOICE, DETAIL_SERIALS_NEEDED),))
    return SourcePick(tuple(Take(item.pk, Decimal(1)) for item in pool))  # every one of them: nothing to choose


def _differences(pool: Sequence[StockItem], takes: Sequence[Take]) -> tuple[ReviewReason, ...]:
    """A choice matters only when there is one: a pick that empties every item chose nothing."""
    if sum((take.quantity for take in takes), Decimal(0)) == sum((item.quantity for item in pool), Decimal(0)):
        return ()
    reasons = []
    if len({item.batch or "" for item in pool}) > 1:
        reasons.append(_reason(ReasonCode.STOCK_CHOICE, DETAIL_BATCH))
    if len({item.supplier_part for item in pool}) > 1:
        reasons.append(_reason(ReasonCode.STOCK_CHOICE, DETAIL_SUPPLIER_PART))
    return tuple(reasons)


def _reason(code: ReasonCode, detail: str) -> ReviewReason:
    return ReviewReason(code, ReasonSource.RULE, detail)


def _item(row: Mapping[str, Any]) -> StockItem:
    pk, status = row.get("pk"), row.get("status")
    if not isinstance(pk, int) or isinstance(pk, bool) or pk < 1:
        raise ValueError("a stock item has a positive integer pk")
    if not isinstance(status, int) or isinstance(status, bool):
        raise ValueError("a stock item has an integer status")
    supplier_part = row.get("supplier_part")
    if supplier_part is not None and (not isinstance(supplier_part, int) or isinstance(supplier_part, bool)):
        raise ValueError("a stock item's supplier part is an integer or null")
    serial, batch = row.get("serial"), row.get("batch")
    if not isinstance(serial, str | None) or not isinstance(batch, str | None):
        raise ValueError("a stock item's serial and batch are text or null")
    return StockItem(
        pk,
        _quantity(row, "quantity"),
        status,
        _quantity(row, "allocated"),
        serial.strip() or None if serial else None,
        batch.strip() or None if batch else None,
        supplier_part,
    )


def _quantity(row: Mapping[str, Any], name: str) -> Decimal:
    if name not in row:
        raise ValueError(f"a stock item has a {name}")  # a missing allocation is not the same as none
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"a stock item's {name} is a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"a stock item's {name} is a number") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"a stock item's {name} is a finite number, zero or more")
    return number
