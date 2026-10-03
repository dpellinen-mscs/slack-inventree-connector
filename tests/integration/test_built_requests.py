"""Every body `core/write_request.py` builds is accepted by each pinned InvenTree version and does exactly what it
says: decimal-string quantities, `merge: false` on transfer lines, and notes whose marker can be found."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import build_request, write_notes

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

LINK = "https://acme-fiber.slack.com/archives/C0ACME001/p1790000000000100"


def notes() -> str:
    return write_notes(Marker(new_marker_id(), AttemptKind.APPLY, 1), "U0SAM0001", LINK)


def send(api: RawApi, operation: Operation, takes: list[Take], location: int | None = None) -> str:
    written = notes()
    request = build_request(operation, takes, notes=written, location=location)
    response = api.post(request.path, dict(request.body))
    assert response.status_code == 201, response.text[:500]
    return written


def history(api: RawApi, written: str) -> list[dict[str, Any]]:
    marker = written.split(" ", 1)[0]
    found = api.get("/api/stock/track/", search=marker)
    return [row for row in (found["results"] if isinstance(found, dict) else found) if row["notes"] == written]


def quantity(scratch: Scratch, pk: int) -> Decimal:
    return Decimal(str(scratch.item(pk)["quantity"]))


def test_a_built_transfer_splits_the_item_and_leaves_its_marker(api: RawApi, scratch: Scratch) -> None:
    item = scratch.stock(5)
    written = send(api, Operation.TRANSFER, [Take(item["pk"], Decimal("2.5"))], location=scratch.other)
    assert quantity(scratch, item["pk"]) == Decimal("2.5")
    assert scratch.item(item["pk"])["location"] == scratch.parent
    moved = api.items(scratch.part, scratch.other)
    assert [Decimal(str(row["quantity"])) for row in moved] == [Decimal("2.5")]
    assert history(api, written)  # the marker is found, the notes stored whole


def test_a_built_transfer_does_not_merge_into_stock_at_the_destination(api: RawApi, scratch: Scratch) -> None:
    waiting = scratch.stock(4, location=scratch.other)
    moving = scratch.stock(3)
    send(api, Operation.TRANSFER, [Take(moving["pk"], Decimal(3))], location=scratch.other)
    assert quantity(scratch, waiting["pk"]) == 4
    assert scratch.item(moving["pk"])["location"] == scratch.other  # moved whole, still its own item


def test_built_remove_add_and_count_change_the_item_by_exactly_what_they_say(api: RawApi, scratch: Scratch) -> None:
    item = scratch.stock(10)
    removed = send(api, Operation.REMOVE, [Take(item["pk"], Decimal("1.25"))])
    assert quantity(scratch, item["pk"]) == Decimal("8.75")
    added = send(api, Operation.ADD, [Take(item["pk"], Decimal(3))])
    assert quantity(scratch, item["pk"]) == Decimal("11.75")
    counted = send(api, Operation.COUNT, [Take(item["pk"], Decimal(7))])
    assert quantity(scratch, item["pk"]) == 7
    for written in (removed, added, counted):
        assert history(api, written)


def test_a_built_request_acts_on_several_items_at_once(api: RawApi, scratch: Scratch) -> None:
    first, second = scratch.stock(2), scratch.stock(6)
    send(api, Operation.TRANSFER, [Take(first["pk"], Decimal(2)), Take(second["pk"], Decimal(1))], scratch.other)
    assert scratch.item(first["pk"])["location"] == scratch.other
    assert quantity(scratch, second["pk"]) == 5
