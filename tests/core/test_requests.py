"""The request state machine against design section 7."""

import itertools
from collections import deque
from collections.abc import Callable

import pytest

from stockthread.core.requests import (
    FINAL,
    LEFT_NO_CHANGE,
    TRANSITIONS,
    UNSETTLED,
    Actor,
    IllegalTransition,
    RequestState,
    check_transition,
    may_approve_new_revision,
    next_states,
)

S = RequestState
A = Actor

# The "Next" column of the table in design section 7, written out again by hand. A change to the
# state machine must change both, on purpose.
DESIGN_TABLE: dict[RequestState, set[RequestState]] = {
    S.PLANNED: {S.APPROVED, S.REJECTED, S.CANCELLED, S.EXPIRED},
    S.APPROVED: {S.APPLYING, S.PLANNED, S.VERIFY_FAILED, S.CANCELLED},
    S.APPLYING: {S.APPLIED, S.FAILED, S.UNKNOWN},
    S.FAILED: {S.APPROVED},
    S.UNKNOWN: {S.APPLIED, S.FAILED, S.VERIFY_FAILED},
    S.APPLIED: {S.VERIFIED, S.VERIFY_FAILED},
    S.VERIFIED: {S.UNDOING},
    S.VERIFY_FAILED: {S.ACCEPTED, S.UNDOING},
    S.ACCEPTED: set(),
    S.UNDOING: {S.UNDONE, S.UNDO_FAILED, S.UNDO_UNKNOWN, S.VERIFY_FAILED},  # verify_failed: ADR 0017
    S.UNDO_FAILED: {S.UNDOING},
    S.UNDO_UNKNOWN: {S.UNDONE, S.UNDO_FAILED, S.VERIFY_FAILED},
    S.UNDONE: set(),
    S.REJECTED: set(),
    S.CANCELLED: set(),
    S.EXPIRED: set(),
}


def reachable(start: RequestState, edge_allowed: Callable[[frozenset[Actor]], bool]) -> set[RequestState]:
    """States reachable from `start` in one or more steps, using only the edges `edge_allowed` accepts."""
    seen: set[RequestState] = set()
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for (source, target), actors in TRANSITIONS.items():
            if source == state and edge_allowed(actors) and target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def test_transitions_match_the_design_table() -> None:
    assert set(DESIGN_TABLE) == set(RequestState)
    for state, targets in DESIGN_TABLE.items():
        assert next_states(state) == targets, state


def test_final_states_have_no_way_out() -> None:
    assert {state for state, targets in DESIGN_TABLE.items() if not targets} == FINAL


def test_every_state_is_reachable_and_can_finish() -> None:
    assert reachable(S.PLANNED, lambda _: True) | {S.PLANNED} == set(RequestState)
    for state in set(RequestState) - FINAL:
        assert reachable(state, lambda _: True) & FINAL, state


@pytest.mark.parametrize("start", [S.APPLYING, S.FAILED, S.UNKNOWN])
def test_no_write_is_retried_without_a_human(start: RequestState) -> None:
    # Once a write was attempted, only a human can lead to another one.
    assert S.APPLYING not in reachable(start, lambda actors: A.HUMAN not in actors)


@pytest.mark.parametrize("start", [S.UNDOING, S.UNDO_FAILED, S.UNDO_UNKNOWN])
def test_no_undo_is_retried_without_a_human(start: RequestState) -> None:
    assert S.UNDOING not in reachable(start, lambda actors: A.HUMAN not in actors)


def test_only_a_human_starts_an_undo_or_closes_a_failed_verification() -> None:
    for (source, target), actors in TRANSITIONS.items():
        if target in (S.UNDOING, S.ACCEPTED, S.REJECTED):
            assert actors == {A.HUMAN}, (source, target)


def test_the_policy_can_only_approve_a_planned_request() -> None:
    assert [edge for edge, actors in TRANSITIONS.items() if A.POLICY in actors] == [(S.PLANNED, S.APPROVED)]


