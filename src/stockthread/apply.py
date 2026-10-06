"""The apply protocol (design section 7, "Apply protocol"): `apply_request` runs steps 1 to 6 for one request, and
`check_post_markers` is step 2, the marker check of a whole post against InvenTree.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import anyio

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import Finding, FindingKind, check_markers
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import Actor, RequestState
from stockthread.core.resolve import StockItem, stock_items
from stockthread.core.write_request import WriteRequest
from stockthread.gate import Judgement
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, MarkedEntry, ServerInfo
from stockthread.precondition import Pair, Snapshot, Stale, check, pairs
from stockthread.readback import created, verify
from stockthread.slack.replies import MovementView
from stockthread.store import Store
from stockthread.writer import Outcome, Sent, WriteRefused


class HistoryReader(Protocol):
    """What step 2 reads from InvenTree (`InvenTreeReader`)."""

    async def server_info(self) -> ServerInfo: ...

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]: ...

    async def stock_item_gone(self, pk: int) -> bool: ...


async def check_post_markers(reader: HistoryReader, store: Store, post: PostKey) -> list[Finding]:
    """Step 2: search the history for the marker id of every request of the post and compare the hits with the
    recorded states (`core.marker_check`). Empty means the history agrees with the store.

    A marker that should be there but is not is excused only for the one write search is measured not to find
    (design section 5, Depletion): on a server below `DEPLETING_MARKER_FOUND_FROM`, a write that left every item it
    named at 0, whose stock items all answer InvenTree's own 404 now. That is a removal of stock (the apply of a
    remove, or the undo of an add) that the snapshot stored with the attempt shows taking the whole quantity of
    every item, or a count of every item to 0. Any other write, or a newer
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
        (finding.marker, operations[finding.key])
        for finding in findings
        if finding.kind is FindingKind.MISSING and _may_empty(operations[finding.key], finding.marker.kind)
    ]
    if not candidates or (await reader.server_info()).api_version >= DEPLETING_MARKER_FOUND_FROM:
        return findings
    gone = [
        marker
        for marker, operation in candidates
        if (lines := store.attempt_lines(marker))
        and _empties_all(operation, lines, store.attempt_snapshot(marker))
        and await _items_gone(reader, [pk for pk, _ in lines])
    ]
    return check_markers(recorded, found, item_gone=gone) if gone else findings


def _may_empty(operation: Operation, kind: AttemptKind) -> bool:
    """True for a write that may leave an item at 0, and so delete it on depletion: one that takes stock away, or a
    count."""
    return operation is Operation.COUNT or operation is (
        Operation.REMOVE if kind is AttemptKind.APPLY else Operation.ADD
    )


def _empties_all(operation: Operation, lines: list[tuple[int, Decimal]], snapshot: Any) -> bool:
    """True if the write leaves every item it names at 0: a count of each to 0 (measured to delete a delete_on_deplete
    item, `test_char_a_write_that_empties_an_item`), or a removal of each item's whole quantity (`depletes`)."""
    if operation is Operation.COUNT:
        return all(quantity == 0 for _, quantity in lines)
    return depletes(lines, snapshot)


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


class ApplyReader(HistoryReader, Protocol):
    """What the whole protocol reads from InvenTree (`InvenTreeReader`)."""

    async def item_entries(self, pk: int) -> list[int]: ...

    async def stock_items(self, part: int, location: int) -> list[dict[str, Any]]: ...


class Sender(Protocol):
    """What step 4 writes with (`InvenTreeWriter`)."""

    async def send(self, request: WriteRequest) -> Sent: ...


@dataclass(frozen=True)
class Applied:
    """Where one run of `apply_request` left the request."""

    state: RequestState
    why: str
    """One line for the trail and the log: no message text, no token."""
    problems: tuple[str, ...] = ()
    """What the marker check or the read-back found, when the request is `verify_failed`."""


