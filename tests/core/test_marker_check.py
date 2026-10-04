"""Step 2 of the apply protocol: found markers against recorded states (design section 7)."""

from collections.abc import Iterable

import pytest

from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.marker_check import (
    NEVER_WROTE,
    Finding,
    FindingKind,
    Outcome,
    RecordedAttempt,
    RecordedRequest,
    check_markers,
)
from stockthread.core.model import Operation, PostKey, RequestKey
from stockthread.core.requests import RequestState

S = RequestState
OC = Outcome
APPLY, UNDO = AttemptKind.APPLY, AttemptKind.UNDO
POST = PostKey("T0ACME01", "C0STOCK1", "1727600000.000100")
ID_1 = "k7m2q9x4t1b8w3n6p5r0zd"
ID_2 = "a1b2c3d4e5f6g7h8j9k0mn"
KEY_1 = RequestKey(POST, 1, 1)
KEY_2 = RequestKey(POST, 1, 2)


def m(kind: AttemptKind, attempt: int, marker_id: str = ID_1) -> Marker:
    return Marker(marker_id, kind, attempt)


def request(
    state: RequestState,
    *attempts: tuple[AttemptKind, int, Outcome | None],
    operation: Operation = Operation.TRANSFER,
    key: RequestKey = KEY_1,
    marker_id: str = ID_1,
) -> RecordedRequest:
    return RecordedRequest(
        key,
        operation,
        state,
        marker_id,
        tuple(RecordedAttempt(m(kind, number, marker_id), outcome) for kind, number, outcome in attempts),
    )


def check(one: RecordedRequest, found: Iterable[Marker] = ()) -> list[tuple[FindingKind, Marker]]:
    return [(finding.kind, finding.marker) for finding in check_markers([one], {one.marker_id: list(found)})]


HIT, MISSING = FindingKind.HIT, FindingKind.MISSING


# Requests that never wrote


@pytest.mark.parametrize("state", sorted(NEVER_WROTE))
def test_a_request_that_never_wrote_and_has_no_marker_is_clear(state: RequestState) -> None:
    assert check(request(state)) == []


def test_never_wrote_is_exactly_the_states_that_left_no_change_before_an_apply() -> None:
    assert {S.PLANNED, S.APPROVED, S.REJECTED, S.CANCELLED, S.EXPIRED, S.FAILED} == NEVER_WROTE


@pytest.mark.parametrize("state", [S.PLANNED, S.APPROVED, S.REJECTED, S.CANCELLED, S.EXPIRED])
def test_a_marker_with_no_recorded_attempt_is_a_hit(state: RequestState) -> None:
    # A lost or restored database: InvenTree holds a write the store never started.
    assert check(request(state), [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


def test_a_late_write_of_an_unknown_attempt_recovered_as_failed_is_a_hit() -> None:
    failed = request(S.FAILED, (APPLY, 1, OC.UNKNOWN))
    assert check(failed) == []
    assert check(failed, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


def test_a_new_attempt_after_a_failure_is_stopped_by_the_earlier_attempts_write() -> None:
    approved_again = request(S.APPROVED, (APPLY, 1, OC.UNKNOWN))
    findings = check_markers([approved_again], {ID_1: [m(APPLY, 1)]})
    assert findings == [Finding(KEY_1, HIT, m(APPLY, 1), "the request is approved, yet this apply wrote")]


@pytest.mark.parametrize("state", [S.FAILED, S.APPROVED, S.VERIFY_FAILED, S.ACCEPTED, S.UNKNOWN])
def test_a_refused_attempt_that_wrote_is_a_hit_in_any_state(state: RequestState) -> None:
    refused = request(state, (APPLY, 1, OC.REFUSED))
    assert check(refused, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


# Verified and undone


def test_a_verified_request_with_its_marker_is_clear() -> None:
    assert check(request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED)), [m(APPLY, 1)]) == []


def test_a_verified_request_recovered_from_unknown_with_its_marker_is_clear() -> None:
    assert check(request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN)), [m(APPLY, 1)]) == []


