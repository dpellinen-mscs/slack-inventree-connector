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

An add or a count needs a single existing stock item of the part at its location (`target_item`): v1.0 never
creates stock items.

Pure: no I/O. The rows are InvenTree's JSON and are checked here, so a malformed row raises rather than moves stock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from stockthread.core.model import ReasonCode, ReasonSource, ReviewReason

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
