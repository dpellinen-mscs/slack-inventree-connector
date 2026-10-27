# SPDX-License-Identifier: MIT
"""Step 2 of the apply protocol against a reader: search, compare, and excuse only InvenTree's 404 (design §7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stockthread.apply import check_post_markers, depletes
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import FindingKind
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.write_request import write_notes
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, MarkedEntry, ServerInfo
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


OLD_API = DEPLETING_MARKER_FOUND_FROM - 1
"""A server whose search cannot find the marker of a removal that deleted its item (1.4.3)."""


class FakeReader:
    def __init__(
        self, entries: dict[Marker, list[int]] | None = None, gone: set[int] | None = None, api: int = OLD_API
    ) -> None:
        self.entries = entries or {}
        self.gone = gone or set()
        self.api = api
        self.searched: list[str] = []
        self.asked: list[int] = []

    async def server_info(self) -> ServerInfo:
        return ServerInfo(version="x", api_version=self.api)

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


def verified(store: Store, key: RequestKey, pks: list[int], held: str = "1", quantity: str | list[str] = "1") -> Marker:
    """A verified write of `quantity` on each item (taking it, for a remove), whose snapshot shows each item holding
    `held` before it."""
    store.transition(key, expected=S.PLANNED, target=S.APPROVED, actor=A.HUMAN)
    marker = store.next_marker(key, AttemptKind.APPLY)
    body: dict[str, Any] = {
        "items": [
            {"pk": pk, "quantity": each}
            for pk, each in zip(pks, [quantity] * len(pks) if isinstance(quantity, str) else quantity, strict=True)
        ],
        "notes": write_notes(marker, "U0SAM"),
    }
    snapshot = {"stock_items": {str(pk): held for pk in pks}}
    store.begin_attempt(
        key,
        AttemptKind.APPLY,
        expected=S.APPROVED,
        actor=A.APPLIER,
        snapshot=snapshot,
        request_body=body,
        lock_owner=OWNER,
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


@pytest.mark.parametrize("held", ["2", "1.5", "x"])
async def test_a_missing_marker_of_a_partial_removal_is_missing_even_if_its_items_are_gone(
    store: Store, held: str
) -> None:
    # A partial removal keeps its item and its entry: if the item 404s later, someone else deleted it, or this
    # database never had it. Only the snapshot showing the whole quantity taken explains a missing marker.
    marker = verified(store, KEY_1, [7, 9], held=held)
    findings = await check_post_markers(FakeReader(gone={7, 9}), store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]


@pytest.mark.parametrize(
    ("quantity", "gone", "excused"),
    [("0", {7, 9}, True), ("1", {7, 9}, False), ("0", {7}, False), (["0", "1"], {7, 9}, False)],
)
async def test_a_missing_marker_of_a_count_is_excused_only_when_it_counted_every_item_to_0_and_they_are_gone(
    tmp_path: Path, quantity: str | list[str], gone: set[int], excused: bool
) -> None:
    """A count to 0 deletes a delete_on_deplete item, whatever it held (`test_char_a_write_that_empties_an_item`)."""
    counts = Store.open(tmp_path / "counts.db")
    counts.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    counts.create_post(POST)
    counts.create_plan(POST, "0" * 64, (Operation.COUNT,))
    marker = verified(counts, KEY_1, [7, 9], held="4", quantity=quantity)
    findings = await check_post_markers(FakeReader(gone=gone), counts, POST)
    assert [(f.kind, f.marker) for f in findings] == ([] if excused else [(FindingKind.MISSING, marker)])
    newer = await check_post_markers(FakeReader(gone={7, 9}, api=DEPLETING_MARKER_FOUND_FROM), counts, POST)
    assert [(f.kind, f.marker) for f in newer] == [(FindingKind.MISSING, marker)]  # its search finds the entry
    counts.close()


@pytest.mark.parametrize("snapshot", [{}, {"stock_items": {"7": "1"}}, {"stock_items": {"7": 1, "9": 1}}, []])
def test_only_a_snapshot_of_every_item_held_whole_shows_depletion(snapshot: Any) -> None:
    lines = [(7, Decimal(1)), (9, Decimal(1))]
    assert depletes(lines, snapshot) is False
    assert depletes(lines, {"stock_items": {"7": "1.0", "9": "1"}}) is True
    assert depletes([], {"stock_items": {}}) is False


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


def test_attempt_lines_and_snapshot_read_what_was_stored(store: Store) -> None:
    marker = verified(store, KEY_1, [7, 9], held="3")
    assert store.attempt_lines(marker) == [(7, Decimal(1)), (9, Decimal(1))]
    assert store.attempt_snapshot(marker) == {"stock_items": {"7": "3", "9": "3"}}
    with pytest.raises(KeyError):
        store.attempt_lines(Marker(marker.marker_id, AttemptKind.UNDO, 1))


async def test_a_write_that_names_no_item_is_never_excused(store: Store) -> None:
    marker = verified(store, KEY_1, [])  # the builder never makes one; the excuse must not follow from all([])
    findings = await check_post_markers(FakeReader(), store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]


@pytest.mark.parametrize("api", [DEPLETING_MARKER_FOUND_FROM, DEPLETING_MARKER_FOUND_FROM + 10])
async def test_a_server_whose_search_finds_depletions_excuses_nothing(store: Store, api: int) -> None:
    # A 404 there may be an item this database never had (a restored backup, another instance).
    marker = verified(store, KEY_1, [7])
    reader = FakeReader(gone={7}, api=api)
    findings = await check_post_markers(reader, store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]
    assert reader.asked == []


async def test_only_a_removal_may_be_excused(store: Store) -> None:
    store.transition(KEY_1, expected=S.PLANNED, target=S.REJECTED, actor=A.HUMAN)
    marker = verified(store, KEY_2, [7])  # a transfer keeps its item
    reader = FakeReader(gone={7})
    findings = await check_post_markers(reader, store, POST)
    assert [(f.kind, f.marker) for f in findings] == [(FindingKind.MISSING, marker)]
    assert reader.asked == []


def undone(store: Store, key: RequestKey, pks: list[int], quantity: str = "1") -> Marker:
    verified(store, key, pks)
    marker = store.next_marker(key, AttemptKind.UNDO)
    body: dict[str, Any] = {
        "items": [{"pk": pk, "quantity": quantity} for pk in pks],
        "notes": write_notes(marker, "U0SAM"),
    }
    snapshot = {"stock_items": {str(pk): "1" for pk in pks}}
    store.begin_attempt(
        key,
        AttemptKind.UNDO,
        expected=S.VERIFIED,
        actor=A.HUMAN,
        snapshot=snapshot,
        request_body=body,
        lock_owner=OWNER,
    )
    store.transition(key, expected=S.UNDOING, target=S.UNDONE, actor=A.APPLIER)
    return marker


@pytest.mark.parametrize(("operation", "excused"), [(Operation.ADD, True), (Operation.REMOVE, False)])
async def test_an_undo_is_excused_only_when_it_removes(tmp_path: Path, operation: Operation, excused: bool) -> None:
    # The undo of an add is a remove and may deplete; the undo of a remove adds.
    opened = Store.open(tmp_path / "undo.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (operation,))
    apply_marker = Marker(opened.request(KEY_1).marker_id, AttemptKind.APPLY, 1)
    undo = undone(opened, KEY_1, [7])
    findings = await check_post_markers(FakeReader({apply_marker: [7]}, gone={7}), opened, POST)
    opened.close()
    assert [(f.kind, f.marker) for f in findings] == ([] if excused else [(FindingKind.MISSING, undo)])


@pytest.mark.parametrize(("quantity", "excused"), [("0", True), ("3", False)])
async def test_an_undo_of_a_count_is_excused_only_when_it_counts_every_item_to_0(
    tmp_path: Path, quantity: str, excused: bool
) -> None:
    """The undo of a count counts back; only a count back to 0 may delete the item (review of #115)."""
    opened = Store.open(tmp_path / "undo-count.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.create_post(POST)
    opened.create_plan(POST, "0" * 64, (Operation.COUNT,))
    apply_marker = Marker(opened.request(KEY_1).marker_id, AttemptKind.APPLY, 1)
    undo = undone(opened, KEY_1, [7], quantity=quantity)
    findings = await check_post_markers(FakeReader({apply_marker: [7]}, gone={7}), opened, POST)
    opened.close()
    assert [(f.kind, f.marker) for f in findings] == ([] if excused else [(FindingKind.MISSING, undo)])