def test_a_verified_request_without_its_marker_is_missing() -> None:
    assert check(request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED))) == [(MISSING, m(APPLY, 1))]


def test_a_verified_write_whose_item_is_gone_may_miss_its_marker() -> None:
    # 1.4.3: a write that deleted its stock item cannot be found by search (design section 5, Depletion). Only the
    # caller's 404 excuses it, never the operation alone: a partial remove still leaves its marker.
    remove = request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED), operation=Operation.REMOVE)
    assert check(remove) == [(MISSING, m(APPLY, 1))]
    assert check_markers([remove], {ID_1: []}, item_gone=[m(APPLY, 1)]) == []
    assert check_markers([remove], {ID_1: []}, item_gone=[m(APPLY, 2)]) != []


def test_an_earlier_attempt_that_wrote_is_a_hit_even_if_the_last_ones_item_is_gone() -> None:
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN), (APPLY, 2, OC.ACCEPTED), operation=Operation.REMOVE)
    findings = check_markers([twice], {ID_1: [m(APPLY, 1)]}, item_gone=[m(APPLY, 2)])
    assert [(finding.kind, finding.marker) for finding in findings] == [(HIT, m(APPLY, 1))]


def test_an_earlier_apply_attempt_of_a_verified_request_that_also_wrote_is_a_hit() -> None:
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN), (APPLY, 2, OC.ACCEPTED))
    assert check(twice, [m(APPLY, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(APPLY, 2)]) == [(HIT, m(APPLY, 1))]


def test_a_verified_request_found_only_by_an_earlier_attempt_is_a_hit_and_missing() -> None:
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN), (APPLY, 2, OC.ACCEPTED))
    assert check(twice, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1)), (MISSING, m(APPLY, 2))]


def test_an_undo_of_a_verified_request_that_wrote_is_a_hit() -> None:
    # undo_unknown, recovered as verified: the undo did not happen, so its marker must not be there.
    verified = request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN))
    assert check(verified, [m(APPLY, 1)]) == []
    assert check(verified, [m(APPLY, 1), m(UNDO, 1)]) == [(HIT, m(UNDO, 1))]


def test_an_undone_request_with_both_markers_is_clear() -> None:
    assert check(request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.ACCEPTED)), [m(APPLY, 1), m(UNDO, 1)]) == []


def test_an_undone_request_without_its_undo_marker_is_missing() -> None:
    undone = request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.ACCEPTED))
    assert check(undone, [m(APPLY, 1)]) == [(MISSING, m(UNDO, 1))]
    assert check(undone, [m(UNDO, 1)]) == [(MISSING, m(APPLY, 1))]


def test_an_undone_request_whose_undo_item_is_gone_may_miss_its_undo_marker() -> None:
    undone = request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.ACCEPTED), operation=Operation.ADD)
    assert check(undone, [m(APPLY, 1)]) == [(MISSING, m(UNDO, 1))]
    assert check_markers([undone], {ID_1: [m(APPLY, 1)]}, item_gone=[m(UNDO, 1)]) == []


def test_an_undone_apply_whose_outcome_stayed_unknown_may_miss_its_apply_marker() -> None:
    # unknown, recovery: not found but changed -> verify_failed; the operator undid what was observed.
    undone = request(S.UNDONE, (APPLY, 1, OC.UNKNOWN), (UNDO, 1, OC.ACCEPTED))
    assert check(undone, [m(UNDO, 1)]) == []
    assert check(undone) == [(MISSING, m(UNDO, 1))]
    verified = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN))
    assert check(verified) == [(MISSING, m(APPLY, 1))]


def test_an_earlier_undo_attempt_of_an_undone_request_that_also_wrote_is_a_hit() -> None:
    undone = request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN), (UNDO, 2, OC.ACCEPTED))
    assert check(undone, [m(APPLY, 1), m(UNDO, 1), m(UNDO, 2)]) == [(HIT, m(UNDO, 1))]