def test_recovery_acts_only_on_unknown_outcomes() -> None:
    """And on a write a crash left in flight, which it may only declare unknown (`conclude_crashed_writes`)."""
    edges = {edge for edge, actors in TRANSITIONS.items() if A.RECOVERY in actors}
    assert {source for source, _ in edges} == {S.UNKNOWN, S.UNDO_UNKNOWN, S.APPLYING, S.UNDOING}
    assert {edge for edge in edges if edge[0] in (S.APPLYING, S.UNDOING)} == {
        (S.APPLYING, S.UNKNOWN),
        (S.UNDOING, S.UNDO_UNKNOWN),
    }


def test_only_the_applier_sends_or_concludes_a_write() -> None:
    for (source, target), actors in TRANSITIONS.items():
        if (source, target) == (S.APPROVED, S.CANCELLED):
            continue  # nothing was sent: a stale plan withdraws its approval
        if (source, target) in ((S.APPLYING, S.UNKNOWN), (S.UNDOING, S.UNDO_UNKNOWN)):
            assert actors == {A.APPLIER, A.RECOVERY}, (source, target)  # recovery's: a crash left it in flight
            continue
        if source in (S.APPROVED, S.APPLYING, S.APPLIED, S.UNDOING) and target != S.UNDOING:
            assert actors == {A.APPLIER}, (source, target)
    assert TRANSITIONS[(S.APPROVED, S.CANCELLED)] == {A.PLANNER}


def test_check_transition_allows_every_listed_edge() -> None:
    for (source, target), actors in TRANSITIONS.items():
        for actor in actors:
            check_transition(source, target, actor)


def test_check_transition_refuses_everything_else() -> None:
    for source, target, actor in itertools.product(RequestState, RequestState, Actor):
        if actor in TRANSITIONS.get((source, target), frozenset()):
            continue
        with pytest.raises(IllegalTransition):
            check_transition(source, target, actor)


def test_error_names_who_may_make_the_transition() -> None:
    with pytest.raises(IllegalTransition, match="made by human, not by applier"):
        check_transition(S.FAILED, S.APPROVED, A.APPLIER)
    with pytest.raises(IllegalTransition, match="cannot go from undone to verified"):
        check_transition(S.UNDONE, S.VERIFIED, A.RECOVERY)


def test_next_states_for_one_actor() -> None:
    assert next_states(S.PLANNED, A.POLICY) == {S.APPROVED}
    assert next_states(S.VERIFY_FAILED, A.APPLIER) == set()
    assert next_states(S.UNKNOWN, A.RECOVERY) == {S.APPLIED, S.FAILED, S.VERIFY_FAILED}


def test_unsettled_states_are_the_ones_with_a_write_in_flight() -> None:
    assert {S.APPLYING, S.UNKNOWN, S.UNDOING, S.UNDO_UNKNOWN} == UNSETTLED
    assert not UNSETTLED & FINAL


@pytest.mark.parametrize(
    ("earlier", "allowed"),
    [
        ([], True),
        ([S.REJECTED, S.EXPIRED, S.CANCELLED], True),
        ([S.UNDONE, S.FAILED], True),
        ([S.UNDONE, S.VERIFIED], False),
        ([S.ACCEPTED], False),  # the post is closed
        ([S.UNKNOWN], False),
        ([S.UNDO_UNKNOWN], False),
        ([S.PLANNED], False),
        ([S.APPROVED], False),
    ],
)
def test_may_approve_new_revision(earlier: list[RequestState], allowed: bool) -> None:
    assert may_approve_new_revision(earlier) is allowed


def test_left_no_change_means_nothing_can_still_be_in_flight() -> None:
    assert not LEFT_NO_CHANGE & UNSETTLED
    assert S.ACCEPTED not in LEFT_NO_CHANGE
