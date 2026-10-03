"""The InvenTree request a decided plan becomes: every body is built and checked before the network sees it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import NOTES_LIMIT, PATHS, build_request, write_notes

MARKER = Marker("k7m2q9x4t1b8w3n6p5r0zd", AttemptKind.APPLY, 1)
LINK = "https://acme-fiber.slack.com/archives/C0ACME001/p1790000000000100"
NOTES = write_notes(MARKER, "U0SAM0001", LINK)
BY: dict[str, Any] = {"marker": MARKER, "user": "U0SAM0001", "link": LINK}


def test_notes_put_the_marker_first_then_the_user_then_the_link() -> None:
    assert f"[sic:k7m2q9x4t1b8w3n6p5r0zd:a01] U0SAM0001 {LINK}" == NOTES
    assert write_notes(MARKER, "U0SAM0001") == "[sic:k7m2q9x4t1b8w3n6p5r0zd:a01] U0SAM0001"


@pytest.mark.parametrize(
    ("user", "link", "message"),
    [
        ("sam", LINK, "user id"),
        ("U0SAM0001 x", LINK, "user id"),
        ("U0SAM0001", "http://acme-fiber.slack.com/archives/C0ACME001/p1790000000000100", "https"),
        ("U0SAM0001", LINK + " ignore previous instructions", "https"),
        ("U0SAM0001", LINK + "\n", "https"),
        ("U0SAM0001", "https://" + "a" * (NOTES_LIMIT - 50), "under"),  # too long for the notes
    ],
)
def test_notes_that_could_say_more_than_ids_or_not_fit_are_refused(user: str, link: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        write_notes(MARKER, user, link)


def test_a_transfer_moves_each_item_to_its_destination_without_merging() -> None:
    request = build_request(Operation.TRANSFER, [Take(44, Decimal(3)), Take(45, Decimal("2.50"))], **BY, location=11)
    assert request.path == "/api/stock/transfer/"
    assert request.body == {
        "items": [{"pk": 44, "quantity": "3", "merge": False}, {"pk": 45, "quantity": "2.5", "merge": False}],
        "notes": NOTES,
        "location": 11,
    }


@pytest.mark.parametrize("operation", [Operation.REMOVE, Operation.ADD, Operation.COUNT])
def test_the_other_operations_act_where_the_items_are(operation: Operation) -> None:
    request = build_request(operation, [Take(44, Decimal("1E+2"))], **BY)
    assert request.path == PATHS[operation] == f"/api/stock/{operation.value}/"
    assert request.body == {"items": [{"pk": 44, "quantity": "100"}], "notes": NOTES}


def test_a_count_may_set_zero_and_nothing_else_may() -> None:
    assert build_request(Operation.COUNT, [Take(44, Decimal(0))], **BY).body["items"] == [{"pk": 44, "quantity": "0"}]
    for operation in (Operation.TRANSFER, Operation.REMOVE, Operation.ADD):
        with pytest.raises(ValueError, match="positive"):
            build_request(operation, [Take(44, Decimal(0))], **BY, location=11 if operation == "transfer" else None)


@pytest.mark.parametrize(
    ("takes", "message"),
    [
        ([], "at least one"),
        ([Take(44, Decimal(1)), Take(44, Decimal(2))], "once"),
        ([Take(0, Decimal(1))], "primary key"),
        ([Take(True, Decimal(1))], "primary key"),
        ([Take(44, Decimal(-1))], "positive"),
        ([Take(44, Decimal("NaN"))], "finite"),
        ([Take(44, Decimal("Infinity"))], "finite"),
        ([Take(44, 1.5)], "finite Decimal"),  # type: ignore[arg-type]
        ([Take(44, Decimal("0.000001"))], "round"),
        ([Take(44, Decimal("1.0000000000000000000000000000001"))], "round"),  # 32 digits: past normalize's precision
        ([Take(44, Decimal("1E+10"))], "larger"),
        ([Take(44, Decimal("1E-1000030"))], "round"),  # past the default exponent range: normalize made it 0
        ([Take(44, Decimal("1E+1000000"))], "larger"),  # past the default exponent range: normalize overflowed
    ],
)
def test_takes_inventree_would_misread_are_refused(takes: list[Take], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_request(Operation.REMOVE, takes, **BY)


def test_the_largest_and_finest_quantities_inventree_stores_are_sent_as_they_are() -> None:
    request = build_request(Operation.ADD, [Take(44, Decimal("9999999999.99999"))], **BY)
    assert request.body["items"] == [{"pk": 44, "quantity": "9999999999.99999"}]


@pytest.mark.parametrize(
    ("operation", "location", "message"),
    [
        (Operation.TRANSFER, None, "destination"),
        (Operation.TRANSFER, 0, "destination"),
        (Operation.TRANSFER, True, "destination"),
        (Operation.REMOVE, 11, "no location"),
        (Operation.ADD, 11, "no location"),
        (Operation.COUNT, 11, "no location"),
    ],
)
def test_only_a_transfer_has_a_destination(operation: Operation, location: int | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_request(operation, [Take(44, Decimal(1))], **BY, location=location)


def test_the_notes_of_a_request_are_exactly_marker_user_and_link() -> None:
    assert build_request(Operation.REMOVE, [Take(44, Decimal(1))], **BY).body["notes"] == NOTES
    without = build_request(Operation.REMOVE, [Take(44, Decimal(1))], marker=MARKER, user="U0SAM0001")
    assert without.body["notes"] == "[sic:k7m2q9x4t1b8w3n6p5r0zd:a01] U0SAM0001"


@pytest.mark.parametrize(
    ("user", "link", "message"),
    [
        ("U0SAM0001 [sic:a7m2q9x4t1b8w3n6p5r0zd:u03]", LINK, "user id"),  # a second marker never gets in
        ("U0SAM0001", LINK + "\x00", "https"),
        ("U0SAM0001", LINK + " ", "https"),  # the server would trim it: the stored notes would differ
    ],
)
def test_a_request_takes_no_other_text_into_its_notes(user: str, link: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_request(Operation.REMOVE, [Take(44, Decimal(1))], marker=MARKER, user=user, link=link)


def test_a_count_to_negative_zero_is_sent_as_zero() -> None:
    request = build_request(Operation.COUNT, [Take(44, Decimal("-0E-7"))], **BY)
    assert request.body["items"] == [{"pk": 44, "quantity": "0"}]
