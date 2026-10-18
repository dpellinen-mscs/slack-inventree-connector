"""A decided plan becomes its InvenTree requests: one per movement, in the post's order (design section 7, "Units
of work").

Nothing here writes. `plan_requests` refuses a plan that is not decided in full rather than leave a movement out:
a plan is applied whole or not at all. Decided means what the dry run shows as "could apply" (no reason from
`gate_reasons`) and, checked here again, what the requests are built from: an operation, the one record of every
place the operation acts at, and for every line a count (no unit but "each" or the like, no pack size: the
request carries the bare quantity) with a pick without reasons whose takes add up to the line's quantity.
The switch and the policy are the other two thirds of the write gate (design section 6), checked by the caller.

Not checked here: that each pick was read for its line's part at the place the operation acts at. The views come
from the worker's `_with_stock`, which reads them with the same `operation_for`; a consume policy that changes the
operation must reach both, or the plan must carry what each pick was read for.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Match, Operation, operation_for
from stockthread.core.resolve import Take
from stockthread.core.validate import counts_pieces
from stockthread.core.write_request import WriteRequest, build_request
from stockthread.slack.replies import MovementView, gate_reasons


class NotDecided(ValueError):
    """The plan has a movement no request can be built for without a human."""


def plan_requests(
    views: Sequence[MovementView], markers: Sequence[Marker], *, user: str, link: str | None = None
) -> list[WriteRequest]:
    """The requests of a decided plan, one per movement in the post's order, each with its own apply marker
    (`markers`, one per movement, in the same order). Raises NotDecided naming the first movement that is not
    decided, and ValueError for markers that do not fit."""
    if not views:
        raise NotDecided("a plan has at least one movement")
    if len(markers) != len(views):
        raise ValueError("one marker per movement")
    if any(marker.kind is not AttemptKind.APPLY for marker in markers):
        raise ValueError("a plan's requests carry apply markers")
    if len({marker.marker_id for marker in markers}) != len(markers):
        raise ValueError("each request has its own marker id")
    return [
        _request(number, view, marker, user, link)
        for number, (view, marker) in enumerate(zip(views, markers, strict=True), start=1)
    ]


def _request(number: int, view: MovementView, marker: Marker, user: str, link: str | None) -> WriteRequest:
    def refuse(why: str) -> NotDecided:
        return NotDecided(f"movement {number}: {why}")

    if gate_reasons(view):
        raise refuse("it has a review reason")
    operation = operation_for(view.movement.movement_type)
    if operation is None:
        raise refuse("its movement type becomes no operation")
    destination = _record(view.destination)
    if operation is Operation.TRANSFER:
        if destination is None or _record(view.source) is None:
            raise refuse("a transfer needs its source and its destination")
        if destination == _record(view.source):
            raise refuse("a transfer to the place it takes from")
    elif operation is Operation.REMOVE:
        if _record(view.source) is None:
            raise refuse("a remove needs its source")
    elif destination is None:
        raise refuse(f"a {operation.value} needs the place it acts at")
    if len(view.stock) != len(view.items) or not view.items:
        raise refuse("the stock of every line was not read")
    takes: dict[int, Decimal] = {}
    for line, (item, part, pick) in enumerate(
        zip(view.movement.line_items, view.items, view.stock, strict=True), start=1
    ):
        if _record(part) is None or item.quantity is None:
            raise refuse(f"line {line} has no part or no quantity")
        if not counts_pieces(item.unit) or item.pack_size is not None:
            raise refuse(f"line {line} is not a count: the request would drop its unit")
        if pick is None or not pick.decided:
            raise refuse(f"line {line} has no decided stock")
        if sum((take.quantity for take in pick.takes), Decimal(0)) != item.quantity:
            raise refuse(f"line {line}: its stock does not add up to its quantity")
        if operation in (Operation.ADD, Operation.COUNT) and len(pick.takes) != 1:
            raise refuse(f"line {line}: a {operation.value} acts on one stock item")
        for take in pick.takes:
            if operation is Operation.COUNT and take.item in takes:
                raise refuse(f"line {line}: two lines count the same stock item")
            takes[take.item] = takes.get(take.item, Decimal(0)) + take.quantity
    return build_request(
        operation,
        [Take(item, quantity) for item, quantity in takes.items()],
        marker=marker,
        user=user,
        link=link,
        location=destination if operation is Operation.TRANSFER else None,
    )


def _record(match: Match | None) -> int | None:
    """The record of a match that can authorize a write, else None."""
    return match.record if match is not None and match.can_authorize_a_write else None
