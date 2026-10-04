"""Step 2 of the apply protocol against a reader: search, compare, and excuse only InvenTree's 404 (design §7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from stockthread.apply import check_post_markers
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import FindingKind
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import write_notes
from stockthread.inventree import InvenTreeError, MarkedEntry
from stockthread.store import Store

pytestmark = pytest.mark.anyio

S, A = RequestState, Actor
POST = PostKey("T0ACME", "C0STOCK", "1727600000.000100")
OWNER = "test-instance"
KEY_1, KEY_2 = RequestKey(POST, 1, 1), RequestKey(POST, 1, 2)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (Operation.REMOVE, Operation.TRANSFER))
    yield opened
    opened.close()


class FakeReader:
    def __init__(self, entries: dict[Marker, list[int]] | None = None, gone: set[int] | None = None) -> None:
        self.entries = entries or {}
        self.gone = gone or set()
        self.searched: list[str] = []
        self.asked: list[int] = []

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]:
        self.searched.append(marker_id)
        return [
            MarkedEntry(100 + n, item, marker, {})
            for marker, items in self.entries.items()
            if marker.marker_id == marker_id
            for n, item in enumerate(items)
        ]

    async def stock_item_gone(self, pk: int) -> bool:
        self.asked.append(pk)
        return pk in self.gone


def verified(store: Store, key: RequestKey, pks: list[int]) -> Marker:
    store.transition(key, expected=S.PLANNED, target=S.APPROVED, actor=A.HUMAN)
    marker = store.next_marker(key, AttemptKind.APPLY)
    body: dict[str, Any] = {"items": [{"pk": pk, "quantity": "1"} for pk in pks], "notes": write_notes(marker, "U0SAM")}
    store.begin_attempt(
        key, AttemptKind.APPLY, expected=S.APPROVED, actor=A.APPLIER, snapshot={}, request_body=body, lock_owner=OWNER
    )
    store.transition(key, expected=S.APPLYING, target=S.APPLIED, actor=A.APPLIER)
    store.transition(key, expected=S.APPLIED, target=S.VERIFIED, actor=A.APPLIER)
    return marker


async def test_every_request_of_the_post_is_searched_and_a_clear_history_has_no_findings(store: Store) -> None:
    marker = verified(store, KEY_1, [7])
    reader = FakeReader({marker: [7]})
    assert await check_post_markers(reader, store, POST) == []
    assert reader.searched == [store.request(KEY_1).marker_id, store.request(KEY_2).marker_id]
    assert reader.asked == []


async def test_a_hit_of_a_request_that_never_ran_is_found(store: Store) -> None:
    verified(store, KEY_1, [7])
    late = Marker(store.request(KEY_2).marker_id, AttemptKind.APPLY, 1)
    reader = FakeReader({Marker(store.request(KEY_1).marker_id, AttemptKind.APPLY, 1): [7], late: [8]})
    findings = await check_post_markers(reader, store, POST)
    assert [(f.key, f.kind, f.marker) for f in findings] == [(KEY_2, FindingKind.HIT, late)]


async def test_a_missing_marker_is_excused_when_every_item_of_its_write_is_gone(store: Store) -> None:
    verified(store, KEY_1, [7, 9])
    reader = FakeReader(gone={7, 9})
    assert await check_post_markers(reader, store, POST) == []
    assert reader.asked == [7, 9]


@pytest.mark.parametrize("gone", [set(), {7}, {9}])
async def test_a_missing_marker_with_an_item_still_there_is_missing(store: Store, gone: set[int]) -> None:
    marker = verified(store, KEY_1, [7, 9])
    findings = await check_post_markers(FakeReader(gone=gone), store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]


async def test_gone_items_do_not_excuse_a_hit(store: Store) -> None:
    verified(store, KEY_1, [7])
    late = Marker(store.request(KEY_2).marker_id, AttemptKind.APPLY, 1)
    reader = FakeReader({Marker(store.request(KEY_1).marker_id, AttemptKind.APPLY, 1): [7], late: [8]}, gone={7, 8})
    findings = await check_post_markers(reader, store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.HIT, late)]
    assert reader.asked == []


async def test_a_read_error_propagates(store: Store) -> None:
    verified(store, KEY_1, [7])

    class Failing(FakeReader):
        async def stock_item_gone(self, pk: int) -> bool:
            raise InvenTreeError("GET /api/stock/7/ returned a 404 that is not InvenTree's")

    with pytest.raises(InvenTreeError):
        await check_post_markers(Failing(), store, POST)


def test_attempt_items_reads_the_stored_body(store: Store) -> None:
    marker = verified(store, KEY_1, [7, 9])
    assert store.attempt_items(marker) == [7, 9]
    with pytest.raises(KeyError):
        store.attempt_items(Marker(marker.marker_id, AttemptKind.UNDO, 1))


async def test_a_write_that_names_no_item_is_never_excused(store: Store) -> None:
    marker = verified(store, KEY_1, [])  # the builder never makes one; the excuse must not follow from all([])
    findings = await check_post_markers(FakeReader(), store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]
