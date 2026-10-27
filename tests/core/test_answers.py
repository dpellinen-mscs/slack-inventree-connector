# SPDX-License-Identifier: MIT
"""What answers change about a reading (design section 4, "Acting on an answer")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pytest

from stockthread.core.answers import Given, Typed, clears_on_yes, replay
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
from stockthread.core.resolve import SourcePick
from stockthread.core.typed_answers import TypedAnswer
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
    stock: Sequence[SourcePick | None] = ()


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
    assert effects.cleared == {(1, None, ReasonCode.PHOTO_NOT_READ, "")}
    assert effects.rejected is None


def test_yes_clears_only_the_detail_it_was_asked_about() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0, detail="source and destination are the same")
    effects = replay([given(1, question, Choice("yes"))], [1], APPROVERS)
    assert effects.cleared == {(0, None, ReasonCode.CONTRADICTION, "source and destination are the same")}


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
    assert by_approver.cleared == {(0, 0, ReasonCode.QUANTITY_ABOVE_LIMIT, "")}
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


# The stock question (design section 4, "The stock question").

CHOOSE_STOCK = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
SPLIT = Question(Ask.CONFIRM_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
AGAIN = Question(Ask.READ_STOCK_AGAIN, ReasonCode.QUANTITY_ABOVE_STOCK, 0, 0)


def test_a_stock_item_picked_or_a_split_said_yes_to_names_the_lines_stock() -> None:
    picked = replay([given(1, CHOOSE_STOCK, Choice("record", record=57))], [1], APPROVERS)
    assert (picked.stock, picked.applied) == ({(0, 0): (57,)}, [1])
    split = replay([given(1, SPLIT, Choice("yes"))], [1], APPROVERS)
    assert split.stock == {(0, 0): (41, 57)}
    none = replay([given(1, CHOOSE_STOCK, Choice("none"))], [1], APPROVERS)
    assert none.stock == {(0, 0): ()}


def test_a_stock_item_that_was_not_offered_changes_nothing() -> None:
    effects = replay([given(1, CHOOSE_STOCK, Choice("record", record=60))], [1], APPROVERS)
    assert (effects.stock, effects.ignored) == ({}, [1])
    whole = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, None, None, (41,))
    assert replay([given(1, whole, Choice("record", record=41))], [1], APPROVERS).ignored == [1]


def test_drop_it_and_no_to_a_split_end_the_plan() -> None:
    assert replay([given(1, AGAIN, Choice("no"))], [1], APPROVERS).rejected == 1
    assert replay([given(1, SPLIT, Choice("no"))], [1], APPROVERS).rejected == 1
    assert replay([given(1, CHOOSE_STOCK, Choice("no"))], [1], APPROVERS).rejected is None


def test_read_the_stock_again_ends_the_lines_earlier_stock_answers_only() -> None:
    other = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, 1, None, (60,))
    part = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    effects = replay(
        [
            given(1, part, Choice("record", record=5)),
            given(2, CHOOSE_STOCK, Choice("none")),
            given(3, other, Choice("record", record=60)),
            given(4, AGAIN, Choice("again")),
        ],
        [2],
        APPROVERS,
    )
    assert effects.stock == {(0, 1): (60,)}
    assert effects.parts[(0, 0)].record == 5
    assert effects.applied == [1, 2, 3, 4]
    later = replay(
        [given(2, CHOOSE_STOCK, Choice("none")), given(4, AGAIN, Choice("again")), given(5, SPLIT, Choice("yes"))],
        [1],
        APPROVERS,
    )
    assert later.stock == {(0, 0): (41, 57)}


# Answers by thread reply (`Typed`).


def typed(
    question: Question, answer: TypedAnswer, by: str = SAM, item: int = 7, text: str = "typed", poster: str | None = SAM
) -> Typed:
    return Typed(item, question, answer, text, by, poster)


def test_a_typed_quantity_replaces_the_line_quantity_and_settles_every_doubt_about_it() -> None:
    question = Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 0)
    effects = replay([typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(3)))], [1], APPROVERS)
    assert effects.quantities == {(0, 0): Decimal(3)}
    assert effects.applied == [7]
    assert effects.retyped == {0}
    assert {code for _, _, code in effects.settled} == {
        ReasonCode.QUANTITY_MISSING,
        ReasonCode.QUANTITY_VAGUE,
        ReasonCode.QUANTITY_FROM_PHOTO,
        ReasonCode.QUANTITY_NOT_IN_TEXT,
    }
    assert effects.pieces == set()  # a pack-unit doubt is not settled by "how many?"


def test_a_typed_quantity_for_a_pack_unit_question_counts_pieces() -> None:
    question = Question(Ask.QUANTITY, ReasonCode.PACK_UNIT_AMBIGUOUS, 0, 0)
    effects = replay([typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(24)))], [1], APPROVERS)
    assert effects.pieces == {(0, 0)}
    assert (0, 0, ReasonCode.PACK_UNIT_AMBIGUOUS) in effects.settled


def test_a_typed_name_gives_the_matchers_result_and_keeps_the_typed_text() -> None:
    part = Question(Ask.NAME_PART, ReasonCode.PART_UNMATCHED, 0, 1)
    place = Question(Ask.NAME_LOCATION, ReasonCode.IMPLIED_SOURCE, 0, None, "source")
    loose = Match(MatchStatus.MATCHED, MatchRule.WORDS, 4)
    effects = replay(
        [
            typed(part, TypedAnswer(Ask.NAME_PART, match=loose), item=1, text="the pro one"),
            typed(place, TypedAnswer(Ask.NAME_LOCATION, match=EXACT), item=2, text="main stock"),
        ],
        [2],
        APPROVERS,
    )
    assert effects.parts == {(0, 1): loose}  # a loose match is confirmed by the next question
    assert effects.places == {(0, "source"): EXACT}
    assert effects.named == {(0, 1): "the pro one", (0, "source"): "main stock"}
    assert effects.applied == [1, 2]


def test_typed_serials_replace_the_lines_serials() -> None:
    question = Question(Ask.SERIALS, ReasonCode.SERIAL_UNCONFIRMED, 0, 0)
    effects = replay([typed(question, TypedAnswer(Ask.SERIALS, serials=("SN-1A", "SN-2B")))], [1], APPROVERS)
    assert effects.serials == {(0, 0): ("SN-1A", "SN-2B")}
    assert (0, 0, ReasonCode.SERIAL_UNCONFIRMED) in effects.settled


@pytest.mark.parametrize(
    ("question", "answer", "by"),
    [
        # for approvers, typed by the poster
        (Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 0, approver_only=True), Decimal(3), SAM),
        (Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 4), Decimal(3), SAM),  # a line the reading lacks
        (Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 2, 0), Decimal(3), SAM),  # a movement it lacks
        (Question(Ask.SERIALS, ReasonCode.SERIAL_UNCONFIRMED, 0, 0), Decimal(3), SAM),  # the answer of another ask
    ],
)
def test_a_typed_answer_that_does_not_fit_changes_nothing(question: Question, answer: Decimal, by: str) -> None:
    effects = replay([typed(question, TypedAnswer(Ask.QUANTITY, quantity=answer), by=by)], [1], APPROVERS)
    assert effects.ignored == [7]
    assert (effects.quantities, effects.settled, effects.retyped) == ({}, set(), set())


def test_a_typed_answer_counts_only_from_the_poster_or_someone_who_is_an_approver_now() -> None:
    question = Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 0)
    by_luis = typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(24)), by=LUIS)
    assert replay([by_luis], [1], APPROVERS).quantities == {(0, 0): Decimal(24)}
    removed = replay([by_luis], [1], frozenset())  # Luis answered as an approver and is one no longer
    assert removed.ignored == [7]
    assert (removed.quantities, removed.settled, removed.retyped) == ({}, set(), set())
    by_sam = typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(3)))
    assert replay([by_sam], [1], frozenset()).quantities == {(0, 0): Decimal(3)}  # the poster needs no list
    stranger = typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(3)), by="U0ANA0009")
    assert replay([stranger], [1], APPROVERS).ignored == [7]
    unknown = typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(3)), poster=None)
    assert replay([unknown], [1], APPROVERS).ignored == [7]  # no poster stored: only an approver counts


def test_a_typed_answer_after_a_rejection_is_not_applied() -> None:
    no = Given(1, Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0), Choice("no"), SAM)
    question = Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 0)
    effects = replay([no, typed(question, TypedAnswer(Ask.QUANTITY, quantity=Decimal(3)), item=2)], [1], APPROVERS)
    assert effects.rejected == 1
    assert effects.ignored == [2]
    assert effects.quantities == {}
