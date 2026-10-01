"""The one focused question per post (design section 4, "Questions")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from corpus import candidate, cases, eval_catalog

from stockthread.core.matching import match_location, match_part
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
from stockthread.core.questions import _BY_REASON, MAX_CHOICES, Ask, Question, pick_question
from stockthread.core.validate import validate
from stockthread.slack.replies import MovementView, gate_reasons


@dataclass(frozen=True)
class View:
    movement: CandidateMovement
    items: Sequence[Match]
    source: Match | None
    destination: Match | None
    reasons: Sequence[ReviewReason]


EXACT = Match(MatchStatus.MATCHED, MatchRule.EXACT, 1)


def take(*lines: LineItem, **fields: Any) -> CandidateMovement:
    values: dict[str, Any] = {
        "movement_type": MovementType.TAKE_TO_VEHICLE,
        "line_items": lines or (LineItem("Disc 40", Decimal(2)),),
        "source_text": "main stock",
        "destination_text": "truck 1",
    }
    values.update(fields)
    return CandidateMovement(**values)


def view(movement: CandidateMovement | None = None, *reasons: ReasonCode, **matches: Any) -> View:
    movement = movement or take()
    return View(
        movement,
        matches.get("items", tuple(EXACT for _ in movement.line_items)),
        matches.get("source", EXACT),
        matches.get("destination", EXACT),
        tuple(ReviewReason(code, ReasonSource.RULE) for code in reasons),
    )


def test_nothing_to_ask_without_a_reason() -> None:
    assert pick_question([view(), view()]) is None
    assert pick_question([]) is None


def test_every_reason_code_has_a_tier() -> None:
    assert set(_BY_REASON) == set(ReasonCode)


@pytest.mark.parametrize("code", list(ReasonCode))
def test_every_reason_alone_leads_to_a_question(code: ReasonCode) -> None:
    question = pick_question([view(take(), code)])
    assert question is not None
    assert question.reason is code


def test_doubts_about_the_reading_come_before_details() -> None:
    reasons = (ReasonCode.QUANTITY_VAGUE, ReasonCode.IMPLIED_SOURCE, ReasonCode.CONTRADICTION)
    assert pick_question([view(take(), *reasons)]) == Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0)


def test_an_unclear_type_is_asked_as_a_choice_of_type() -> None:
    unclear = take(movement_type=MovementType.UNCLEAR)
    reasons = validate(unclear, "2 Disc 40 from main stock to truck 1", max_quantity=100)
    assert pick_question([View(unclear, (EXACT,), EXACT, EXACT, reasons)]) == Question(
        Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, 0
    )


def test_a_hedge_on_an_unclear_type_is_confirmed_first() -> None:
    unclear = take(movement_type=MovementType.UNCLEAR)
    reasons = validate(unclear, "I think 2 Disc 40 from main stock to truck 1", max_quantity=100)
    assert pick_question([View(unclear, (EXACT,), EXACT, EXACT, reasons)]) == Question(
        Ask.CONFIRM, ReasonCode.MOVEMENT_UNCLEAR, 0
    )


def test_a_hedge_on_a_known_type_is_a_confirmation() -> None:
    question = pick_question([view(take(), ReasonCode.MOVEMENT_UNCLEAR)])
    assert question == Question(Ask.CONFIRM, ReasonCode.MOVEMENT_UNCLEAR, 0)


def test_items_come_before_quantities_and_places() -> None:
    lines = (LineItem("Disc 40", None), LineItem("psu", Decimal(1)))
    ambiguous = Match(MatchStatus.AMBIGUOUS, candidates=(4, 5))
    question = pick_question(
        [view(take(*lines), ReasonCode.QUANTITY_MISSING, ReasonCode.IMPLIED_SOURCE, items=(EXACT, ambiguous))]
    )
    assert question == Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 1, None, (4, 5))


def test_too_many_candidates_are_asked_as_a_reply() -> None:
    many = Match(MatchStatus.AMBIGUOUS, candidates=tuple(range(1, MAX_CHOICES + 2)))
    assert pick_question([view(items=(many,))]) == Question(Ask.NAME_PART, ReasonCode.PART_AMBIGUOUS, 0, 0)
    five = Match(MatchStatus.AMBIGUOUS, candidates=tuple(range(1, MAX_CHOICES + 1)))
    assert pick_question([view(items=(five,))]).ask is Ask.CHOOSE_PART  # type: ignore[union-attr]


def test_a_loose_match_is_confirmed_with_its_record() -> None:
    loose = Match(MatchStatus.MATCHED, MatchRule.FUZZY, 7)
    assert pick_question([view(items=(loose,))]) == Question(Ask.CONFIRM_PART, ReasonCode.FUZZY_MATCH, 0, 0, None, (7,))
    assert pick_question([view(destination=loose)]) == Question(
        Ask.CONFIRM_LOCATION, ReasonCode.FUZZY_MATCH, 0, None, "destination", (7,)
    )


def test_an_unmatched_part_is_named_in_a_reply() -> None:
    lines = (LineItem("Disc 40", Decimal(2)), LineItem("widget", Decimal(1)))
    unmatched = Match(MatchStatus.UNMATCHED)
    assert pick_question([view(take(*lines), items=(EXACT, unmatched))]) == Question(
        Ask.NAME_PART, ReasonCode.PART_UNMATCHED, 0, 1
    )


def test_a_missing_item_is_asked_once_not_also_as_unmatched() -> None:
    missing = take(LineItem(None, Decimal(2)))
    question = pick_question([view(missing, ReasonCode.ITEMS_MISSING, items=(Match(MatchStatus.UNMATCHED),))])
    assert question == Question(Ask.NAME_PART, ReasonCode.ITEMS_MISSING, 0, 0)


def test_the_quantity_question_names_its_line() -> None:
    lines = (LineItem("Disc 40", Decimal(2)), LineItem("psu", None))
    question = pick_question([view(take(*lines), ReasonCode.QUANTITY_MISSING)])
    assert question == Question(Ask.QUANTITY, ReasonCode.QUANTITY_MISSING, 0, 1)
    serials = ReviewReason(ReasonCode.SERIAL_UNCONFIRMED, ReasonSource.RULE, "line 2: serial not in the text")
    question = pick_question([View(take(*lines), (EXACT, EXACT), EXACT, EXACT, (serials,))])
    assert question == Question(Ask.SERIALS, ReasonCode.SERIAL_UNCONFIRMED, 0, 1)


def test_the_source_comes_before_the_destination() -> None:
    no_source = take(source_text=None)
    question = pick_question([view(no_source, ReasonCode.IMPLIED_SOURCE, ReasonCode.IMPLIED_DESTINATION, source=None)])
    assert question == Question(Ask.NAME_LOCATION, ReasonCode.IMPLIED_SOURCE, 0, None, "source")
    ambiguous = Match(MatchStatus.AMBIGUOUS, candidates=(101, 102))
    assert pick_question([view(destination=ambiguous)]) == Question(
        Ask.CHOOSE_LOCATION, ReasonCode.LOCATION_AMBIGUOUS, 0, None, "destination", (101, 102)
    )


def test_approval_reasons_come_last() -> None:
    question = pick_question([view(take(), ReasonCode.LATE_INGEST, ReasonCode.SERIAL_UNCONFIRMED)])
    assert question is not None
    assert question.ask is Ask.SERIALS
    assert pick_question([view(take(), ReasonCode.LATE_INGEST)]) == Question(Ask.CONFIRM, ReasonCode.LATE_INGEST, 0)


def test_a_tier_goes_across_movements_before_the_next_tier() -> None:
    first = view(take(), ReasonCode.IMPLIED_SOURCE)
    second = view(take(), ReasonCode.CONTRADICTION)
    assert pick_question([first, second]) == Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 1)
    assert pick_question([first, view(take(), ReasonCode.IMPLIED_SOURCE)]).movement == 0  # type: ignore[union-attr]


def test_a_match_reason_among_the_reasons_does_not_hide_the_match_question() -> None:
    loose = Match(MatchStatus.MATCHED, MatchRule.WORDS, 7)
    question = pick_question([view(take(), ReasonCode.FUZZY_MATCH, items=(loose,))])
    assert question == Question(Ask.CONFIRM_PART, ReasonCode.FUZZY_MATCH, 0, 0, None, (7,))


CATALOG = eval_catalog()[0]


def corpus_views(case: dict[str, Any]) -> list[MovementView]:
    """The expected movements of a case, matched against the eval catalog and validated by rule."""
    text = "\n".join(message.get("text") or "" for message in case["post"]["messages"])
    poster = case["post"]["user"]
    views = []
    for expected in case["expected"]["movements"]:
        movement = candidate(expected)
        views.append(
            MovementView(
                movement,
                tuple(
                    match_part(item.item_text, CATALOG, poster) if item.item_text else Match(MatchStatus.UNMATCHED)
                    for item in movement.line_items
                ),
                match_location(movement.source_text, CATALOG, poster) if movement.source_text else None,
                match_location(movement.destination_text, CATALOG, poster) if movement.destination_text else None,
                validate(movement, text, max_quantity=100),
            )
        )
    return views


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["id"])
def test_a_corpus_case_gets_a_question_exactly_when_the_reply_says_it_needs_a_human(case: dict[str, Any]) -> None:
    views = corpus_views(case)
    question = pick_question(views)
    assert (question is not None) == any(gate_reasons(v) for v in views)
    if question is not None:
        target = views[question.movement]
        assert question.line is None or 0 <= question.line < len(target.movement.line_items)
        if question.ask in (Ask.CHOOSE_PART, Ask.CHOOSE_LOCATION):
            assert 2 <= len(question.candidates) <= MAX_CHOICES


@pytest.mark.parametrize(
    ("case_id", "ask", "reason"),
    [
        ("clear-001", Ask.CONFIRM_PART, ReasonCode.FUZZY_MATCH),  # a whole-word match, before the implied source
        ("informal-001", Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS),
        ("informal-003", Ask.NAME_LOCATION, ReasonCode.IMPLIED_SOURCE),
        ("ambig-009", Ask.NAME_PART, ReasonCode.ITEMS_MISSING),
        ("ambig-015", Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR),
        ("adv-001", Ask.CONFIRM, ReasonCode.SUSPICIOUS_INSTRUCTIONS),
        ("clear-003", None, None),
    ],
)
def test_the_expected_question_of_some_corpus_cases(case_id: str, ask: Ask | None, reason: ReasonCode | None) -> None:
    question = pick_question(corpus_views(next(case for case in cases() if case["id"] == case_id)))
    assert (question.ask, question.reason) == (ask, reason) if question else ask is None


def test_a_line_named_in_a_detail_is_the_line_asked() -> None:
    lines = (LineItem("Disc 40", Decimal(2)), LineItem("Disc 60", Decimal(1)))
    reasons = validate(take(*lines), "took 2 Disc 40 from main stock to truck 1", max_quantity=100)
    question = pick_question([View(take(*lines), (EXACT, EXACT), EXACT, EXACT, reasons)])
    assert question == Question(Ask.CONFIRM, ReasonCode.ITEM_NOT_IN_TEXT, 0, 1)


@pytest.mark.parametrize("detail", ["line 0: x", "line 3: x", "line 9"])
def test_a_line_out_of_range_is_not_named(detail: str) -> None:
    lines = (LineItem("Disc 40", Decimal(2)), LineItem("Disc 60", Decimal(1)))
    reason = ReviewReason(ReasonCode.SERIAL_UNCONFIRMED, ReasonSource.RULE, detail)
    question = pick_question([View(take(*lines), (EXACT, EXACT), EXACT, EXACT, (reason,))])
    assert question == Question(Ask.SERIALS, ReasonCode.SERIAL_UNCONFIRMED, 0, None)


def test_a_photo_read_into_items_is_confirmed_not_asked_again() -> None:
    assert pick_question([view(take(), ReasonCode.PHOTO_ONLY)]) == Question(Ask.CONFIRM, ReasonCode.PHOTO_ONLY, 0)
    empty = take(LineItem(None, None))
    assert pick_question([view(empty, ReasonCode.PHOTO_ONLY)]).ask is Ask.NAME_PART  # type: ignore[union-attr]
    nothing = CandidateMovement(MovementType.UNCLEAR, (), None, None)
    assert pick_question(
        [View(nothing, (), None, None, (ReviewReason(ReasonCode.PHOTO_ONLY, ReasonSource.RULE),))]
    ) == (Question(Ask.NAME_PART, ReasonCode.PHOTO_ONLY, 0))


def test_a_stated_place_without_a_match_is_asked_by_its_side() -> None:
    blank = take(source_text="")
    assert pick_question([view(blank, source=None)]) == Question(
        Ask.NAME_LOCATION, ReasonCode.LOCATION_UNMATCHED, 0, None, "source"
    )


def test_a_whole_movement_reason_comes_before_a_line_in_its_tier() -> None:
    ambiguous = Match(MatchStatus.AMBIGUOUS, candidates=(4, 5))
    question = pick_question([view(take(), ReasonCode.ITEMS_MISSING, items=(ambiguous,))])
    assert question is not None
    assert question.reason is ReasonCode.ITEMS_MISSING
