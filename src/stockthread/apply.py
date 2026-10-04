"""The apply protocol (design section 7, "Apply protocol"), one step at a time.

Built so far: step 2, the marker check of a whole post against InvenTree.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import Finding, FindingKind, check_markers
from stockthread.core.model import Operation, PostKey
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, MarkedEntry, ServerInfo
from stockthread.store import Store


class HistoryReader(Protocol):
    """What step 2 reads from InvenTree (`InvenTreeReader`)."""

    async def server_info(self) -> ServerInfo: ...

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]: ...

    async def stock_item_gone(self, pk: int) -> bool: ...


async def check_post_markers(reader: HistoryReader, store: Store, post: PostKey) -> list[Finding]:
    """Step 2: search the history for the marker id of every request of the post and compare the hits with the
    recorded states (`core.marker_check`). Empty means the history agrees with the store.

    A marker that should be there but is not is excused only for the one write search is measured not to find
    (design section 5, Depletion): a removal of stock (the apply of a remove, or the undo of an add), on a server
    below `DEPLETING_MARKER_FOUND_FROM`, that the snapshot stored with the attempt shows taking the whole quantity
    of every item it named, and whose stock items all answer InvenTree's own 404 now. Any other write, or a newer
    server, has no excuse: an item that 404s there may never have existed in this database (a restored backup,
    another instance), which is what a missing marker must catch. A partial removal keeps its item and its entry,
    so its item answering 404 later (deleted by someone else) excuses nothing either. Read errors propagate: a
    check that could not read proves nothing, and the caller must not write.
    """
    recorded = store.recorded_requests(post)
    found: dict[str, list[Marker]] = {}
    for request in recorded:
        found[request.marker_id] = [entry.marker for entry in await reader.marker_entries(request.marker_id)]
    findings = check_markers(recorded, found)
    operations = {request.key: request.operation for request in recorded}
    candidates = [
        finding.marker
        for finding in findings
        if finding.kind is FindingKind.MISSING and _removes(operations[finding.key], finding.marker.kind)
    ]
    if not candidates or (await reader.server_info()).api_version >= DEPLETING_MARKER_FOUND_FROM:
        return findings
    gone = [
        marker
        for marker in candidates
        if (lines := store.attempt_lines(marker))
        and depletes(lines, store.attempt_snapshot(marker))
        and await _items_gone(reader, [pk for pk, _ in lines])
    ]
    return check_markers(recorded, found, item_gone=gone) if gone else findings


def _removes(operation: Operation, kind: AttemptKind) -> bool:
    """True for a write that takes stock away, and so may delete an item on depletion."""
    return operation is (Operation.REMOVE if kind is AttemptKind.APPLY else Operation.ADD)


def depletes(lines: list[tuple[int, Decimal]], snapshot: Any) -> bool:
    """True if `snapshot` (step 3: `{"stock_items": {"<pk>": "<quantity>"}}`, what each source item held just
    before the write) shows every line taking its item's whole quantity. Anything else, or a snapshot without the
    item, is False: only an expected depletion can explain a missing marker."""
    held = snapshot.get("stock_items") if isinstance(snapshot, dict) else None
    if not lines or not isinstance(held, dict):
        return False
    for pk, quantity in lines:
        value = held.get(str(pk))
        if not isinstance(value, str):
            return False
        try:
            if Decimal(value) != quantity:
                return False
        except InvalidOperation:
            return False
    return True


async def _items_gone(reader: HistoryReader, pks: list[int]) -> bool:
    for pk in pks:
        if not await reader.stock_item_gone(pk):
            return False
    return True
