"""The write gate: the switch, the policy and validation each block alone; automatic apply is stricter than Apply."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from stockthread.config import parse_settings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import (
    CandidateMovement,
    LineItem,
    Match,
    MatchRule,
    MatchStatus,
    MovementType,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.resolve import SourcePick, Take
from stockthread.gate import Policy, Verdict, judge
from stockthread.slack.replies import MovementView

SAM = "U0SAM0001"
LUIS = "U0LUIS001"
LINK = "https://acme-fiber.slack.com/archives/C0ACME0001/p1700000000000100"
ON = Policy(True, frozenset({MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE}), frozenset({LUIS}))
SECRET_PREFIXES = {"SLACK_BOT_TOKEN": "xoxb-", "SLACK_APP_TOKEN": "xapp-", "INVENTREE_TOKEN": ""}
SAMS_TRUCK = 11
"""The destination `view` matches by default: Sam's mapped vehicle."""
TAKE_OUTS = (MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE)
CONFIRMED = (MovementType.RETURN, MovementType.RECEIVE, MovementType.TRANSFER, MovementType.CONSUME, MovementType.COUNT)


def matched(pk: int, rule: MatchRule = MatchRule.EXACT) -> Match:
    return Match(MatchStatus.MATCHED, rule, pk)


def view(movement_type: MovementType = MovementType.TAKE_TO_VEHICLE, **fields: Any) -> MovementView:
    has_source = movement_type not in (MovementType.RECEIVE, MovementType.COUNT)
    has_destination = movement_type is not MovementType.CONSUME
    movement = CandidateMovement(
        movement_type,
        (LineItem("Disc 40", Decimal(2)),),
        "main stock" if has_source else None,
        "my truck" if has_destination else None,
    )
    values: dict[str, Any] = {
        "movement": movement,
        "items": (matched(1),),
        "source": matched(10) if has_source else None,
        "destination": matched(11) if has_destination else None,
        "reasons": (),
        "stock": (SourcePick((Take(57, Decimal(2)),)),),
    }
    values.update(fields)
    return MovementView(**values)


def markers(count: int = 1) -> list[Marker]:
    return [Marker(new_marker_id(), AttemptKind.APPLY, 1) for _ in range(count)]


def run(views: list[MovementView], policy: Policy = ON, **given: Any) -> Any:
    arguments: dict[str, Any] = {
        "poster": SAM,
        "link": LINK,
        "answered": False,
        "rejected": False,
        "poster_vehicle": SAMS_TRUCK,
        **given,
    }
    return judge(views, markers(len(views)), policy, **arguments)


@pytest.mark.parametrize("movement_type", TAKE_OUTS)
def test_a_clear_take_out_is_allowed_by_the_policy_with_the_poster_in_the_notes(movement_type: MovementType) -> None:
    judgement = run([view(movement_type)])
    assert judgement.verdict is Verdict.ALLOWED
    assert judgement.allowed
    assert judgement.approved_by is None
    (request,) = judgement.requests
    assert SAM in request.body["notes"]


@pytest.mark.parametrize("movement_type", CONFIRMED)
def test_other_movement_types_wait_for_apply_even_with_no_reason(movement_type: MovementType) -> None:
    judgement = run([view(movement_type)])
    assert judgement.verdict is Verdict.WAITS
    assert judgement.requests == ()
    assert f"{movement_type.value} waits for Apply" in judgement.why


@pytest.mark.parametrize("movement_type", CONFIRMED + TAKE_OUTS)
def test_an_approvers_click_allows_any_decided_plan_with_the_approver_in_the_notes(
    movement_type: MovementType,
) -> None:
    judgement = run([view(movement_type)], answered=True, clicked_by=LUIS)
    assert judgement.allowed
    assert judgement.approved_by == LUIS
    (request,) = judgement.requests
    assert LUIS in request.body["notes"]
    assert SAM not in request.body["notes"]


def test_a_click_from_someone_who_is_not_an_approver_now_counts_for_nothing() -> None:
    judgement = run([view(MovementType.RETURN)], clicked_by=SAM)
    assert judgement.verdict is Verdict.WAITS
    assert judgement.approved_by is None
    removed = Policy(True, ON.auto_apply, frozenset())
    assert run([view(MovementType.RETURN)], removed, clicked_by=LUIS).verdict is Verdict.WAITS


def test_a_click_from_someone_who_is_not_an_approver_never_names_them_in_the_notes() -> None:
    judgement = run([view()], clicked_by="U0NOBODY1")
    assert judgement.allowed
    assert judgement.approved_by is None
    (request,) = judgement.requests
    assert SAM in request.body["notes"]
    assert "U0NOBODY1" not in request.body["notes"]


def test_an_empty_auto_apply_list_makes_every_movement_wait() -> None:
    review_first = Policy(True, frozenset(), ON.approvers)
    assert run([view()], review_first).verdict is Verdict.WAITS
    assert run([view()], review_first, clicked_by=LUIS).allowed


@pytest.mark.parametrize("clicked_by", [None, LUIS])
def test_the_switch_blocks_alone_whoever_clicks(clicked_by: str | None) -> None:
    off = Policy(False, ON.auto_apply, ON.approvers)
    judgement = run([view()], off, clicked_by=clicked_by)
    assert judgement.verdict is Verdict.BLOCKED
    assert judgement.why == "stock writes are off"
    assert judgement.requests == ()


