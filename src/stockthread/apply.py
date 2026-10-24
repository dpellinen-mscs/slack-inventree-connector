"""The apply protocol (design section 7, "Apply protocol"): `apply_request` runs steps 1 to 6 for one request,
`check_post_markers` is step 2, the marker check of a whole post against InvenTree, `recover_request` concludes an
apply whose outcome is unknown ("Unknown outcomes and recovery"), `undo_apply` runs one undo ("Undo"), and
`recover_undo` concludes an undo whose outcome is unknown.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import anyio

from stockthread.core.marker import AttemptKind, Marker, marked
from stockthread.core.marker_check import Finding, FindingKind, check_markers
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import UNDOABLE, Actor, RequestState
from stockthread.core.resolve import StockItem, stock_items
from stockthread.core.write_request import PATHS, WriteRequest
from stockthread.gate import Judgement
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, InvenTreeError, MarkedEntry, ServerInfo
from stockthread.inverse import (
    NoUndo,
    Reversal,
    UndoSnapshot,
    check_undo,
    reversals,
    undo_changes,
    undo_request,
    undo_snapshot,
    verify_undo,
)
from stockthread.precondition import Pair, Snapshot, Stale, built_from, check, pairs
from stockthread.readback import created, verify
from stockthread.slack.replies import MovementView
from stockthread.store import EventsWaiting, Store, WriteBlocked
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


async def _units(reader: ApplyReader, view: MovementView) -> dict[int, str]:
    """The units, read now, of each part a line carries as a length in its own unit (`MovementView.units`)."""
    parts = {match.record for match, units in zip(view.items, view.units, strict=False) if units is not None}
    return {part: await reader.part_units(part) for part in sorted(pk for pk in parts if pk is not None)}


async def _items_gone(reader: HistoryReader, pks: list[int]) -> bool:
    for pk in pks:
        if not await reader.stock_item_gone(pk):
            return False
    return True


class ApplyReader(HistoryReader, Protocol):
    """What the whole protocol reads from InvenTree (`InvenTreeReader`)."""

    async def item_entries(self, pk: int) -> list[int]: ...

    async def stock_items(self, part: int, location: int) -> list[dict[str, Any]]: ...

    async def part_units(self, pk: int) -> str: ...


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


def _back_to_review(store: Store, key: RequestKey, why: str) -> Applied:
    """An `approved` request the store refuses to write for good goes back to `planned`, nothing sent."""
    store.transition(key, expected=RequestState.APPROVED, target=RequestState.PLANNED, actor=Actor.APPLIER)
    return Applied(RequestState.PLANNED, f"back to review: {why}")


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
    send_by: float | None = None,
    too_late: Callable[[], str | None] = lambda: None,
) -> Applied:
    """Run the apply protocol (design section 7) once for the `approved` request `key`, whose movement is `view`.

    `rejudge(marker)` judges the request's whole plan again now (`gate.judge`), with `marker` as this request's;
    the request sent is its `requests[key.sequence - 1]`. `chosen`: the lines (from 0) a person named the stock items
    for (`precondition.check`). `lock` is the process's one writer lock: steps 2 to 6 run under it, so the connector
    never has two writes in flight. `send_by` (`anyio.current_time()`): no attempt is begun after it, so a caller that
    cuts the step off cuts a read off, not a write; the request then stays `approved`. `too_late()`, asked after the
    reads, right before the attempt is begun: a reason the request may no longer be written (the plan expired during
    the reads); it then goes back to review, nothing written.

    Each outcome is a state of the request: still `approved` when it waits for another request of the post
    (`Store.apply_waits_for`), the gate blocks it now (nothing read or written) or an event came into the queue
    during the reads (`EventsWaiting`: an edit may take the approval back; nothing written), `planned` when the store
    refuses the write for good (an earlier request of the plan went wrong, `Store.plan_stopped_before`, checked before
    the gate with nothing read; or `WriteBlocked`: a write of the post may still land, or the attempt limit), so it
    goes back to review, `verify_failed` when the history already disagrees with the store, `planned` when the
    stock changed since the plan, `failed` when InvenTree refused the write (or the writer refused to send it),
    `unknown` when the outcome is not known (recovery's), `verified` or `verify_failed` after the read-back. A read
    error before the write leaves the request `approved` and raises; one in the read-back leaves it `applied` (not
    read back, which blocks every other write of the post; `readback_request` concludes it) and raises. Nothing is
    written twice: `begin_attempt` refuses a second attempt of a write that was not refused.
    """
    async with lock:
        row = store.request(key)
        if row.state is not RequestState.APPROVED:
            raise ValueError(f"request {key} is {row.state}, not approved")
        if waits := store.apply_waits_for(key):
            return Applied(RequestState.APPROVED, f"not written yet: {waits}")
        if stopped := store.plan_stopped_before(key):
            return _back_to_review(store, key, stopped)
        # Step 1: the gate again, with the marker the attempt will carry.
        try:
            marker = store.next_marker(key, AttemptKind.APPLY)
        except WriteBlocked as blocked:  # the attempt limit
            return _back_to_review(store, key, str(blocked))
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
            else check(
                view,
                request,
                await _stock(reader, pairs(view)),
                history=before,
                chosen=chosen,
                units=await _units(reader, view),
            )
        )
        if isinstance(snapshot, Stale):
            store.transition(key, expected=RequestState.APPROVED, target=RequestState.PLANNED, actor=Actor.APPLIER)
            return Applied(RequestState.PLANNED, f"back to review: {snapshot.why}")
        if send_by is not None and anyio.current_time() > send_by:
            return Applied(RequestState.APPROVED, "not written yet: the reads took too long to send it in this step")
        if late := too_late():
            return _back_to_review(store, key, late)
        try:
            attempt = store.begin_attempt(
                key,
                AttemptKind.APPLY,
                expected=RequestState.APPROVED,
                actor=Actor.APPLIER,
                snapshot=snapshot.as_json(),
                request_body=dict(request.body),
                lock_owner=lock_owner,
            )
        except EventsWaiting as waiting:  # the worker drains the queue first, then this step runs again
            return Applied(RequestState.APPROVED, f"not written yet: {waiting}")
        except WriteBlocked as blocked:  # for good: what would wait was waited for above, under the same lock
            return _back_to_review(store, key, str(blocked))
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


async def recover_request(
    key: RequestKey,
    view: MovementView,
    *,
    store: Store,
    reader: ApplyReader,
    lock: anyio.Lock,
    settle: timedelta,
) -> Applied:
    """Conclude the `unknown` apply of request `key` (design section 7, "Unknown outcomes and recovery"), whose
    movement is `view`, the one its request was built from.

    Nothing happens until `settle` (`apply.settle_seconds`) has passed since its outcome became unknown (not since
    its intent was stored: the send has no overall deadline): a request that timed out on the client may still be
    running on the server. Then: the attempt's marker found, the request is
    `applied` and read back like any write (`verified` or `verify_failed`). Not found, and every named stock item, every
    total and every named item's history is as the stored snapshot has it: `failed`, and never sent again (ADR 0015).
    Not found, and anything else: `verify_failed`. Read errors raise and change nothing, but one in the
    read-back leaves the request `applied` (`readback_request` concludes it). Nothing is written.
    """
    async with lock:
        row = store.request(key)
        if row.state is not RequestState.UNKNOWN:
            raise ValueError(f"request {key} is {row.state}, not unknown")
        attempt = store.unknown_attempt(key)
        if attempt.marker.kind is not AttemptKind.APPLY:
            raise ValueError(f"the unknown attempt of request {key} is not an apply")
        if attempt.age < settle:
            return Applied(RequestState.UNKNOWN, "still settling: the write may be running on the server")
        snapshot = Snapshot.from_json(attempt.snapshot)
        request = _stored_request(row.operation, attempt.body, attempt.marker, view)
        named = sorted({int(line["pk"]) for line in request.body["items"]})
        found = await reader.marker_entries(attempt.marker.marker_id)
        if any(entry.marker == attempt.marker for entry in found):
            store.transition(
                key,
                expected=RequestState.UNKNOWN,
                target=RequestState.APPLIED,
                actor=Actor.RECOVERY,
                marker_found=True,
            )
            problems = await _read_back(reader, view, request, attempt.marker, snapshot, named)
            target = RequestState.VERIFY_FAILED if problems else RequestState.VERIFIED
            store.transition(key, expected=RequestState.APPLIED, target=target, actor=Actor.APPLIER)
            why = "marker found; read back: " + ("does not agree with the plan" if problems else "as planned")
            return Applied(target, why, problems)
        changes = [f"history entries {_listed(e.pk for e in found)} carry another attempt"] if found else []
        changes += await _changes(reader, view, snapshot, named)
        target = RequestState.VERIFY_FAILED if changes else RequestState.FAILED
        store.transition(key, expected=RequestState.UNKNOWN, target=target, actor=Actor.RECOVERY, marker_found=False)
        why = "marker not found; " + ("the stock changed" if changes else "nothing changed, so it never ran")
        return Applied(target, why, tuple(changes))


async def readback_request(
    key: RequestKey,
    view: MovementView,
    *,
    store: Store,
    reader: ApplyReader,
    lock: anyio.Lock,
) -> Applied:
    """Read back the `applied` request `key` (steps 5 and 6 of design section 7), whose movement is `view`: the
    request a read error in `apply_request`'s or `recover_request`'s read-back left `applied`, which blocks every
    other write of the post until this concludes it. `verified` or `verify_failed`, from the stored request and
    snapshot of its last attempt. Read errors raise and leave it `applied`. Nothing is written."""
    async with lock:
        row = store.request(key)
        if row.state is not RequestState.APPLIED:
            raise ValueError(f"request {key} is {row.state}, not applied")
        attempt = store.applied_attempt(key)
        if attempt.marker.kind is not AttemptKind.APPLY:
            raise ValueError(f"the applied attempt of request {key} is not an apply")
        request = _stored_request(row.operation, attempt.body, attempt.marker, view)
        named = sorted({int(line["pk"]) for line in request.body["items"]})
        snapshot = Snapshot.from_json(attempt.snapshot)
        problems = await _read_back(reader, view, request, attempt.marker, snapshot, named)
        target = RequestState.VERIFY_FAILED if problems else RequestState.VERIFIED
        store.transition(key, expected=RequestState.APPLIED, target=target, actor=Actor.APPLIER)
        return Applied(target, "read back: " + ("does not agree with the plan" if problems else "as planned"), problems)


async def undo_apply(
    key: RequestKey,
    view: MovementView,
    *,
    store: Store,
    reader: ApplyReader,
    writer: Sender,
    lock: anyio.Lock,
    lock_owner: str,
    user: str,
    link: str | None = None,
    send_by: float | None = None,
) -> Applied:
    """Undo the apply of request `key` (design section 7, "Undo") once, as the person `user` asked: `key` is
    `verified`, `verify_failed` or `undo_failed`, and `view` is the movement its request was built from.

    The protocol of `apply_request`, with an undo marker: the marker check of the post (step 2), then the undo built
    from the entries of the apply that wrote (`inverse.reversals`), checked on the stock read now
    (`inverse.check_undo`: histories first, then the stock, so an entry written between the two reads is one the
    read-back reports), the intent stored with the stock it was built on (`inverse.undo_snapshot`), sent once, and
    read back (`inverse.verify_undo`). A refusal before the intent (an attempt the store refuses, a finding, no apply
    that wrote, no undo the history allows) leaves the state as it was and says why; a read error there raises and
    changes nothing. Then: `undo_failed` when InvenTree refused the undo or the writer refused to send it,
    `undo_unknown` when its outcome is not known, `undone` or `verify_failed` after the read-back. A read error (or
    any other exception) in the read-back moves it to `undo_unknown` and raises: there is no state for an undo
    accepted but not read back, and recovery of an unknown undo finds its marker and reads it back (ADR 0017).
    Whether the undo is still allowed (`undo.max_age_hours`, who may ask) is the caller's to decide:
    `applier.undo_refusal`. `send_by`: as `apply_request`'s; past it the state stays as it was."""
    async with lock:
        row = store.request(key)
        if row.state not in UNDOABLE:
            raise ValueError(f"request {key} is {row.state}, not one an undo may start from")
        try:
            marker = store.next_marker(key, AttemptKind.UNDO)
        except WriteBlocked as blocked:
            return Applied(row.state, f"no undo: {blocked}")
        if findings := await check_post_markers(reader, store, key.post):
            problems = tuple(f"{finding.marker}: {finding.why}" for finding in findings)
            return Applied(row.state, "no undo: the stock history disagrees with the records", problems)
        try:
            written = store.written_apply(key)
        except KeyError:
            return Applied(row.state, "no undo: no apply of this request wrote")
        request = _stored_request(row.operation, written.body, written.marker, view)
        snapshot = Snapshot.from_json(written.snapshot)
        found = reversals(view, request, written.marker, snapshot, await reader.marker_entries(row.marker_id))
        if isinstance(found, NoUndo):
            return Applied(row.state, f"no undo: {found.why}")
        history = await _histories(reader, sorted({pk for reversal in found for pk in reversal.history_items}))
        if isinstance(history, Stale):
            return Applied(row.state, f"no undo: {history.why}")
        read = await _stock(reader, pairs(view))
        entries = await reader.marker_entries(row.marker_id)
        if refused := check_undo(view, found, written.marker, snapshot, read, entries=entries, history=history):
            return Applied(row.state, f"no undo: {refused.why}")
        before = undo_snapshot(view, found, read, history)
        undo = undo_request(view, found, marker, user=user, link=link)
        if send_by is not None and anyio.current_time() > send_by:  # as for `apply_request`
            return Applied(row.state, "no undo: InvenTree answered too slowly, ask again in a minute")
        try:
            attempt = store.begin_attempt(
                key,
                AttemptKind.UNDO,
                expected=row.state,
                actor=Actor.HUMAN,
                snapshot=before.as_json(),
                request_body=dict(undo.body),
                lock_owner=lock_owner,
            )
        except WriteBlocked as blocked:
            return Applied(row.state, f"no undo: {blocked}")
        try:
            sent = await writer.send(undo)
        except WriteRefused as error:
            store.transition(key, expected=RequestState.UNDOING, target=RequestState.UNDO_FAILED, actor=Actor.APPLIER)
            return Applied(RequestState.UNDO_FAILED, f"not sent: {error}")
        except BaseException:
            store.transition(key, expected=RequestState.UNDOING, target=RequestState.UNDO_UNKNOWN, actor=Actor.APPLIER)
            raise
        if sent.outcome is not Outcome.ACCEPTED:
            target = RequestState.UNDO_FAILED if sent.outcome is Outcome.REFUSED else RequestState.UNDO_UNKNOWN
            store.transition(key, expected=RequestState.UNDOING, target=target, actor=Actor.APPLIER)
            return Applied(target, sent.why)
        try:
            after = await reader.marker_entries(row.marker_id)
            now = await _stock(reader, pairs(view))
            later, gone = await _reversed_histories(reader, found)
            problems = tuple(verify_undo(view, found, attempt.marker, before, after, now, history=later, gone=gone))
        except BaseException:
            store.transition(key, expected=RequestState.UNDOING, target=RequestState.UNDO_UNKNOWN, actor=Actor.APPLIER)
            raise
        target = RequestState.VERIFY_FAILED if problems else RequestState.UNDONE
        store.transition(key, expected=RequestState.UNDOING, target=target, actor=Actor.APPLIER)
        return Applied(target, "undo read back: " + ("does not agree" if problems else "as built"), problems)


async def recover_undo(
    key: RequestKey,
    view: MovementView,
    *,
    store: Store,
    reader: ApplyReader,
    lock: anyio.Lock,
    settle: timedelta,
) -> Applied:
    """Conclude the `undo_unknown` undo of request `key` (design section 7, "Unknown outcomes and recovery"; ADR
    0017), whose movement is `view`: the undo's outcome was not known, or its read-back could not read.

    Nothing happens until `settle` has passed since its outcome became unknown, as for an apply. The undo is rebuilt
    from what it was built from: the entries of the apply that wrote (`Store.written_apply`; only that apply's own,
    as `undo_apply` found them before the undo could add its own) and the undo's stored snapshot
    (`inverse.UndoSnapshot`), and the stored undo body must be the one they make. Then: the undo's marker found, it is
    read back like any undo (`inverse.verify_undo`): `undone` or `verify_failed`. Not found, and every reversed item,
    every total and every reversed item's history is as the undo's snapshot has it: `undo_failed`, the apply's stock
    standing, and never sent again (ADR 0015: it may still land). Not found, and anything else: `verify_failed`.
    Entries of the request's id that carry neither the apply nor this undo are `verify_failed`, found or not, and
    so is an apply history that no longer shows what the undo reversed, and a reversed item InvenTree deleted. Other
    read errors raise and change nothing, so it stays `undo_unknown` and a later run concludes it. Nothing is
    written."""
    async with lock:
        row = store.request(key)
        if row.state is not RequestState.UNDO_UNKNOWN:
            raise ValueError(f"request {key} is {row.state}, not undo_unknown")
        attempt = store.unknown_attempt(key)
        if attempt.marker.kind is not AttemptKind.UNDO:
            raise ValueError(f"the unknown attempt of request {key} is not an undo")
        if attempt.age < settle:
            return Applied(RequestState.UNDO_UNKNOWN, "still settling: the undo may be running on the server")
        before = UndoSnapshot.from_json(attempt.snapshot)
        written = store.written_apply(key)
        request = _stored_request(row.operation, written.body, written.marker, view)
        entries = await reader.marker_entries(row.marker_id)
        found = any(entry.marker == attempt.marker for entry in entries)
        own = [entry for entry in entries if entry.marker == written.marker]
        undone = reversals(view, request, written.marker, Snapshot.from_json(written.snapshot), own)
        if isinstance(undone, NoUndo):
            store.transition(
                key,
                expected=RequestState.UNDO_UNKNOWN,
                target=RequestState.VERIFY_FAILED,
                actor=Actor.RECOVERY,
                marker_found=found,
            )
            return Applied(
                RequestState.VERIFY_FAILED, "the apply's history no longer shows what the undo reversed", (undone.why,)
            )
        _check_stored_undo(view, undone, attempt.marker, attempt.body)
        # The stock before the history, as in every read-back.
        read = await _stock(reader, pairs(view))
        history, gone = await _reversed_histories(reader, undone)
        others = sorted(entry.pk for entry in entries if entry.marker not in (written.marker, attempt.marker))
        another = [f"history entries {_listed(others)} carry another attempt"] if others else []
        if found:
            problems = (
                *another,
                *verify_undo(view, undone, attempt.marker, before, entries, read, history=history, gone=gone),
            )
            target = RequestState.VERIFY_FAILED if problems else RequestState.UNDONE
            store.transition(
                key, expected=RequestState.UNDO_UNKNOWN, target=target, actor=Actor.RECOVERY, marker_found=True
            )
            why = "undo marker found; read back: " + ("does not agree" if problems else "as built")
            return Applied(target, why, problems)
        changes = another + undo_changes(undone, before, read, history=history, gone=gone)
        target = RequestState.VERIFY_FAILED if changes else RequestState.UNDO_FAILED
        store.transition(
            key, expected=RequestState.UNDO_UNKNOWN, target=target, actor=Actor.RECOVERY, marker_found=False
        )
        why = "undo marker not found; " + ("the stock changed" if changes else "nothing changed, so it never ran")
        return Applied(target, why, tuple(changes))


def _check_stored_undo(view: MovementView, found: Sequence[Reversal], marker: Marker, body: Any) -> None:
    """ValueError unless `body` is the undo of `found` with `marker` (its notes lead with it): the undo recovery
    reads back is the one that was sent."""
    built = undo_request(view, found, marker, user="U00000000").body  # the notes are not compared
    if not isinstance(body, dict) or marked(body.get("notes"), marker.marker_id) != marker:
        raise ValueError("the stored undo does not lead its notes with its marker")
    if {name: value for name, value in body.items() if name != "notes"} != {
        name: value for name, value in built.items() if name != "notes"
    }:
        raise ValueError("the stored undo is not the one the apply's history makes")


def _stored_request(operation: Operation, body: Any, marker: Marker, view: MovementView) -> WriteRequest:
    """The request as stored for an attempt with `marker`; ValueError unless it was built from `view` and leads its
    notes with `marker`."""
    request = WriteRequest(operation, PATHS[operation], body)
    if not built_from(view, request) or not str(request.body.get("notes", "")).startswith(str(marker)):
        raise ValueError("the stored request was not built from this movement and marker")
    return request


async def _changes(reader: ApplyReader, view: MovementView, snapshot: Snapshot, named: Sequence[int]) -> list[str]:
    """How the stock differs from `snapshot`: a named item gone, moved or holding another quantity, a total that
    changed, or a history entry on a named item that the snapshot lacks. Stock before history, as `_read_back`."""
    changes = [f"stock item {pk} is gone" for pk in named if await reader.stock_item_gone(pk)]
    read = await _stock(reader, pairs(view))
    held = {item.pk: item.quantity for items in read.values() for item in items}
    for pk in named:
        before = snapshot.stock_items.get(pk)
        if pk not in held:
            changes.append(f"stock item {pk} is no longer where the movement acts")
        elif before is None or held[pk] != before:
            changes.append(f"stock item {pk} holds {held[pk]}, the snapshot says {before}")
    for pair, total in sorted(snapshot.totals.items()):
        now = sum((item.quantity for item in read.get(pair, ())), Decimal(0))
        if pair not in read or now != total:
            changes.append(f"part {pair[0]} at location {pair[1]} holds {now}, the snapshot says {total}")
    for pk in named:
        if pk in held:
            new = set(await reader.item_entries(pk)) - snapshot.history.get(pk, frozenset())
            if new:
                changes.append(f"stock item {pk} has history entries {_listed(new)} the snapshot lacks")
    return changes


def _listed(pks: Any) -> str:
    return ", ".join(str(pk) for pk in sorted(pks))


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


async def _reversed_histories(reader: ApplyReader, found: Sequence[Reversal]) -> tuple[dict[int, list[int]], list[int]]:
    """The history of each reversed item, and the reversed items InvenTree deleted (a depleted delete_on_deplete
    item: its history read answers 400), which an undo's read-back reports instead of reading. Any other failure
    raises."""
    history: dict[int, list[int]] = {}
    gone: list[int] = []
    for reversal in found:
        try:
            history[reversal.item] = await reader.item_entries(reversal.item)
        except InvenTreeError:
            if not await reader.stock_item_gone(reversal.item):
                raise
            gone.append(reversal.item)
    return history, gone


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
