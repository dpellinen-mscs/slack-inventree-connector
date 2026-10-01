"""A question in the thread: its words and buttons, and that post text can never become Slack markup."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from corpus import cases, corpus_views, eval_catalog

from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType, ReasonCode
from stockthread.core.questions import Ask, Choice, Question, choices, pick_question
from stockthread.slack.replies import MAX_LABEL, REASONS, MovementView, question_message

PARTS = {1: "Norvane Disc 40 Access Point", 2: "Norvane Disc 60 Access Point", 3: "x" * 100}
LOCATIONS = {10: "Warehouse/Main Stock", 11: "Vehicles/Truck 1", 12: "Vehicles/Truck 2", 13: "<!channel>", 14: "  "}
LONG_PATH = "Region/" * 20 + "Shelf A"
EXACT = Match(MatchStatus.MATCHED, MatchRule.EXACT, 1)


def view(item: str | None = "Disc 40", **fields: Any) -> MovementView:
    movement = CandidateMovement(
        fields.pop("movement_type", MovementType.TAKE_TO_VEHICLE),
        (LineItem(item, Decimal(2)),),
        fields.pop("source_text", "main stock"),
        fields.pop("destination_text", "truck 1"),
    )
    return MovementView(movement, (EXACT,), EXACT, EXACT, ())


def ids(question: Question) -> list[str]:
    return [f"button{n}" for n, _ in enumerate(choices(question))]


def message(question: Question, *views: MovementView) -> dict[str, Any]:
    return question_message(question, views or (view(),), PARTS, LOCATIONS, ids(question))


def text_of(body: dict[str, Any]) -> str:
    return str(body["blocks"][0]["text"]["text"])


def buttons(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [] if len(body["blocks"]) < 2 else list(body["blocks"][1]["elements"])


def test_choices_follow_the_kind_of_question() -> None:
    assert choices(Question(Ask.CONFIRM, ReasonCode.LATE_INGEST, 0)) == (Choice("yes"), Choice("no"))
    assert choices(Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (1, 2))) == (
        Choice("record", 1),
        Choice("record", 2),
        Choice("none"),
    )
    types = choices(Question(Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, 0))
    assert {choice.movement_type for choice in types} == set(MovementType) - {MovementType.UNCLEAR}
    for ask in (Ask.NAME_PART, Ask.NAME_LOCATION, Ask.QUANTITY, Ask.SERIALS):
        assert choices(Question(ask, ReasonCode.IMPLIED_SOURCE, 0)) == ()


def test_a_choice_of_parts_has_one_button_per_candidate_and_none_of_these() -> None:
    body = message(Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (1, 2)))
    assert text_of(body) == "Which part is “Disc 40”?"
    shown = buttons(body)
    assert [b["text"]["text"] for b in shown] == [PARTS[1], PARTS[2], "None of these"]
    assert [b["value"] for b in shown] == ["button0", "button1", "button2"]
    assert [b["action_id"] for b in shown] == ["answer-0", "answer-1", "answer-2"]


def test_a_long_name_is_cut_to_fit_a_button() -> None:
    body = message(Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (1, 3)))
    label = buttons(body)[1]["text"]["text"]
    assert len(label) == MAX_LABEL
    assert label.endswith("…")


def test_a_loose_location_match_is_confirmed_by_name() -> None:
    question = Question(Ask.CONFIRM_LOCATION, ReasonCode.FUZZY_MATCH, 0, None, "destination", (11,))
    body = message(question)
    assert text_of(body) == "Is “truck 1” *Vehicles/Truck 1*?"
    assert body["text"] == "Is “truck 1” Vehicles/Truck 1?"
    assert [b["text"]["text"] for b in buttons(body)] == ["Yes", "No"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (Question(Ask.NAME_PART, ReasonCode.PART_UNMATCHED, 0, 0), "Which part is “Disc 40”?"),
        (Question(Ask.QUANTITY, ReasonCode.QUANTITY_VAGUE, 0, 0), "How many “Disc 40”?"),
        (Question(Ask.NAME_LOCATION, ReasonCode.IMPLIED_SOURCE, 0, None, "source"), "Which stock location is"),
        (Question(Ask.SERIALS, ReasonCode.SERIAL_UNCONFIRMED, 0, 0), "Which serial numbers of “Disc 40”?"),
        (Question(Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, 0), "What kind of movement is this?"),
        (Question(Ask.CONFIRM, ReasonCode.LATE_INGEST, 0), REASONS[ReasonCode.LATE_INGEST]),
    ],
)
def test_each_kind_of_question_has_its_words(question: Question, expected: str) -> None:
    body = message(question)
    assert text_of(body).startswith(expected)
    assert len(buttons(body)) == len(choices(question))


def test_an_unstated_place_is_asked_by_its_side() -> None:
    no_source = view(source_text=None)
    question = Question(Ask.NAME_LOCATION, ReasonCode.IMPLIED_SOURCE, 0, None, "source")
    assert text_of(message(question, no_source)).startswith("Where was it taken from?")
    no_destination = view(destination_text=None)
    question = Question(Ask.NAME_LOCATION, ReasonCode.IMPLIED_DESTINATION, 0, None, "destination")
    assert text_of(message(question, no_destination)).startswith("Where did it go?")


def test_a_missing_item_is_asked_without_quoting_it() -> None:
    question = Question(Ask.NAME_PART, ReasonCode.ITEMS_MISSING, 0, 0)
    assert text_of(message(question, view(None))).startswith("Which item was moved?")


def test_a_post_with_several_movements_names_the_movement() -> None:
    question = Question(Ask.QUANTITY, ReasonCode.QUANTITY_VAGUE, 1, 0)
    assert text_of(message(question, view(), view("psu"))).startswith("Movement 2: How many “psu”?")


def test_post_text_cannot_become_markup() -> None:
    question = Question(Ask.NAME_PART, ReasonCode.PART_UNMATCHED, 0, 0)
    shown = text_of(message(question, view("<!channel> <https://x.example|psu>")))
    assert "<" not in shown
    assert ">" not in shown


def test_every_choice_needs_a_stored_button_id() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.LATE_INGEST, 0)
    for values in ([], ["a"], ["a", ""], ["a", "b", "c"]):
        with pytest.raises(ValueError, match="stored button id"):
            question_message(question, [view()], PARTS, LOCATIONS, values)


def test_names_that_look_alike_get_their_key() -> None:
    names = {**PARTS, 4: "Disc 40", 5: "Disc  40"}
    question = Question(Ask.CHOOSE_PART, ReasonCode.PART_AMBIGUOUS, 0, 0, None, (4, 5))
    body = question_message(question, [view()], names, LOCATIONS, ids(question))
    assert [b["text"]["text"] for b in buttons(body)] == ["Disc 40 #4", "Disc 40 #5", "None of these"]


def test_a_long_location_path_keeps_its_end() -> None:
    question = Question(Ask.CHOOSE_LOCATION, ReasonCode.LOCATION_AMBIGUOUS, 0, None, "destination", (11, 15))
    body = question_message(question, [view()], PARTS, {**LOCATIONS, 15: LONG_PATH}, ids(question))
    label = buttons(body)[1]["text"]["text"]
    assert len(label) == MAX_LABEL
    assert label.startswith("…")
    assert label.endswith("Shelf A")


def test_an_empty_name_is_shown_by_its_key() -> None:
    question = Question(Ask.CHOOSE_LOCATION, ReasonCode.LOCATION_AMBIGUOUS, 0, None, "destination", (11, 14))
    assert buttons(message(question))[1]["text"]["text"] == "#14"


def test_catalog_names_and_places_cannot_become_markup() -> None:
    question = Question(Ask.CONFIRM_LOCATION, ReasonCode.FUZZY_MATCH, 0, None, "destination", (13,))
    shown = text_of(message(question, view(destination_text="<@U0SAM0001> truck")))
    assert "<" not in shown
    assert ">" not in shown


def test_a_confirmation_about_a_line_names_its_item() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.ITEM_NOT_IN_TEXT, 0, 0)
    assert text_of(message(question)).startswith("About “Disc 40”: ")


def test_the_notification_keeps_asterisks_of_the_post() -> None:
    question = Question(Ask.CONFIRM_PART, ReasonCode.FUZZY_MATCH, 0, 0, None, (1,))
    body = message(question, view("2*4 bracket"))
    assert body["text"] == f"Is “2*4 bracket” {PARTS[1]}?"


_, CORPUS_PARTS, CORPUS_LOCATIONS = eval_catalog()


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["id"])
def test_every_corpus_question_can_be_shown(case: dict[str, Any]) -> None:
    views = corpus_views(case)
    question = pick_question(views)
    if question is None:
        return
    body = question_message(question, views, CORPUS_PARTS, CORPUS_LOCATIONS, ids(question))
    assert text_of(body)
    labels = [b["text"]["text"] for b in buttons(body)]
    assert all(0 < len(label) <= MAX_LABEL for label in labels)
    assert len(set(labels)) == len(labels)
