# SPDX-License-Identifier: MIT
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
    *attempts: tuple[AttemptKind, int, Outcome | None] | tuple[AttemptKind, int, Outcome | None, bool],
    operation: Operation = Operation.TRANSFER,
    key: RequestKey = KEY_1,
    marker_id: str = ID_1,
) -> RecordedRequest:
    return RecordedRequest(
        key,
        operation,
        state,
        marker_id,
        tuple(RecordedAttempt(m(kind, number, marker_id), *rest) for kind, number, *rest in attempts),
    )


def check(one: RecordedRequest, found: Iterable[Marker] = ()) -> list[tuple[FindingKind, Marker]]:
    return [(finding.kind, finding.marker) for finding in check_markers([one], {one.marker_id: list(found)})]


HIT, MISSING = FindingKind.HIT, FindingKind.MISSING
FOUND, NOT_FOUND = True, False
"""What recovery concluded about an unknown attempt's marker."""


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
    failed = request(S.FAILED, (APPLY, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(failed) == []
    assert check(failed, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


def test_a_new_attempt_after_a_failure_is_stopped_by_the_earlier_attempts_write() -> None:
    approved_again = request(S.APPROVED, (APPLY, 1, OC.UNKNOWN, NOT_FOUND))
    findings = check_markers([approved_again], {ID_1: [m(APPLY, 1)]})
    assert findings == [
        Finding(KEY_1, HIT, m(APPLY, 1), "recovery did not find this attempt's marker, yet it wrote late")
    ]


@pytest.mark.parametrize("state", [S.FAILED, S.APPROVED, S.VERIFY_FAILED, S.ACCEPTED, S.UNKNOWN])
def test_a_refused_attempt_that_wrote_is_a_hit_in_any_state(state: RequestState) -> None:
    refused = request(state, (APPLY, 1, OC.REFUSED))
    assert check(refused, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


# Verified and undone


def test_a_verified_request_with_its_marker_is_clear() -> None:
    assert check(request(S.VERIFIED, (APPLY, 1, OC.ACCEPTED)), [m(APPLY, 1)]) == []


def test_a_verified_request_recovered_from_unknown_with_its_marker_is_clear() -> None:
    assert check(request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, FOUND)), [m(APPLY, 1)]) == []


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
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (APPLY, 2, OC.ACCEPTED), operation=Operation.REMOVE)
    findings = check_markers([twice], {ID_1: [m(APPLY, 1)]}, item_gone=[m(APPLY, 2)])
    assert [(finding.kind, finding.marker) for finding in findings] == [(HIT, m(APPLY, 1))]


def test_an_earlier_apply_attempt_of_a_verified_request_that_also_wrote_is_a_hit() -> None:
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (APPLY, 2, OC.ACCEPTED))
    assert check(twice, [m(APPLY, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(APPLY, 2)]) == [(HIT, m(APPLY, 1))]


def test_a_verified_request_found_only_by_an_earlier_attempt_is_a_hit_and_missing() -> None:
    # A history the state machine cannot produce (a found apply is applied, never tried again): the rule holds.
    twice = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, FOUND), (APPLY, 2, OC.ACCEPTED))
    findings = check_markers([twice], {ID_1: [m(APPLY, 1)]})
    assert findings == [
        Finding(KEY_1, HIT, m(APPLY, 1), "the store records another attempt of this request as the one that wrote"),
        Finding(KEY_1, MISSING, m(APPLY, 2), "the request is verified, but not found"),
    ]


def test_an_undo_recovery_did_not_find_that_wrote_late_is_a_hit() -> None:
    # undo_unknown, recovered as undo_failed: the undo did not happen, so its marker must not be there.
    failed = request(S.UNDO_FAILED, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(failed, [m(APPLY, 1)]) == []
    assert check(failed, [m(APPLY, 1), m(UNDO, 1)]) == [(HIT, m(UNDO, 1))]


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


def test_an_undone_apply_whose_marker_recovery_did_not_find_may_miss_it() -> None:
    # unknown, recovery: not found but changed -> verify_failed; the operator undid what was observed.
    undone = request(S.UNDONE, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (UNDO, 1, OC.ACCEPTED))
    assert check(undone, [m(UNDO, 1)]) == []
    assert check(undone) == [(MISSING, m(UNDO, 1))]
    verified = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, FOUND))
    assert check(verified) == [(MISSING, m(APPLY, 1))]


