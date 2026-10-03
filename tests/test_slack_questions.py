"""A question in the thread: its words and buttons, and that post text can never become Slack markup."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import pytest
from corpus import cases, corpus_views, eval_catalog

from stockthread.core import resolve
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType, ReasonCode
from stockthread.core.questions import (
    Ask,
    Choice,
    Question,
    choice_from_json,
    choice_json,
    choices,
    pick_question,
    question_from_json,
    question_json,
)
from stockthread.slack.replies import (
    MAX_BLOCKS,
    MAX_LABEL,
    MAX_MOVEMENTS,
    REASONS,
    MovementView,
    dry_run_reply,
    question_message,
    with_question,
)

PARTS = {1: "Norvane Disc 40 Access Point", 2: "Norvane Disc 60 Access Point", 3: "x" * 100}
LOCATIONS = {10: "Warehouse/Main Stock", 11: "Vehicles/Truck 1", 12: "Vehicles/Truck 2", 13: "<!channel>", 14: "  "}
LONG_PATH = "Region/" * 20 + "Shelf A"
EXACT = Match(MatchStatus.MATCHED, MatchRule.EXACT, 1)
_KINDS = "|".join(kind.value for kind in MovementType)
_RULES = "|".join(rule.value for rule in MatchRule)
_STOCK_DETAILS = [value for name, value in vars(resolve).items() if name.startswith("DETAIL_")]
_DETAILS = (
    r"line \d+",
    r"line \d+: (above policy\.max_quantity|serial count differs from the quantity|serial not in the text)",
    r"line \d+: the unit is not the one in the text",
    "no number|no text|type unclear|container word in the text|vague quantity word in the text",
    "correction word in the text|someone else is named as the actor|a person is mentioned",
    f"no word in the text supports ({_KINDS})",
    "question or request|plan or condition|a time ahead|negation|hedge|source|destination",
    "the text does not put the (source|destination) on that side",
    f"a ({_KINDS}) whose destination is a vehicle|take_to_vehicle whose destination names no vehicle",
    "a receive has no source location|zero quantity outside a count|source and destination are the same",
    f"(part|location) found by the ({_RULES}) rule",
    rf"line \d+: ({'|'.join(re.escape(d) for d in _STOCK_DETAILS)})",
)
RULE_DETAIL = re.compile("|".join(f"({detail})" for detail in _DETAILS))
"""Every detail the rules write (core/validate.py, Match.review_reason): fixed words, never words of the post."""


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


def test_a_confirmation_says_which_doubt_of_its_kind_it_asks_about() -> None:
    same = Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0, detail="source and destination are the same")
    assert "contradicts itself (source and destination are the same). Is" in text_of(message(same))
    limit = Question(Ask.CONFIRM, ReasonCode.ITEM_NOT_IN_TEXT, 0, 0, detail="line 1")
    assert "()" not in text_of(message(limit))
    assert "(line" not in text_of(message(limit))


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


@pytest.mark.parametrize("case", cases(), ids=lambda case: case["id"])
def test_every_corpus_question_is_stored_as_ids_and_read_back(case: dict[str, Any]) -> None:
    views = corpus_views(case)
    question = pick_question(views)
    if question is None:
        return
    stored = json.loads(json.dumps(question_json(question)))
    assert question_from_json(stored) == question
    for choice in choices(question):
        assert choice_from_json(json.loads(json.dumps(choice_json(choice)))) == choice
    # Codes, indexes and keys only: no words of the post.
    codes = {ask.value for ask in Ask} | {code.value for code in ReasonCode} | {"source", "destination"}
    detail = stored.pop("detail")
    assert detail == "" or RULE_DETAIL.fullmatch(detail), detail
    for name, value in stored.items():
        for leaf in value if isinstance(value, list) else [value]:
            assert leaf is None or isinstance(leaf, int) or leaf in codes, name


@pytest.mark.parametrize(
    "stored",
    [
        {"kind": "record", "record": None, "movement_type": None},
        {"kind": "yes", "record": 1, "movement_type": None},
        {"kind": "type", "record": None, "movement_type": None},
        {"kind": "maybe", "record": None, "movement_type": None},
        {"kind": "record", "record": True, "movement_type": None},
        {"kind": "record", "record": -1, "movement_type": None},
        {"kind": "type", "record": None, "movement_type": "unclear"},
        {"kind": "yes", "record": None},
    ],
)
def test_a_stored_choice_that_does_not_fit_its_kind_is_refused(stored: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"choice|index"):
        choice_from_json(stored)


def test_a_stored_question_with_a_bad_side_or_index_is_refused() -> None:
    good = question_json(Question(Ask.CHOOSE_LOCATION, ReasonCode.LOCATION_AMBIGUOUS, 0, None, "source", (10, 11)))
    bad_ones: list[dict[str, Any]] = [
        {"side": "middle"},
        {"movement": "0"},
        {"candidates": [10, False]},
        {"line": -1},
        {"approver_only": 1},
        {"approver_only": "true"},
        {"candidates": "10"},
    ]
    for bad in bad_ones:
        with pytest.raises(ValueError, match=r"side|index|stored question"):
            question_from_json({**good, **bad})
    without = {name: value for name, value in good.items() if name != "approver_only"}
    with pytest.raises(ValueError, match="stored question"):
        question_from_json(without)
    with pytest.raises(ValueError, match="stored question"):
        question_from_json({**good, "detail": None})


def test_a_question_stored_before_details_were_kept_has_none() -> None:
    stored = question_json(Question(Ask.CONFIRM, ReasonCode.CONTRADICTION, 0, detail="zero quantity outside a count"))
    del stored["detail"]
    assert question_from_json(stored).detail == ""  # so its "yes" clears no detailed reason: that one is asked again


def test_the_question_goes_below_the_reply_in_one_message() -> None:
    question = Question(Ask.CONFIRM, ReasonCode.LATE_INGEST, 0, approver_only=True)
    reply = dry_run_reply([view()], PARTS, LOCATIONS)
    body = with_question(reply, message(question))
    assert body["text"] == reply["text"]
    assert body["blocks"][: len(reply["blocks"])] == reply["blocks"]
    assert body["blocks"][len(reply["blocks"])] == {"type": "divider", "block_id": "question"}
    assert body["blocks"][-2:] == message(question)["blocks"]


def test_the_longest_reply_still_has_room_for_its_question() -> None:
    question = Question(Ask.MOVEMENT_TYPE, ReasonCode.MOVEMENT_UNCLEAR, MAX_MOVEMENTS + 4)
    views = [view() for _ in range(MAX_MOVEMENTS + 5)]
    body = with_question(dry_run_reply(views, PARTS, LOCATIONS), message(question, *views))
    assert len(body["blocks"]) <= MAX_BLOCKS
    too_many = {"text": "", "blocks": [{"type": "divider"}] * MAX_BLOCKS}
    with pytest.raises(ValueError, match="more blocks"):
        with_question(too_many, message(question, *views))


# The stock question (design section 4, "The stock question").


def stock_item(pk: int, quantity: str = "5", **fields: Any) -> resolve.StockItem:
    return resolve.StockItem(
        pk, Decimal(quantity), 10, Decimal(0), batch=fields.get("batch"), supplier_part=fields.get("supplier_part")
    )


def stock_view(pick: resolve.SourcePick, movement_type: MovementType = MovementType.TAKE_TO_VEHICLE) -> MovementView:
    movement = CandidateMovement(movement_type, (LineItem("Disc 40", Decimal(2)),), "main stock", "truck 1")
    return MovementView(movement, (EXACT,), EXACT, EXACT, (), (pick,))


def test_stock_choices_are_items_and_none_or_again_and_drop_it() -> None:
    choose = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
    assert choices(choose) == (Choice("record", 41), Choice("record", 57), Choice("none"))
    split = Question(Ask.CONFIRM_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
    assert choices(split) == (Choice("yes"), Choice("no"))
    again = Question(Ask.READ_STOCK_AGAIN, ReasonCode.NO_TARGET_STOCK_ITEM, 0, 0)
    assert choices(again) == (Choice("again"), Choice("no"))
    assert choice_from_json(json.loads(json.dumps(choice_json(Choice("again"))))) == Choice("again")
    with pytest.raises(ValueError, match="a choice carries"):
        choice_from_json({"kind": "again", "record": 41, "movement_type": None})


def test_a_stock_item_is_labelled_by_its_id_what_is_on_hand_and_its_batch_or_supplier_part() -> None:
    offer = (stock_item(41, "5", batch="B-1"), stock_item(57, "2.5", supplier_part=9), stock_item(60, batch="x" * 90))
    pick = resolve.SourcePick((), (), offer=offer)
    question = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57, 60), detail="line 1: x")
    body = question_message(question, [stock_view(pick)], PARTS, LOCATIONS, ids(question))
    labels = [b["text"]["text"] for b in buttons(body)]
    assert labels[:2] == ["#41 · 5 left · batch B-1", "#57 · 2.5 left · supplier part #9"]
    assert len(labels[2]) == MAX_LABEL
    assert labels[2].startswith("#60 · 5 left · batch x")
    assert labels[3] == "None of these"
    assert "Which stock item is “Disc 40” taken from (x)?" in text_of(body)
    target = question_message(question, [stock_view(pick, MovementType.COUNT)], PARTS, LOCATIONS, ids(question))
    assert "Which stock item of “Disc 40” is it (x)?" in text_of(target)


def test_a_stock_question_about_items_the_pick_does_not_offer_is_not_shown() -> None:
    pick = resolve.SourcePick((), (), offer=(stock_item(41),))
    choose = Question(Ask.CHOOSE_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
    with pytest.raises(ValueError, match="not offered"):
        question_message(choose, [stock_view(pick)], PARTS, LOCATIONS, ids(choose))
    split = Question(Ask.CONFIRM_STOCK, ReasonCode.STOCK_CHOICE, 0, 0, None, (41, 57))
    with pytest.raises(ValueError, match="not the line's"):
        question_message(split, [stock_view(pick)], PARTS, LOCATIONS, ids(split))


def test_read_the_stock_again_says_what_is_wrong_and_where_it_is_fixed() -> None:
    question = Question(
        Ask.READ_STOCK_AGAIN, ReasonCode.QUANTITY_ABOVE_STOCK, 0, 0, detail=f"line 1: {resolve.DETAIL_TOO_FEW}"
    )
    body = question_message(question, [stock_view(resolve.SourcePick(()))], PARTS, LOCATIONS, ids(question))
    assert [b["text"]["text"] for b in buttons(body)] == ["Read the stock again", "Drop it"]
    text = text_of(body)
    assert f"({resolve.DETAIL_TOO_FEW})" in text
    assert "Fix it in InvenTree or edit the post" in text
