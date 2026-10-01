"""What answers change about a reading (design section 4, "Acting on an answer")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest

from stockthread.core.answers import Given, clears_on_yes, replay
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
from stockthread.core.questions import (
    _BY_REASON,
    Ask,
    Choice,
    Question,
    Tier,
    choice_json,
    pick_question,
    question_json,
    tier_of,
)
from stockthread.core.validate import TYPE_UNCLEAR

SAM, LUIS = "U0SAM0001", "U0LUIS0005"
APPROVERS = frozenset({LUIS})
EXACT = Match(MatchStatus.MATCHED, MatchRule.EXACT, 1)


@dataclass(frozen=True)
class View:
    movement: CandidateMovement
    items: Sequence[Match]
    source: Match | None
    destination: Match | None
    reasons: Sequence[ReviewReason]


def view(kind: MovementType, *reasons: ReviewReason) -> View:
    movement = CandidateMovement(kind, (LineItem("Disc 40", Decimal(2)),), "main stock", "truck 1")
    return View(movement, (EXACT,), EXACT, EXACT, reasons)


def given(item: int, question: Question, choice: Choice, by: str = SAM) -> Given:
    return Given(item, question, choice, by)


def test_a_part_picked_from_the_list_becomes_a_match_by_answer() -> None:
    question = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 1, None, (4, 5))
    effects = replay([given(7, question, Choice("record", record=5))], [2], APPROVERS)
    assert effects.parts == {(0, 1): Match(MatchStatus.MATCHED, MatchRule.ANSWER, 5)}
    assert effects.parts[(0, 1)].can_authorize_a_write
    assert effects.applied == [7]
    assert effects.ignored == []


def test_none_of_these_and_no_make_the_line_unmatched() -> None:
    choose = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    confirm = Question(Ask.CONFIRM_LOCATION, ReasonCode.FUZZY_MATCH, 0, None, "destination", (11,))
    effects = replay([given(1, choose, Choice("none")), given(2, confirm, Choice("no"))], [1], APPROVERS)
    assert effects.parts == {(0, 0): Match(MatchStatus.UNMATCHED)}
    assert effects.places == {(0, "destination"): Match(MatchStatus.UNMATCHED)}
    assert effects.rejected is None


def test_yes_confirms_the_loosely_matched_record() -> None:
    confirm = Question(Ask.CONFIRM_LOCATION, ReasonCode.FUZZY_MATCH, 0, None, "source", (10,))
    effects = replay([given(1, confirm, Choice("yes"))], [1], APPROVERS)
    assert effects.places == {(0, "source"): Match(MatchStatus.MATCHED, MatchRule.ANSWER, 10)}


def test_a_record_that_was_not_offered_changes_nothing() -> None:
    question = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    effects = replay([given(1, question, Choice("record", record=6))], [1], APPROVERS)
    assert effects.parts == {}
    assert effects.ignored == [1]


@pytest.mark.parametrize(
    "question",
    [
        Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 1, 0, None, (4, 5)),  # no movement 2
        Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 3, None, (4, 5)),  # no line 4
        Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, None, None, (4, 5)),  # no line at all
        Question(Ask.CHOOSE_LOCATION, ReasonCode.LOCATION_AMBIGUOUS, 0, None, None, (10, 11)),  # no side
    ],
)
def test_an_answer_that_does_not_fit_the_reading_changes_nothing(question: Question) -> None:
    choice = Choice("record", record=question.candidates[0])
    effects = replay([given(1, question, choice)], [2], APPROVERS)
    assert (effects.parts, effects.places, effects.applied, effects.ignored) == ({}, {}, [], [1])


def test_a_movement_type_picked_replaces_the_unclear_one() -> None:
    question = Question(Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, 0)
    effects = replay([given(1, question, Choice("type", movement_type=MovementType.RETURN))], [1], APPROVERS)
    assert effects.types == {0: MovementType.RETURN}


def test_yes_to_a_doubt_about_the_reading_clears_that_code_for_that_movement_only() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.PHOTO_NOT_READ, 1)
    effects = replay([given(1, question, Choice("yes"))], [1, 1], APPROVERS)
    assert effects.cleared == {(1, None, ReasonCode.PHOTO_NOT_READ)}
    assert effects.rejected is None


def test_no_to_a_doubt_about_the_reading_ends_the_plan_and_nothing_after_it_counts() -> None:
    doubt = Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0)
    later = Question(Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, 0)
    effects = replay(
        [given(3, doubt, Choice("no")), given(4, later, Choice("type", movement_type=MovementType.RETURN))],
        [1],
        APPROVERS,
    )
    assert effects.rejected == 3
    assert effects.applied == [3]
    assert effects.ignored == [4]
    assert effects.types == {}


def test_an_approval_answer_counts_only_from_someone_who_is_an_approver_now() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.QUANTITY_ABOVE_LIMIT, 0, 0, approver_only=True)
    by_approver = replay([given(1, question, Choice("yes"), LUIS)], [1], APPROVERS)
    assert by_approver.cleared == {(0, 0, ReasonCode.QUANTITY_ABOVE_LIMIT)}
    no_longer = replay([given(1, question, Choice("yes"), LUIS)], [1], frozenset())
    assert (no_longer.cleared, no_longer.ignored) == (set(), [1])
    rejected = replay([given(1, question, Choice("no"), SAM)], [1], APPROVERS)
    assert (rejected.rejected, rejected.ignored) == (None, [1])


def test_yes_to_a_stock_doubt_clears_nothing_yet_and_no_ends_the_plan() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.STOCK_CHOICE, 0)
    assert not clears_on_yes(question)
    effects = replay([given(1, question, Choice("yes"))], [1], APPROVERS)
    assert (effects.cleared, effects.ignored) == (set(), [1])
    assert replay([given(1, question, Choice("no"))], [1], APPROVERS).rejected == 1


def test_a_later_answer_about_the_same_line_wins() -> None:
    question = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    effects = replay(
        [given(1, question, Choice("record", record=4)), given(2, question, Choice("record", record=5))],
        [1],
        APPROVERS,
    )
    assert effects.parts[(0, 0)].record == 5


def test_an_answer_is_read_back_from_what_the_store_holds() -> None:
    question = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    choice = Choice("record", record=4)
    stored = Given.stored(9, question_json(question), {"button": 0, **choice_json(choice)}, SAM)
    assert stored == Given(9, question, choice, SAM)
    with pytest.raises(ValueError, match="not a choice"):
        Given.stored(9, question_json(question), {"button": 0, "kind": "maybe", "record": None}, SAM)


@pytest.mark.parametrize("code", list(ReasonCode))
def test_the_tier_of_a_stored_question_is_the_tier_it_was_asked_in(code: ReasonCode) -> None:
    question = pick_question([view(MovementType.TAKE_TO_VEHICLE, ReviewReason(code, ReasonSource.RULE))])
    assert question is not None
    expected = Tier.READING if code is ReasonCode.PHOTO_ONLY else _BY_REASON[code][0]  # the line has an item
    assert tier_of(question) is expected


def test_the_type_question_is_in_the_type_tier() -> None:
    unclear = ReviewReason(ReasonCode.MOVEMENT_UNCLEAR, ReasonSource.RULE, TYPE_UNCLEAR)
    question = pick_question([view(MovementType.UNCLEAR, unclear)])
    assert question is not None
    assert question.ask is Ask.MOVEMENT_TYPE
    assert tier_of(question) is Tier.TYPE
    assert not clears_on_yes(question)