def test_a_verified_apply_whose_marker_recovery_did_not_find_may_miss_it() -> None:
    # unknown -> verify_failed (not found, changed) -> undo unknown -> recovery: not found, unchanged -> verified.
    # The request is verified on recovery's conclusion that the apply left no marker: that must not block forever.
    verified = request(S.VERIFIED, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (UNDO, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(verified) == []
    assert check(verified, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


def test_an_undone_apply_whose_marker_recovery_found_must_still_be_found() -> None:
    # unknown -> applied (found) -> verified -> undone: the marker was proven present, so losing it is MISSING.
    undone = request(S.UNDONE, (APPLY, 1, OC.UNKNOWN, FOUND), (UNDO, 1, OC.ACCEPTED))
    assert check(undone, [m(APPLY, 1), m(UNDO, 1)]) == []
    assert check(undone, [m(UNDO, 1)]) == [(MISSING, m(APPLY, 1))]


def test_an_undone_apply_whose_marker_recovery_did_not_find_is_a_hit_when_it_lands_late() -> None:
    # The undo reversed what was observed; the apply landing after that is a second change.
    undone = request(S.UNDONE, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (UNDO, 1, OC.ACCEPTED))
    assert check(undone, [m(APPLY, 1), m(UNDO, 1)]) == [(HIT, m(APPLY, 1))]


def test_an_earlier_undo_attempt_of_an_undone_request_that_also_wrote_is_a_hit() -> None:
    undone = request(S.UNDONE, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN, NOT_FOUND), (UNDO, 2, OC.ACCEPTED))
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
def test_an_attempt_not_known_to_have_written_may_be_missing_in_any_state(state: RequestState) -> None:
    if state is S.APPLYING:
        one = request(state, (APPLY, 1, None))
    elif state in (S.UNKNOWN, S.UNDO_UNKNOWN):
        one = request(state, (APPLY, 1, OC.UNKNOWN))
    else:
        one = request(state, (APPLY, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(one) == []
    assert check(one, [m(APPLY, 2)]) == [(HIT, m(APPLY, 2))]


@pytest.mark.parametrize("state", [S.APPLIED, S.VERIFY_FAILED, S.ACCEPTED, S.UNDOING, S.UNDO_FAILED, S.UNDO_UNKNOWN])
@pytest.mark.parametrize("recovered", [False, True])
def test_an_apply_known_to_have_written_must_be_found_in_any_state(state: RequestState, recovered: bool) -> None:
    # verified -> undo refused (4xx) -> undo_failed, then the apply's marker is gone: the history an undo would be
    # built on is not trustworthy, so a retried undo must stop (and likewise from verify_failed).
    apply = (APPLY, 1, OC.UNKNOWN, FOUND) if recovered else (APPLY, 1, OC.ACCEPTED)
    undo: dict[RequestState, list[tuple[AttemptKind, int, Outcome | None]]] = {
        S.UNDOING: [(UNDO, 1, None)],
        S.UNDO_FAILED: [(UNDO, 1, OC.REFUSED)],
        S.UNDO_UNKNOWN: [(UNDO, 1, OC.UNKNOWN)],
    }
    one = request(state, apply, *undo.get(state, []))
    assert check(one, [m(APPLY, 1)]) == []
    assert check(one) == [(MISSING, m(APPLY, 1))]
    assert check_markers([one], {ID_1: []}, item_gone=[m(APPLY, 1)]) == []


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED])
def test_an_undo_known_to_have_written_must_be_found_in_any_state(state: RequestState) -> None:
    one = request(state, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN, FOUND))
    assert check(one, [m(APPLY, 1), m(UNDO, 1)]) == []
    assert check(one, [m(APPLY, 1)]) == [(MISSING, m(UNDO, 1))]


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
    twice = request(state, (APPLY, 1, OC.UNKNOWN, NOT_FOUND), (APPLY, 2, OC.ACCEPTED))
    assert check(twice, [m(APPLY, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(APPLY, 2)]) == [(HIT, m(APPLY, 1))]


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.UNDO_UNKNOWN, S.UNDOING])
def test_two_undo_attempts_that_both_wrote_are_a_hit_in_any_state(state: RequestState) -> None:
    second: tuple[AttemptKind, int, Outcome | None] | tuple[AttemptKind, int, Outcome | None, bool]
    second = (
        (UNDO, 2, OC.UNKNOWN)
        if state is S.UNDO_UNKNOWN
        else (UNDO, 2, None)
        if state is S.UNDOING
        else (UNDO, 2, OC.UNKNOWN, FOUND)
    )
    twice = request(state, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN, NOT_FOUND), second)
    assert check(twice, [m(APPLY, 1), m(UNDO, 2)]) == []
    assert check(twice, [m(APPLY, 1), m(UNDO, 1), m(UNDO, 2)]) == [(HIT, m(UNDO, 1))]