def test_a_refused_undo_that_wrote_is_a_hit() -> None:
    undone = request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.REFUSED), (UNDO, 2, OC.ACCEPTED))
    assert check(undone, [m(APPLY, 1), m(UNDO, 1), m(UNDO, 2)]) == [(HIT, m(UNDO, 1))]


@pytest.mark.parametrize("state", [S.VERIFIED, S.UNDONE])
def test_a_verified_or_undone_request_with_no_attempt_is_refused(state: RequestState) -> None:
    with pytest.raises(ValueError, match="attempt"):
        check(request(state))


# States someone is already looking at, or whose outcome is open


@pytest.mark.parametrize(
    "state", [S.APPLYING, S.UNKNOWN, S.APPLIED, S.VERIFY_FAILED, S.ACCEPTED, S.UNDOING, S.UNDO_UNKNOWN]
)
def test_open_or_operator_states_are_checked_only_for_unexplained_hits(state: RequestState) -> None:
    one = request(state, (APPLY, 1, None if state in (S.APPLYING,) else OC.UNKNOWN))
    assert check(one) == []
    assert check(one, [m(APPLY, 1)]) == []
    assert check(one, [m(APPLY, 2)]) == [(HIT, m(APPLY, 2))]


# The whole post


def test_findings_are_per_request_of_the_post() -> None:
    first = request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED))
    second = request(S.APPROVED, key=KEY_2, marker_id=ID_2)
    assert check_markers([first, second], {ID_1: [m(APPLY, 1)], ID_2: []}) == []
    findings = check_markers([first, second], {ID_1: [], ID_2: [m(APPLY, 1, ID_2)]})
    assert [(finding.key, finding.kind) for finding in findings] == [(KEY_1, MISSING), (KEY_2, HIT)]


def test_every_request_of_the_post_must_have_been_searched() -> None:
    first = request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED))
    second = request(S.APPROVED, key=KEY_2, marker_id=ID_2)
    with pytest.raises(ValueError, match="searched"):
        check_markers([first, second], {ID_2: []})
    with pytest.raises(ValueError, match="searched"):
        check_markers([second], {ID_1: [], ID_2: []})


def test_a_found_marker_of_another_id_is_refused() -> None:
    with pytest.raises(ValueError, match="only markers of that id"):
        check(request(S.APPROVED), [m(APPLY, 1, ID_2)])


def test_two_requests_with_one_marker_id_are_refused() -> None:
    with pytest.raises(ValueError, match="own marker id"):
        check_markers([request(S.APPROVED), request(S.PLANNED, key=KEY_2)], {ID_1: []})


def test_a_recorded_attempt_must_carry_the_requests_marker_id() -> None:
    with pytest.raises(ValueError, match="marker id"):
        RecordedRequest(KEY_1, Operation.TRANSFER, S.APPLYING, ID_1, (RecordedAttempt(m(APPLY, 1, ID_2), None),))
    with pytest.raises(ValueError, match="own marker"):
        RecordedRequest(KEY_1, Operation.TRANSFER, S.FAILED, ID_1, (RecordedAttempt(m(APPLY, 1), OC.REFUSED),) * 2)


# A write twice, in any state that may write again


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.APPLIED, S.UNDO_FAILED, S.UNDOING, S.UNKNOWN])
def test_two_apply_attempts_that_both_wrote_are_a_hit_in_any_state(state: RequestState) -> None:
    # unknown -> failed -> a new attempt -> verify_failed, and then the first attempt lands late.
    twice = request(state, (APPLY, 1, OC.UNKNOWN), (APPLY, 2, OC.ACCEPTED))
    assert check(twice, [m(APPLY, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(APPLY, 2)]) == [(HIT, m(APPLY, 1))]


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.UNDO_UNKNOWN, S.UNDOING])
def test_two_undo_attempts_that_both_wrote_are_a_hit_in_any_state(state: RequestState) -> None:
    twice = request(state, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN), (UNDO, 2, OC.UNKNOWN))
    assert check(twice, [m(APPLY, 1), m(UNDO, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(UNDO, 1), m(UNDO, 2)]) == [(HIT, m(UNDO, 1))]