@pytest.mark.parametrize("clicked_by", [None, LUIS])
def test_a_rejected_plan_is_blocked_whoever_clicks(clicked_by: str | None) -> None:
    judgement = run([view()], rejected=True, clicked_by=clicked_by)
    assert judgement.verdict is Verdict.BLOCKED
    assert "rejected" in judgement.why


@pytest.mark.parametrize(
    "undecided",
    [
        view(reasons=(ReviewReason(ReasonCode.IMPLIED_SOURCE, ReasonSource.RULE),)),
        view(reasons=(ReviewReason(ReasonCode.SUSPICIOUS_INSTRUCTIONS, ReasonSource.MODEL),)),
        view(items=(Match(MatchStatus.AMBIGUOUS, None, None, (1, 2)),)),
        view(items=(matched(1, MatchRule.FUZZY),)),
        view(destination=Match(MatchStatus.UNMATCHED)),
        view(stock=(None,)),
        view(stock=(SourcePick((Take(57, Decimal(1)),)),)),  # short of the line's 2
        view(MovementType.UNCLEAR),
    ],
)
@pytest.mark.parametrize("clicked_by", [None, LUIS])
def test_validation_blocks_alone_whoever_clicks(undecided: MovementView, clicked_by: str | None) -> None:
    judgement = run([undecided], clicked_by=clicked_by)
    assert judgement.verdict is Verdict.BLOCKED
    assert judgement.why.startswith("not decided: movement 1")
    assert judgement.requests == ()


def test_one_undecided_movement_blocks_the_whole_plan() -> None:
    judgement = run([view(), view(items=(Match(MatchStatus.UNMATCHED),))], clicked_by=LUIS)
    assert judgement.verdict is Verdict.BLOCKED
    assert "movement 2" in judgement.why


def test_one_movement_that_waits_makes_the_whole_plan_wait() -> None:
    judgement = run([view(), view(MovementType.RETURN)])
    assert judgement.verdict is Verdict.WAITS
    assert judgement.why.startswith("movement 2")
    assert run([view(), view(MovementType.RETURN)], clicked_by=LUIS).allowed


def test_an_answered_plan_waits_for_apply_even_as_a_clear_take_out() -> None:
    """A typed name keeps the matcher's rule and a stock choice changes no match: only `answered` says so."""
    judgement = run([view()], answered=True)
    assert judgement.verdict is Verdict.WAITS
    assert "answered" in judgement.why


@pytest.mark.parametrize(
    ("loose", "why", "given"),
    [
        (view(items=(matched(1, MatchRule.ANSWER),)), "a part was not found", {}),
        (view(source=matched(10, MatchRule.ANSWER)), "a place was not found", {}),
        (view(MovementType.TAKE_TO_SITE, destination=matched(11, MatchRule.ANSWER)), "a place was not found", {}),
        (view(destination=matched(12, MatchRule.NORMALIZED)), "the vehicle is not", {}),
        (view(destination=matched(12, MatchRule.ALIAS)), "the vehicle is not", {}),  # a learned alias, not Sam's
        (view(destination=matched(11, MatchRule.ALIAS)), "the vehicle is not", {"poster_vehicle": None}),
    ],
)
def test_automatic_apply_is_stricter_than_apply(loose: MovementView, why: str, given: dict[str, Any]) -> None:
    """A match by a button answer, or a vehicle found by a normalized name, needs a click even with `answered` False.

    This backs `answered` up for button answers only (see the test above for typed and stock answers)."""
    judgement = run([loose], **given)
    assert judgement.verdict is Verdict.WAITS
    assert why in judgement.why
    assert run([loose], clicked_by=LUIS).allowed


@pytest.mark.parametrize("rule", [MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS])
def test_parts_and_sites_by_exact_normalized_or_alias_rules_apply_automatically(rule: MatchRule) -> None:
    clear = view(
        MovementType.TAKE_TO_SITE, items=(matched(1, rule),), source=matched(10, rule), destination=matched(11, rule)
    )
    assert run([clear]).allowed


def test_a_model_flag_never_lets_the_policy_approve_even_if_the_gate_missed_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stockthread.plan.gate_reasons", lambda _view: set())
    flagged = view(reasons=(ReviewReason(ReasonCode.CONTRADICTION, ReasonSource.MODEL),))
    judgement = run([flagged])
    assert judgement.verdict is Verdict.WAITS
    assert "the model raised a flag" in judgement.why


@pytest.mark.parametrize("rule", [MatchRule.EXACT, MatchRule.ALIAS])
def test_the_posters_mapped_vehicle_or_a_vehicle_by_its_exact_name_applies_automatically(rule: MatchRule) -> None:
    assert run([view(destination=matched(SAMS_TRUCK, rule))]).allowed
    assert run([view(destination=matched(12, MatchRule.EXACT))]).allowed


def test_the_gate_reads_the_settings_it_is_given() -> None:
    data = {
        "slack": {"workspace_id": "T0ACME01", "app_id": "A0STOCK1", "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
    }
    environ = {name: prefix + "synthetic" for name, prefix in SECRET_PREFIXES.items()}
    fresh = Policy.of(parse_settings(data, environ))
    assert fresh == Policy(False, frozenset(TAKE_OUTS), frozenset({LUIS}))
    assert run([view()], fresh).verdict is Verdict.BLOCKED
    data["writes"] = {"enabled": True}
    data["policy"] = {"auto_apply": []}
    assert Policy.of(parse_settings(data, environ)) == Policy(True, frozenset(), frozenset({LUIS}))
