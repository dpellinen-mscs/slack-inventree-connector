"""The apply protocol (design section 7, "Apply protocol"), one step at a time.

Built so far: step 2, the marker check of a whole post against InvenTree.
"""

from __future__ import annotations

from typing import Protocol

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
    below `DEPLETING_MARKER_FOUND_FROM`, whose stock items all answer InvenTree's own 404. Any other write, or a
    newer server, has no excuse: an item that 404s there may never have existed in this database (a restored backup,
    another instance), which is what a missing marker must catch. Read errors propagate: a check that could not
    read proves nothing, and the caller must not write."""
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
    gone = [marker for marker in candidates if await _items_gone(reader, store.attempt_items(marker))]
    return check_markers(recorded, found, item_gone=gone) if gone else findings


def _removes(operation: Operation, kind: AttemptKind) -> bool:
    """True for a write that takes stock away, and so may delete an item on depletion."""
    return operation is (Operation.REMOVE if kind is AttemptKind.APPLY else Operation.ADD)


async def _items_gone(reader: HistoryReader, pks: list[int]) -> bool:
    if not pks:
        return False
    for pk in pks:
        if not await reader.stock_item_gone(pk):
            return False
    return True
