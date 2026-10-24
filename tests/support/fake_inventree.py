"""A fake InvenTree for the apply protocol: it moves a whole stock item the way InvenTree does
(`test_char_history_deltas`), and can fail, time out or land a write late on request."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anyio
from views import SOURCE

from stockthread.core.marker import marked
from stockthread.core.write_request import WriteRequest
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, MarkedEntry, ServerInfo
from stockthread.writer import Outcome, Sent

ITEM = 57
"""The one stock item the fake starts with: part 1 at location `SOURCE`."""


class FakeInvenTree:
    """Stock items by pk as [part, location, quantity], and history entries as (pk, item, notes, deltas, type)."""

    def __init__(self, held: str = "2") -> None:
        self.items: dict[int, list[Any]] = {ITEM: [1, SOURCE, Decimal(held)]}
        self.entries: list[tuple[int, int | None, str, dict[str, Any], int]] = [(570, ITEM, "", {}, 1)]
        self.sent: list[WriteRequest] = []
        self.answer = Outcome.ACCEPTED
        self.raises: BaseException | None = None
        self.moves = True
        self.lands = False
        """With an answer other than accepted: the write ran all the same."""
        self.with_entry = True
        """False: the write leaves no history entry (1.5.6 with PostgreSQL, `test_char_overlong_notes_...`)."""
        self.before_write: Callable[[], None] | None = None
        self.after_write: Callable[[], None] | None = None
        self.fail_reads = False
        self.fail_reads_after_write = False
        self.history_fails: set[int] = set()
        self.fail_stock_reads = False
        self.lock: anyio.Lock | None = None
        self.units: dict[int, str] = {}
        """Each part's `Part.units`; a part not here has none ("")."""
        self.units_read: list[int] = []

    def write_entry(self, item: int, notes: str, deltas: dict[str, Any], kind: int) -> None:
        self.entries.append((1000 + len(self.entries), item, notes, deltas, kind))

    def _read(self) -> None:
        if self.fail_reads or (self.fail_reads_after_write and self.sent):
            raise InvenTreeError("GET /api/stock/ failed: ConnectError")

    async def server_info(self) -> ServerInfo:
        self._read()
        return ServerInfo(version="1.5.6", api_version=DEPLETING_MARKER_FOUND_FROM)

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]:
        self._read()
        return [
            MarkedEntry(pk, item, found, deltas, kind)
            for pk, item, notes, deltas, kind in self.entries
            if (found := marked(notes, marker_id)) is not None
        ]

    async def stock_item_gone(self, pk: int) -> bool:
        self._read()
        return pk not in self.items

    async def item_entries(self, pk: int) -> list[int]:
        self._read()
        if pk not in self.items or pk in self.history_fails:  # InvenTree answers 400 for an item it deleted
            raise InvenTreeError(f"GET /api/stock/track/: 400 for item {pk}")
        return [entry for entry, item, *_ in self.entries if item == pk]

    async def part_units(self, pk: int) -> str:
        self._read()
        self.units_read.append(pk)
        return self.units.get(pk, "")

    async def stock_items(self, part: int, location: int) -> list[dict[str, Any]]:
        self._read()
        if self.fail_stock_reads:
            raise InvenTreeError("GET /api/stock/ failed: ConnectError")
        return [
            {"pk": pk, "part": part, "location": location, "quantity": str(quantity), "status": 10, "allocated": 0}
            for pk, (on, at, quantity) in sorted(self.items.items())
            if (on, at) == (part, location)
        ]

    async def send(self, request: WriteRequest) -> Sent:
        assert self.lock is not None
        assert self.lock.locked(), "a write is sent under the writer lock"
        self.sent.append(request)
        if self.raises is not None:
            raise self.raises
        if self.answer is not Outcome.ACCEPTED and not self.lands:
            return Sent(self.answer, f"POST {request.path}: {self.answer}")
        if self.before_write is not None:
            self.before_write()
        if self.moves:
            for line in request.body["items"]:
                stock = self.items[int(line["pk"])]
                stock[1] = request.body["location"]
                if self.with_entry:
                    self.write_entry(
                        int(line["pk"]),
                        str(request.body["notes"]),
                        {"location": request.body["location"], "quantity": float(stock[2])},
                        20,
                    )
        if self.after_write is not None:
            self.after_write()
        if self.answer is not Outcome.ACCEPTED:  # it ran, but the answer did not say so (a timeout, a 5xx)
            return Sent(self.answer, f"POST {request.path}: {self.answer}")
        return Sent(Outcome.ACCEPTED, f"POST {request.path}: 201", 201)
