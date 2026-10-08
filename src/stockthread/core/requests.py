"""The state machine of a request: one InvenTree write and its undo (design section 7).

This module only decides which transitions are allowed and who may make
them. The store applies a transition as a compare-and-set update, so two
workers that race for the same request cannot both succeed.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class RequestState(StrEnum):
    PLANNED = "planned"
    APPROVED = "approved"
    APPLYING = "applying"
    FAILED = "failed"
    UNKNOWN = "unknown"
    APPLIED = "applied"
    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"
    ACCEPTED = "accepted"
    UNDOING = "undoing"
    UNDO_FAILED = "undo_failed"
    UNDO_UNKNOWN = "undo_unknown"
    UNDONE = "undone"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Actor(StrEnum):
    """Who makes a transition."""

    HUMAN = "human"
    """An authorized Slack user, by button: approve, reject, undo, accept, retry."""
    POLICY = "policy"
    """The write policy, when it applies a movement without a click (ADR 0004)."""
    PLANNER = "planner"
    """The pipeline, when a plan becomes stale or expires."""
    APPLIER = "applier"
    """The apply and undo protocol, including the read-back."""
    RECOVERY = "recovery"
    """Recovery of an unknown outcome, after the settle time; and, at start, of a write a crash left in flight."""


S = RequestState
A = Actor

TRANSITIONS: dict[tuple[RequestState, RequestState], frozenset[Actor]] = {
    (S.PLANNED, S.APPROVED): frozenset({A.HUMAN, A.POLICY}),
    (S.PLANNED, S.REJECTED): frozenset({A.HUMAN}),
    (S.PLANNED, S.CANCELLED): frozenset({A.PLANNER}),
    (S.PLANNED, S.EXPIRED): frozenset({A.PLANNER}),
    (S.APPROVED, S.APPLYING): frozenset({A.APPLIER}),
    (S.APPROVED, S.PLANNED): frozenset({A.APPLIER}),  # a precondition failed: back to review
    (S.APPROVED, S.VERIFY_FAILED): frozenset({A.APPLIER}),  # the marker is already present
    (S.APPROVED, S.CANCELLED): frozenset({A.PLANNER}),  # the plan became stale before the write started
    (S.APPLYING, S.APPLIED): frozenset({A.APPLIER}),
    (S.APPLYING, S.FAILED): frozenset({A.APPLIER}),
    (S.APPLYING, S.UNKNOWN): frozenset({A.APPLIER, A.RECOVERY}),  # recovery's: a crash left it applying
    (S.FAILED, S.APPROVED): frozenset({A.HUMAN}),  # a new attempt, never automatic
    (S.UNKNOWN, S.APPLIED): frozenset({A.RECOVERY}),
    (S.UNKNOWN, S.FAILED): frozenset({A.RECOVERY}),  # no marker, nothing changed; never sent again (ADR 0015)
    (S.UNKNOWN, S.VERIFY_FAILED): frozenset({A.RECOVERY}),
    (S.APPLIED, S.VERIFIED): frozenset({A.APPLIER}),
    (S.APPLIED, S.VERIFY_FAILED): frozenset({A.APPLIER}),
    (S.VERIFIED, S.UNDOING): frozenset({A.HUMAN}),
    (S.VERIFY_FAILED, S.ACCEPTED): frozenset({A.HUMAN}),
    (S.VERIFY_FAILED, S.UNDOING): frozenset({A.HUMAN}),
    (S.UNDOING, S.UNDONE): frozenset({A.APPLIER}),
    (S.UNDOING, S.UNDO_FAILED): frozenset({A.APPLIER}),
    (S.UNDOING, S.UNDO_UNKNOWN): frozenset({A.APPLIER, A.RECOVERY}),  # recovery's: a crash left it undoing
    (S.UNDOING, S.VERIFY_FAILED): frozenset({A.APPLIER}),  # InvenTree accepted the undo; its read-back disagrees
    (S.UNDO_FAILED, S.UNDOING): frozenset({A.HUMAN}),  # a new attempt, never automatic
    (S.UNDO_UNKNOWN, S.UNDONE): frozenset({A.RECOVERY}),
    (S.UNDO_UNKNOWN, S.UNDO_FAILED): frozenset({A.RECOVERY}),  # no marker, nothing changed; never sent again
    (S.UNDO_UNKNOWN, S.VERIFY_FAILED): frozenset({A.RECOVERY}),
}

FINAL: frozenset[RequestState] = frozenset({S.ACCEPTED, S.UNDONE, S.REJECTED, S.CANCELLED, S.EXPIRED})

LEFT_NO_CHANGE: frozenset[RequestState] = frozenset({S.UNDONE, S.REJECTED, S.CANCELLED, S.EXPIRED, S.FAILED})
"""States in which a request left no stock change behind. `failed` counts: InvenTree refused it, or recovery found
no marker and no change. The second may still land, so the store also refuses every apply of such a post (ADR
0015)."""

RECEIPT_STATES: frozenset[RequestState] = frozenset({S.VERIFIED, S.VERIFY_FAILED, S.FAILED, S.UNDONE, S.UNDO_FAILED})
"""States in which a request's write or undo, or its try, has concluded: read back as planned, a check that found a
problem, not applied, undone, or not undone. A plan with a request in one of them that no receipt reported is due a
receipt once no request of it is `WORKING` (`Store.receipts_due`)."""

NEW_TRY: frozenset[RequestState] = frozenset({S.APPROVED, S.UNDOING})
"""States that start a new try of a write or an undo. Moving into one forgets the state a receipt reported, so the
try's outcome is reported even when it is that state again (`Store._set_state`)."""

WORKING: frozenset[RequestState] = frozenset({S.APPROVED, S.APPLYING, S.APPLIED, S.UNKNOWN, S.UNDOING, S.UNDO_UNKNOWN})
"""States in which a step of the apply or undo protocol is due or in flight. A receipt waits for every request of its
plan to leave them, so it never reports a write that is still going on."""

UNSETTLED: frozenset[RequestState] = frozenset({S.APPLYING, S.UNKNOWN, S.UNDOING, S.UNDO_UNKNOWN})
"""States in which a write may be in flight. Nothing else may be sent for the post."""

UNDOABLE: frozenset[RequestState] = frozenset({S.VERIFIED, S.VERIFY_FAILED, S.UNDO_FAILED})
"""The states an undo may start from, by a person (`Actor.HUMAN`): `apply.undo_apply`, `Store.undo_refusal`."""


class IllegalTransition(Exception):
    """A transition that the state machine does not allow, or not for this actor."""


def check_transition(current: RequestState, target: RequestState, actor: Actor) -> None:
    """Raise IllegalTransition unless `actor` may move a request from `current` to `target`."""
    allowed = TRANSITIONS.get((current, target))
    if allowed is None:
        raise IllegalTransition(f"a request cannot go from {current} to {target}")
    if actor not in allowed:
        who = ", ".join(sorted(allowed))
        raise IllegalTransition(f"{current} to {target} is made by {who}, not by {actor}")


def next_states(current: RequestState, actor: Actor | None = None) -> frozenset[RequestState]:
    """The states that `current` can move to, by anyone or by one actor."""
    return frozenset(
        target
        for (source, target), actors in TRANSITIONS.items()
        if source == current and (actor is None or actor in actors)
    )


def may_approve_new_revision(earlier: Iterable[RequestState]) -> bool:
    """True if a new plan revision may be approved, given the states of the earlier revision's requests.

    Every earlier request must have left no stock change behind. After `accepted`, the post is
    closed, so that also returns False.
    """
    return all(state in LEFT_NO_CHANGE for state in earlier)
