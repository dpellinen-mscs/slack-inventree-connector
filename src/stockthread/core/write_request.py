"""The InvenTree request a decided line set becomes (design sections 5 and 7): pure, so every body the connector
could ever send is built and checked here, before any network code sees it.

One request per movement: its lines share one operation and, for a transfer, one destination. Each line acts on
the stock items its pick took (`core/resolve.py`). The builder never repairs a plan. Anything it would have to
guess (a duplicate item, a quantity InvenTree would round, a transfer without a destination, notes that would
not fit) is a ValueError, and nothing is sent. The notes are built here too, from the marker, the user id and the
link, so no other text can reach them.

Not checked here: whether an item is serialized (a `Take` carries only its key). InvenTree answers an add, a
remove or a count on a serialized item with success and changes nothing (design section 5, "Serialized stock"), so
the step that turns a plan into requests must never ask for one. `core/resolve.py` does pick serialized items for
a remove (a consume): that step has to settle it before any write.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from stockthread.core.marker import Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import Take

PATHS = {
    Operation.TRANSFER: "/api/stock/transfer/",
    Operation.REMOVE: "/api/stock/remove/",
    Operation.ADD: "/api/stock/add/",
    Operation.COUNT: "/api/stock/count/",
}

NOTES_LIMIT = 200
"""Notes stay under this many characters: the server stores 512 and PostgreSQL enforces it (`test_char_notes_length`),
and a write whose notes do not fit may change stock without the history entry that carries the marker."""

QUANTITY_PLACES = 5
"""InvenTree stores a stock quantity with 5 decimal places and 15 digits in all."""
QUANTITY_DIGITS = 15

_USER = re.compile(r"[UW][A-Z0-9]{2,}")
_LINK = re.compile(r"https://[!-~]+")


@dataclass(frozen=True)
class WriteRequest:
    operation: Operation
    path: str
    body: Mapping[str, Any]
    """JSON-ready: quantities as decimal strings, never floats."""


def write_notes(marker: Marker, user: str, link: str | None = None) -> str:
    """The notes of a write: the marker first, then the Slack user id, then the link to the Slack message."""
    if not _USER.fullmatch(user):
        raise ValueError("a Slack user id looks like U0123ABCD")
    if link is not None and not _LINK.fullmatch(link):
        raise ValueError("the link is an https URL of printable ASCII without spaces")
    notes = " ".join([str(marker), user, *([link] if link is not None else [])])
    if len(notes) >= NOTES_LIMIT:
        raise ValueError(f"the notes would be {len(notes)} characters; they must stay under {NOTES_LIMIT}")
    return notes


def build_request(
    operation: Operation,
    takes: Sequence[Take],
    *,
    marker: Marker,
    user: str,
    link: str | None = None,
    location: int | None = None,
) -> WriteRequest:
    """The request that applies `takes` by `operation`. A transfer moves them to `location`; the other operations
    act where the items are, so they take no location. The notes are `write_notes(marker, user, link)`.

    Each take is a stock item and a quantity: what a transfer moves or a remove takes out, what an add puts in,
    or what a count sets (zero allowed). Every transfer line carries `merge: false` (design section 5, "No merge").
    """
    if not takes:
        raise ValueError("a request acts on at least one stock item")
    pks = [take.item for take in takes]
    if any(isinstance(pk, bool) or not isinstance(pk, int) or pk < 1 for pk in pks):
        raise ValueError("a stock item is a positive primary key")
    if len(set(pks)) != len(pks):
        raise ValueError("a request acts on each stock item once")
    for take in takes:
        _check_quantity(take.quantity, zero_allowed=operation is Operation.COUNT)
    notes = write_notes(marker, user, link)
    if operation is Operation.TRANSFER:
        if location is None or isinstance(location, bool) or not isinstance(location, int) or location < 1:
            raise ValueError("a transfer needs the primary key of its destination")
    elif location is not None:
        raise ValueError(f"a {operation.value} acts where its stock items are, and takes no location")
    lines: list[dict[str, Any]] = [
        {
            "pk": take.item,
            "quantity": _decimal(take.quantity),
            **({"merge": False} if operation is Operation.TRANSFER else {}),
        }
        for take in takes
    ]
    body: dict[str, Any] = {"items": lines, "notes": notes}
    if location is not None:
        body["location"] = location
    return WriteRequest(operation, PATHS[operation], body)


def _check_quantity(quantity: Decimal, *, zero_allowed: bool) -> None:
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValueError("a quantity is a finite Decimal")
    if quantity < 0 or (quantity == 0 and not zero_allowed):
        raise ValueError("a quantity is positive (a count may set zero)")
    exponent = _normal(quantity).as_tuple().exponent
    if isinstance(exponent, int) and exponent < -QUANTITY_PLACES:
        raise ValueError(f"InvenTree would round a quantity with more than {QUANTITY_PLACES} decimal places")
    if quantity >= Decimal(10) ** (QUANTITY_DIGITS - QUANTITY_PLACES):
        raise ValueError("the quantity is larger than InvenTree stores")


def _decimal(quantity: Decimal) -> str:
    """`Decimal("2.50")` as "2.5", `Decimal("1E+2")` as "100": never exponent notation."""
    return "0" if quantity == 0 else format(_normal(quantity), "f")  # never "-0"


def _normal(quantity: Decimal) -> Decimal:
    """`quantity` without trailing zeros, never rounded: `normalize` rounds to the context's precision (28 digits)."""
    with localcontext(prec=max(28, len(quantity.as_tuple().digits))):
        return quantity.normalize()
