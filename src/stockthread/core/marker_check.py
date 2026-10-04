"""Step 2 of the apply protocol: do the markers found in the stock history agree with the recorded states?

Design section 7, "Apply protocol" and "The marker". Before a request of a post is sent, the history is searched for
the marker id of every request of the post, and each request's hits are compared with what the store recorded for
it. This module only compares; the caller searches (`InvenTreeReader.marker_entries`) and acts on the findings.

A found marker proves that its write happened, so a hit that the recorded state does not explain is a `HIT`
finding: an attempt the store has no record of (a lost or restored database), an attempt InvenTree refused, an
apply of a request that left no stock change behind (a late write of an unknown attempt), or a second attempt of a
request that is already applied once.

A marker that is not found proves nothing on its own, but a request the store records as verified or undone wrote
its marker, and a history that no longer shows it cannot be trusted to show the others either (an admin deleted
history, or another database wrote). That is a `MISSING` finding, with two excuses only. On 1.4.3 a write that
deleted its stock item cannot be found by search (design section 5, Depletion): the caller passes the markers whose
item answered 404 (`item_gone`); the operation alone excuses nothing, because a partial remove keeps its marker. And
the apply of an undone request whose outcome stayed unknown may never have written: recovery moves "not found,
changed" to `verify_failed`, and the operator's undo reverses what was observed.

In any state, at most one attempt of each kind may have written: a request is applied once and undone once, so two
found apply markers, or two found undo markers, are a `HIT`. For a verified or undone request it must be the last
attempt. Requests in the other states (in flight, `verify_failed`, `accepted`) are checked for that and for hits no
attempt explains.

Not decided here: a request in `verify_failed` whose single undo attempt wrote late, after recovery, is one write,
explained. An undo the operator then starts must be built from the history observed at its start, which holds that
entry (design section 7, Undo).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation, RequestKey
from stockthread.core.requests import RequestState

S = RequestState

NEVER_WROTE: frozenset[RequestState] = frozenset({S.PLANNED, S.APPROVED, S.REJECTED, S.CANCELLED, S.EXPIRED, S.FAILED})
"""States in which no apply attempt of the request may have written. `approved` includes a new attempt after a
failure: the earlier attempt's late write must stop it."""


class Outcome(StrEnum):
    """What the store recorded for an attempt (`request_attempts.outcome`)."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecordedAttempt:
    marker: Marker
    outcome: Outcome | None
    """None while the attempt is in flight."""


@dataclass(frozen=True)
class RecordedRequest:
    """One request of a post as the store recorded it."""

    key: RequestKey
    operation: Operation
    state: RequestState
    marker_id: str
    attempts: tuple[RecordedAttempt, ...]
    """Oldest first."""

    def __post_init__(self) -> None:
        if any(attempt.marker.marker_id != self.marker_id for attempt in self.attempts):
            raise ValueError("an attempt of a request carries the request's marker id")
        if len({attempt.marker for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("each attempt of a request has its own marker")


class FindingKind(StrEnum):
    HIT = "hit"
    """A marker was found that the recorded state does not explain: a write the store does not account for."""
    MISSING = "missing"
    """A marker the recorded state says was written was not found."""


@dataclass(frozen=True)
class Finding:
    key: RequestKey
    kind: FindingKind
    marker: Marker
    why: str


def check_markers(
    requests: Sequence[RecordedRequest], found: Mapping[str, Iterable[Marker]], *, item_gone: Collection[Marker] = ()
) -> list[Finding]:
    """The findings for every request of a post, given the markers found per marker id. Empty means the history
    agrees with the store. `found` must hold a search result for every request: a request left out was not
    searched, which is not the same as nothing found. `item_gone`: markers of writes whose stock item the caller
    has seen answer 404 (`GET /api/stock/<pk>/`), so their entry may not be found; no other missing marker is
    excused."""
    by_id = {request.marker_id: request for request in requests}
    if len(by_id) != len(requests):
        raise ValueError("each request of a post has its own marker id")
    if set(found) != set(by_id):
        raise ValueError("the markers of exactly the post's requests were searched")
    findings: list[Finding] = []
    for request in requests:
        hits = set(found[request.marker_id])
        if any(marker.marker_id != request.marker_id for marker in hits):
            raise ValueError("a search result for a marker id holds only markers of that id")
        findings.extend(_check(request, hits, frozenset(item_gone)))
    return findings


def _check(request: RecordedRequest, hits: set[Marker], item_gone: frozenset[Marker]) -> Iterable[Finding]:
    def hit(marker: Marker, why: str) -> Finding:
        return Finding(request.key, FindingKind.HIT, marker, why)

    recorded = {attempt.marker: attempt for attempt in request.attempts}
    explained: set[Marker] = set()
    for marker in sorted(hits, key=str):
        attempt = recorded.get(marker)
        if attempt is None:
            yield hit(marker, "the store has no record of this attempt")
        elif attempt.outcome is Outcome.REFUSED:
            yield hit(marker, "InvenTree refused this attempt, yet it wrote")
        else:
            explained.add(marker)

    applies = [attempt for attempt in request.attempts if attempt.marker.kind is AttemptKind.APPLY]
    undos = [attempt for attempt in request.attempts if attempt.marker.kind is AttemptKind.UNDO]
    if request.state in NEVER_WROTE:
        for attempt in applies:
            if attempt.marker in explained:
                yield hit(attempt.marker, f"the request is {request.state}, yet this apply wrote")
        return
    # In every other state, at most one attempt of each kind wrote: a request is applied once and undone once. A
    # verified or undone request records which one: the last.
    for attempts in (applies, undos):
        wrote = [attempt.marker for attempt in attempts if attempt.marker in explained]
        settled = request.state in (S.VERIFIED, S.UNDONE) and attempts
        earlier = [m for m in wrote if m != attempts[-1].marker] if settled else wrote[:-1]
        for marker in earlier:
            yield hit(marker, "a later attempt of this request also wrote")
    if request.state is S.VERIFIED:
        for attempt in undos:
            if attempt.marker in explained:
                yield hit(attempt.marker, "the request is verified, yet this undo wrote")
    if request.state in (S.VERIFIED, S.UNDONE):
        yield from _missing(request, applies, explained, item_gone, excused_if_unknown=request.state is S.UNDONE)
    if request.state is S.UNDONE:
        yield from _missing(request, undos, explained, item_gone, excused_if_unknown=False)


def _missing(
    request: RecordedRequest,
    attempts: list[RecordedAttempt],
    explained: set[Marker],
    item_gone: frozenset[Marker],
    *,
    excused_if_unknown: bool,
) -> Iterable[Finding]:
    """The last of `attempts` wrote, so it must be found unless its item is gone. `excused_if_unknown`: an undone
    apply whose outcome stayed unknown may never have written (recovery found no marker but changed stock, and the
    operator undid what was observed)."""
    if not attempts:
        raise ValueError(f"a {request.state} request has an attempt of each kind it went through")
    last = attempts[-1]
    if last.marker in explained or last.marker in item_gone:
        return
    if excused_if_unknown and last.outcome is Outcome.UNKNOWN:
        return
    yield Finding(request.key, FindingKind.MISSING, last.marker, f"the request is {request.state}, but not found")
