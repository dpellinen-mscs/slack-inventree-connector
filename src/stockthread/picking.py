# SPDX-License-Identifier: MIT
"""The stock items a dry run's settled lines act on: which lines are settled enough to read stock for, and the picks
`core/resolve.py` makes from the stock read, with the reasons they raise (design section 4, "Resolve")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from stockthread.core.model import Operation, ReasonCode, ReviewReason, operation_for
from stockthread.core.questions import reason_line
from stockthread.core.resolve import LineStock, SourcePick, StockItem, resolve_lines
from stockthread.slack.replies import MovementView

_QUANTITY_UNSETTLED = frozenset(
    {
        ReasonCode.QUANTITY_MISSING,
        ReasonCode.QUANTITY_VAGUE,
        ReasonCode.PACK_UNIT_AMBIGUOUS,
        ReasonCode.QUANTITY_FROM_PHOTO,
    }
)
"""A line with one of these has no quantity to pick stock for yet: its question comes first (design section 4)."""


def stock_lines(view: MovementView) -> list[tuple[int, Operation, int, int, Decimal]]:
    """The lines whose stock can be read: (line, operation, part, location, quantity). A line qualifies once its
    part, the place its operation acts at (the source for a transfer or a remove, the destination for an add or a
    count) and its quantity are settled; anything less would pick stock for a guess."""
    movement = view.movement
    operation = operation_for(movement.movement_type)
    if operation is None:
        return []
    place = view.source if operation in (Operation.TRANSFER, Operation.REMOVE) else view.destination
    if place is None or not place.can_authorize_a_write or place.record is None:
        return []
    unsettled = {reason_line(reason, movement) for reason in view.reasons if reason.code in _QUANTITY_UNSETTLED}
    if None in unsettled:
        return []  # a quantity doubt that names no line: none is settled
    found = []
    for line, (item, part) in enumerate(zip(movement.line_items, view.items, strict=True)):
        if line in unsettled or item.quantity is None or not part.can_authorize_a_write or part.record is None:
            continue
        if item.quantity < 0 or (item.quantity == 0 and operation is not Operation.COUNT):
            continue  # the validator raises its own reason
        found.append((line, operation, part.record, place.record, item.quantity))
    return found


def with_stock(
    views: Sequence[MovementView],
    found: Mapping[tuple[int, int], Sequence[StockItem]],
    chosen: Mapping[tuple[int, int], tuple[int, ...]] | None = None,
) -> list[MovementView]:
    """The views with the stock items each settled line acts on, and the reasons the picks raise, each naming its
    line (`core/resolve.py`). The lines of every movement of the post share the stock, in the post's order. A line
    a person named stock items for (`chosen`, by movement and line) acts on those only. Lines whose stock was not
    read are left as they are."""
    chosen = chosen or {}
    lines = [
        (index, entry)
        for index, view in enumerate(views)
        for entry in stock_lines(view)
        if (entry[2], entry[3]) in found
    ]
    resolved = resolve_lines(
        [
            LineStock(
                operation,
                quantity,
                found[(part, place)],
                views[index].movement.line_items[line].serials,
                chosen.get((index, line)),
            )
            for index, (line, operation, part, place, quantity) in lines
        ]
    )
    picks: dict[int, list[SourcePick | None]] = {}
    added: dict[int, list[ReviewReason]] = {}
    for (index, (line, *_)), pick in zip(lines, resolved, strict=True):
        picks.setdefault(index, [None] * len(views[index].items))[line] = pick
        added.setdefault(index, []).extend(
            replace(reason, detail=f"line {line + 1}: {reason.detail}") for reason in pick.reasons
        )
    return [
        replace(view, reasons=(*view.reasons, *added[index]), stock=tuple(picks[index])) if index in picks else view
        for index, view in enumerate(views)
    ]