# What recovery concluded


def test_a_late_undo_after_recovery_did_not_find_it_is_a_hit() -> None:
    # verified -> undo u01 times out -> undo_unknown; recovery: not found, changed -> verify_failed. u01 then lands.
    # A second undo built from the apply-time history would reverse the apply twice.
    late = request(S.VERIFY_FAILED, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(late, [m(APPLY, 1)]) == []
    findings = check_markers([late], {ID_1: [m(APPLY, 1), m(UNDO, 1)]})
    assert findings == [
        Finding(KEY_1, HIT, m(UNDO, 1), "recovery did not find this attempt's marker, yet it wrote late")
    ]


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.UNDOING, S.UNDO_FAILED])
def test_a_late_apply_after_recovery_did_not_find_it_is_a_hit(state: RequestState) -> None:
    late = request(state, (APPLY, 1, OC.UNKNOWN, NOT_FOUND))
    assert check(late) == []
    assert check(late, [m(APPLY, 1)]) == [(HIT, m(APPLY, 1))]


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.UNDOING, S.UNDO_FAILED])
def test_an_attempt_recovery_found_explains_its_marker(state: RequestState) -> None:
    assert check(request(state, (APPLY, 1, OC.UNKNOWN, FOUND)), [m(APPLY, 1)]) == []


@pytest.mark.parametrize("outcome", [None, OC.ACCEPTED, OC.REFUSED])
@pytest.mark.parametrize("found", [FOUND, NOT_FOUND])
def test_only_an_unknown_attempt_is_recovered(outcome: Outcome | None, found: bool) -> None:
    with pytest.raises(ValueError, match="unknown outcome"):
        RecordedAttempt(m(APPLY, 1), outcome, found)


@pytest.mark.parametrize("state", [S.VERIFY_FAILED, S.ACCEPTED, S.VERIFIED, S.UNDONE, S.FAILED, S.APPROVED])
def test_an_unknown_attempt_without_recoverys_conclusion_is_refused_once_the_request_left_unknown(
    state: RequestState,
) -> None:
    attempts = [(APPLY, 1, OC.UNKNOWN)] + ([(UNDO, 1, OC.ACCEPTED)] if state is S.UNDONE else [])
    with pytest.raises(ValueError, match="recovery recorded"):
        request(state, *attempts)


def test_an_unknown_attempt_waiting_for_recovery_has_no_conclusion_yet() -> None:
    assert check(request(S.UNKNOWN, (APPLY, 1, OC.UNKNOWN))) == []
    assert check(request(S.UNDO_UNKNOWN, (APPLY, 1, OC.ACCEPTED), (UNDO, 1, OC.UNKNOWN)), [m(APPLY, 1)]) == []
    with pytest.raises(ValueError, match="recovery recorded"):
        request(S.UNDO_UNKNOWN, (APPLY, 1, OC.UNKNOWN), (UNDO, 1, OC.UNKNOWN))
