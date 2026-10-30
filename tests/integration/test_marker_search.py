# SPDX-License-Identifier: MIT
"""The marker search of the apply protocol (design section 7, step 2) against a real server."""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from stockthread import inventree
from stockthread.apply import check_post_markers
from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.marker_check import FindingKind
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.resolve import Take
from stockthread.core.write_request import build_request
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.store import Store

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


async def test_step_two_excuses_a_depleting_write_only_by_inventrees_404(
    api: RawApi, scratch: Scratch, tmp_path: Path
) -> None:
    """A verified remove that deleted its item: found by search on 1.5.6, excused by the item's 404 on 1.4.3
    (design section 5, Depletion). A verified write that never reached InvenTree is missing on both."""
    s, a = RequestState, Actor
    store = Store.open(tmp_path / "stockthread.db")
    store.acquire_lock("it", stale_after=timedelta(seconds=60))

    def verified(post: PostKey, item: int, quantity: int, *, held: int, sent: bool) -> Marker:
        store.create_post(post)
        store.create_plan(post, "0" * 64, (Operation.REMOVE,))
        key = RequestKey(post, 1, 1)
        store.transition(key, expected=s.PLANNED, target=s.APPROVED, actor=a.HUMAN)
        marker = store.next_marker(key, AttemptKind.APPLY)
        request = build_request(Operation.REMOVE, [Take(item, Decimal(quantity))], marker=marker, user="U0SAM0001")
        store.begin_attempt(
            key,
            AttemptKind.APPLY,
            expected=s.APPROVED,
            actor=a.APPLIER,
            snapshot={"stock_items": {str(item): str(held)}},
            request_body=request.body,
            lock_owner="it",
        )
        if sent:
            response = api.post(request.path, dict(request.body))
            assert response.status_code == 201, response.text[:500]
        store.transition(key, expected=s.APPLYING, target=s.APPLIED, actor=a.APPLIER)
        store.transition(key, expected=s.APPLIED, target=s.VERIFIED, actor=a.APPLIER)
        return marker

    depleted = scratch.stock(3, delete_on_deplete=True)
    kept = scratch.stock(3)
    depleting = PostKey("T0ACME", "C0STOCK", "1727600000.000100")
    never_sent = PostKey("T0ACME", "C0STOCK", "1727600000.000200")
    verified(depleting, depleted["pk"], 3, held=3, sent=True)
    unsent = verified(never_sent, kept["pk"], 1, held=3, sent=False)

    async with make_reader() as reader:
        assert await reader.stock_item_gone(depleted["pk"]) is True
        assert await reader.stock_item_gone(kept["pk"]) is False
        assert await check_post_markers(reader, store, depleting) == []
        findings = await check_post_markers(reader, store, never_sent)
    store.close()
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, unsent)]


async def test_char_a_search_that_grows_between_pages_repeats_a_row_and_is_read_again(
    api: RawApi, scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history list comes newest first, so an entry written between two pages of a search pushes the last row
    of the first page onto the second: it comes back twice, and the count grows. The reader never returns that list
    (`InvenTreeReader.list_all`; #220 item 4): it reads the whole list again, and refuses one that changes on every
    read (`MAX_LIST_READS`). Pages of 2 here; the reader's are 100."""
    item = scratch.stock(10)
    marker_id = new_marker_id()
    for attempt in (1, 2, 3):
        send(api, Operation.REMOVE, [Take(item["pk"], Decimal(1))], Marker(marker_id, AttemptKind.APPLY, attempt))

    def write_one_more() -> None:
        quoted = f"see [sic:{marker_id}:a09]"  # matched by the search, though not one of ours
        response = api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 1}], "notes": quoted})
        assert response.status_code == 201, response.text[:500]

    first = api.get("/api/stock/track/", search=marker_id, limit=2, offset=0)
    write_one_more()
    second = api.get("/api/stock/track/", search=marker_id, limit=2, offset=2)
    assert (first["count"], second["count"]) == (3, 4)
    assert second["results"][0]["pk"] == first["results"][-1]["pk"]

    class Interleaved(httpx.AsyncBaseTransport):
        """The real server, with one more matching entry written after each page in `after` is read."""

        def __init__(self, after: set[int]) -> None:
            self.inner = httpx.AsyncHTTPTransport()
            self.after = after
            self.pages = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/api/stock/track/":
                self.pages += 1
                if self.pages in self.after:
                    await response.aread()
                    write_one_more()
            return response

    monkeypatch.setattr(inventree, "PAGE_SIZE", 2)
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    settings = InvenTreeSettings(url=os.environ["STOCKTHREAD_IT_URL"])
    once = Interleaved(after={1})
    async with InvenTreeReader(settings, SecretStr(token), transport=once) as reader:
        assert len(await reader.marker_entries(marker_id)) == 3  # all three, from the second, whole read
    assert once.pages == 2 + 3  # refused on page 2; then 5 entries (ours and two quotes) in 3 pages

    # A write after the first page of every read: 3 more entries, so 8 in 4 pages by the last read.
    always = Interleaved(after={1, 3, 5})
    async with InvenTreeReader(settings, SecretStr(token), transport=always) as reader:
        with pytest.raises(InvenTreeError, match="the list changed while it was read"):
            await reader.marker_entries(marker_id)
        assert len(await reader.marker_entries(marker_id)) == 3  # read again once it holds still: all three
    assert always.pages == 3 * 2 + 4
