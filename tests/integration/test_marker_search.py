"""The marker search of the apply protocol (design section 7, step 2) against a real server."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import build_request
from stockthread.inventree import InvenTreeReader

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

pytestmark = pytest.mark.anyio

LINK = "https://acme-fiber.slack.com/archives/C0ACME0001/p1700000000000100"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_reader() -> InvenTreeReader:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    return InvenTreeReader(InvenTreeSettings(url=os.environ["STOCKTHREAD_IT_URL"]), SecretStr(token))


def send(api: RawApi, operation: Operation, takes: list[Take], marker: Marker) -> None:
    request = build_request(operation, takes, marker=marker, user="U0SAM0001", link=LINK)
    response = api.post(request.path, dict(request.body))
    assert response.status_code == 201, response.text[:500]


async def test_finds_every_attempt_of_one_request_and_nothing_that_only_quotes_it(
    api: RawApi, scratch: Scratch
) -> None:
    item = scratch.stock(10)
    marker_id = new_marker_id()
    send(api, Operation.REMOVE, [Take(item["pk"], Decimal(2))], Marker(marker_id, AttemptKind.APPLY, 1))
    send(api, Operation.ADD, [Take(item["pk"], Decimal(2))], Marker(marker_id, AttemptKind.UNDO, 1))
    send(api, Operation.REMOVE, [Take(item["pk"], Decimal(1))], Marker(new_marker_id(), AttemptKind.APPLY, 1))
    quoted = f"see [sic:{marker_id}:a01]"  # an entry the server search returns, but not one of ours
    response = api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 1}], "notes": quoted})
    assert response.status_code == 201, response.text[:500]

    async with make_reader() as reader:
        found = await reader.marker_entries(marker_id)
        nothing = await reader.marker_entries(new_marker_id())

    assert sorted((entry.marker.kind, entry.item) for entry in found) == [
        (AttemptKind.APPLY, item["pk"]),
        (AttemptKind.UNDO, item["pk"]),
    ]
    assert all(entry.marker.marker_id == marker_id for entry in found)
    assert len({entry.pk for entry in found}) == 2
    assert nothing == []


async def test_char_a_later_split_does_not_copy_the_marked_entry(api: RawApi, scratch: Scratch) -> None:
    """A split of a marked item after the write leaves the marker on the item it was written to only.

    Measured so a marker found on another item can be treated as unexplained (design section 7, "The marker").
    """
    item = scratch.stock(10)
    marker_id = new_marker_id()
    send(api, Operation.REMOVE, [Take(item["pk"], Decimal(2))], Marker(marker_id, AttemptKind.APPLY, 1))
    response = api.post(
        "/api/stock/transfer/", {"items": [{"pk": item["pk"], "quantity": 3}], "location": scratch.other, "notes": "s"}
    )
    assert response.status_code == 201, response.text[:500]
    async with make_reader() as reader:
        found = await reader.marker_entries(marker_id)
    assert [entry.item for entry in found] == [item["pk"]]