async def apply_request(
    key: RequestKey,
    view: MovementView,
    rejudge: Callable[[Marker], Judgement],
    *,
    store: Store,
    reader: ApplyReader,
    writer: Sender,
    lock: anyio.Lock,
    lock_owner: str,
    chosen: Collection[int] = (),
) -> Applied:
    """Run the apply protocol (design section 7) once for the `approved` request `key`, whose movement is `view`.

    `rejudge(marker)` judges the request's whole plan again now (`gate.judge`), with `marker` as this request's;
    the request sent is its `requests[key.sequence - 1]`. `chosen`: the lines (from 0) a person named the stock items
    for (`precondition.check`). `lock` is the process's one writer lock: steps 2 to 6 run under it, so the connector
    never has two writes in flight.

    Each outcome is a state of the request: still `approved` when the gate blocks it now (nothing read or
    written), `verify_failed` when the history already disagrees with the store, `planned` when the stock changed
    since the plan, `failed` when InvenTree refused the write (or the writer refused to send it), `unknown` when the
    outcome is not known (recovery's), `verified` or `verify_failed` after the read-back. A read error before the
    write leaves the request `approved` and raises; one in the read-back leaves it `applied` (not read back, which
    blocks every other write of the post) and raises. Nothing is written twice: `begin_attempt` refuses a second
    attempt of a write that was not refused.
    """
    async with lock:
        row = store.request(key)
        if row.state is not RequestState.APPROVED:
            raise ValueError(f"request {key} is {row.state}, not approved")
        # Step 1: the gate again, with the marker the attempt will carry.
        marker = store.next_marker(key, AttemptKind.APPLY)
        judgement = rejudge(marker)
        if not judgement.allowed:
            return Applied(RequestState.APPROVED, f"not written: {judgement.why}")
        if len(judgement.requests) < key.sequence:
            raise ValueError(f"the plan has no request {key.sequence}")
        request = judgement.requests[key.sequence - 1]
        if request.operation is not row.operation:
            raise ValueError(f"request {key} is a {row.operation}, the plan builds a {request.operation}")
        # Step 2: the history must agree with what the store recorded for every request of the post.
        if findings := await check_post_markers(reader, store, key.post):
            store.transition(
                key, expected=RequestState.APPROVED, target=RequestState.VERIFY_FAILED, actor=Actor.APPLIER
            )
            problems = tuple(f"{finding.marker}: {finding.why}" for finding in findings)
            return Applied(RequestState.VERIFY_FAILED, "the stock history disagrees with the records", problems)
        # Step 3: the history of each named item first, then the stock, so an entry written between the two reads
        # is one the read-back reports, never one it misses.
        named = sorted({int(line["pk"]) for line in request.body["items"]})
        before = await _histories(reader, named)
        snapshot = (
            before
            if isinstance(before, Stale)
            else check(view, request, await _stock(reader, pairs(view)), history=before, chosen=chosen)
        )
        if isinstance(snapshot, Stale):
            store.transition(key, expected=RequestState.APPROVED, target=RequestState.PLANNED, actor=Actor.APPLIER)
            return Applied(RequestState.PLANNED, f"back to review: {snapshot.why}")
        attempt = store.begin_attempt(
            key,
            AttemptKind.APPLY,
            expected=RequestState.APPROVED,
            actor=Actor.APPLIER,
            snapshot=snapshot.as_json(),
            request_body=dict(request.body),
            lock_owner=lock_owner,
        )
        # Step 4: send once. From here on, only InvenTree's own answer makes the outcome known.
        try:
            sent = await writer.send(request)
        except WriteRefused as error:  # raised before anything left the process
            store.transition(key, expected=RequestState.APPLYING, target=RequestState.FAILED, actor=Actor.APPLIER)
            return Applied(RequestState.FAILED, f"not sent: {error}")
        except BaseException:  # a cancellation too: the write may have left, so its outcome is unknown
            store.transition(key, expected=RequestState.APPLYING, target=RequestState.UNKNOWN, actor=Actor.APPLIER)
            raise
        if sent.outcome is not Outcome.ACCEPTED:
            target = RequestState.FAILED if sent.outcome is Outcome.REFUSED else RequestState.UNKNOWN
            store.transition(key, expected=RequestState.APPLYING, target=target, actor=Actor.APPLIER)
            return Applied(target, sent.why)
        store.transition(key, expected=RequestState.APPLYING, target=RequestState.APPLIED, actor=Actor.APPLIER)
        # Step 5: read back. Step 6: record.
        problems = await _read_back(reader, view, request, attempt.marker, snapshot, named)
        target = RequestState.VERIFY_FAILED if problems else RequestState.VERIFIED
        store.transition(key, expected=RequestState.APPLIED, target=target, actor=Actor.APPLIER)
        return Applied(target, "read back: " + ("does not agree with the plan" if problems else "as planned"), problems)


async def _histories(reader: ApplyReader, named: Sequence[int]) -> dict[int, list[int]] | Stale:
    """The history of each named item, or `Stale` when one of them is gone: InvenTree answers 400 to the history of
    an item it deleted (a depleted delete_on_deplete item), so a failed read is stale only if InvenTree says the item
    does not exist. Any other failure raises."""
    history: dict[int, list[int]] = {}
    for pk in named:
        try:
            history[pk] = await reader.item_entries(pk)
        except InvenTreeError:
            if await reader.stock_item_gone(pk):
                return Stale(f"stock item {pk} no longer exists")
            raise
    return history


async def _read_back(
    reader: ApplyReader,
    view: MovementView,
    request: WriteRequest,
    marker: Marker,
    snapshot: Snapshot,
    named: Sequence[int],
) -> tuple[str, ...]:
    gone = [pk for pk in named if await reader.stock_item_gone(pk)]
    api_version = (await reader.server_info()).api_version
    found = await reader.marker_entries(marker.marker_id)
    # The stock before the history, the reverse of step 3: a change another writer made that the stock read shows
    # has its entry in the history read after it.
    read = await _stock(reader, pairs(view))
    owners = sorted((set(named) - set(gone)) | created(found))
    history = {pk: await reader.item_entries(pk) for pk in owners}
    return verify(view, request, marker, snapshot, found, read, gone=gone, history=history, api_version=api_version)


async def _stock(reader: ApplyReader, wanted: Sequence[Pair]) -> Mapping[Pair, list[StockItem]]:
    found: dict[Pair, list[StockItem]] = {}
    for pair in wanted:
        rows = await reader.stock_items(*pair)
        try:
            found[pair] = stock_items(rows)
        except ValueError as error:
            raise InvenTreeError(f"a stock item from InvenTree is not valid ({error})") from None
    return found
