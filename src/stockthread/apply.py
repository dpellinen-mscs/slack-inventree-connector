"""The apply protocol (design section 7, "Apply protocol"), one step at a time.

Built so far: step 2, the marker check of a whole post against InvenTree.
"""

from __future__ import annotations

from typing import Protocol

from stockthread.core.marker import Marker
from stockthread.core.marker_check import Finding, FindingKind, check_markers
from stockthread.core.model import PostKey
from stockthread.inventree import MarkedEntry
from stockthread.store import Store


class HistoryReader(Protocol):
    """What step 2 reads from InvenTree (`InvenTreeReader`)."""

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]: ...

    async def stock_item_gone(self, pk: int) -> bool: ...


async def check_post_markers(reader: HistoryReader, store: Store, post: PostKey) -> list[Finding]:
    """Step 2: search the history for the marker id of every request of the post and compare the hits with the
    recorded states (`core.marker_check`). Empty means the history agrees with the store.

    A marker that should be there but is not is excused only when every stock item its stored body names answers
    InvenTree's 404 (design section 5, Depletion: on 1.4.3 the write that deleted an item cannot be found by search).
    One item still there would carry the entry, so it is not excused. Read errors propagate: a check that could not
    read proves nothing, and the caller must not write."""
    recorded = store.recorded_requests(post)
    found: dict[str, list[Marker]] = {}
    for request in recorded:
        found[request.marker_id] = [entry.marker for entry in await reader.marker_entries(request.marker_id)]
    findings = check_markers(recorded, found)
    gone = [
        finding.marker
        for finding in findings
        if finding.kind is FindingKind.MISSING and await _items_gone(reader, store.attempt_items(finding.marker))
    ]
    return check_markers(recorded, found, item_gone=gone) if gone else findings


async def _items_gone(reader: HistoryReader, pks: list[int]) -> bool:
    if not pks:
        return False
    for pk in pks:
        if not await reader.stock_item_gone(pk):
            return False
    return True
