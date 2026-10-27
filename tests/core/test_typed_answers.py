# SPDX-License-Identifier: MIT
"""Answers by thread reply (core/typed_answers.py): a message answers a question only when it reads cleanly."""

from __future__ import annotations

from decimal import Decimal

import pytest

from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType, ReasonCode
from stockthread.core.questions import MAX_CHOICES, Ask, Question
from stockthread.core.typed_answers import (
    TypedAnswer,
    read_name,
    read_quantity,
    read_serials,
    read_typed_answer,
    replay_typed,
    takes_typed_answer,
)

CATALOG = Catalog(
    parts=(
        PartRecord(1, "AP-NV-D40", "Norvane Disc 40 Access Point", "ap wifi"),
        PartRecord(2, "AP-NV-D60", "Norvane Disc 60 Access Point", "ap wifi"),
        PartRecord(3, "U7-Lite", "U7 Lite Access Point"),
    ),
    locations=(
        LocationRecord(11, "Vehicles/Truck 1", "Sam's truck"),
        LocationRecord(12, "Vehicles/Truck 2", "Mike's truck"),
        LocationRecord(13, "Warehouse/Main Stock"),
        LocationRecord(14, "Warehouse/B-12"),
    ),
    part_aliases=(Alias("d40", 1),),
    vehicles={"U0SAM": 11},
)


def movement(*lines: LineItem, kind: MovementType = MovementType.TAKE_TO_VEHICLE) -> CandidateMovement:
    return CandidateMovement(kind, lines, "main stock", "my truck")


def question(ask: Ask, line: int | None = 0, side: str | None = None) -> Question:
    return Question(ask, ReasonCode.QUANTITY_MISSING, 0, line, side)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", "3"),
        (" three ", "3"),
        ("3 pcs", "3"),
        ("3 each", "3"),
        ("Twelve", "12"),
        ("1,200", "1200"),
        ("2.5", "2.5"),
    ],
)
def test_one_number_reads_as_a_quantity(text: str, expected: str) -> None:
    assert read_quantity(text, None) == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    ["", "2 boxes", "a case of 12", "3 or 4", "a few", "3, and 2 to truck 2", "3 pcs please", "about 3", "1.000",
     "0", "-2", "3.", "twenty five", "3 ft", "ok"],
)  # fmt: skip
def test_anything_but_one_number_is_not_a_quantity(text: str) -> None:
    assert read_quantity(text, None) is None


def test_the_line_s_own_unit_may_follow_unless_it_is_a_container() -> None:
    assert read_quantity("40 ft", "ft") == Decimal(40)
    assert read_quantity("40 feet", "ft") == Decimal(40)
    assert read_quantity("12 m", "ft") is None  # another unit: never converted
    assert read_quantity("2 box", "box") is None  # v1.0 never converts packs
    assert read_quantity("2 boxes", "boxes") is None
    assert read_quantity("24 pcs", "box") == Decimal(24)  # the answer to a pack-unit question


def test_zero_is_a_quantity_only_for_a_count() -> None:
    assert read_quantity("0", None) is None
    assert read_quantity("0", None, zero=True) == Decimal(0)
    count = movement(LineItem("disc 40", None), kind=MovementType.COUNT)
    assert read_typed_answer("0", question(Ask.QUANTITY), count, CATALOG, "U0SAM") == TypedAnswer(
        Ask.QUANTITY, quantity=Decimal(0)
    )


def test_a_name_reads_cleanly_when_the_matcher_finds_one_record_or_a_short_list() -> None:
    assert read_name(Match(MatchStatus.MATCHED, MatchRule.FUZZY, 1)) is not None  # confirmed by the next question
    short = Match(MatchStatus.AMBIGUOUS, candidates=tuple(range(1, MAX_CHOICES + 1)))
    assert read_name(short) == short
    assert read_name(Match(MatchStatus.AMBIGUOUS, candidates=tuple(range(1, MAX_CHOICES + 2)))) is None
    assert read_name(Match(MatchStatus.UNMATCHED)) is None


def test_a_typed_part_or_place_is_matched_against_the_catalog() -> None:
    post = movement(LineItem("ubiquity psu", Decimal(2)))
    part = read_typed_answer("AP-NV-D40", question(Ask.NAME_PART), post, CATALOG, "U0SAM")
    assert part == TypedAnswer(Ask.NAME_PART, match=Match(MatchStatus.MATCHED, MatchRule.EXACT, 1))
    assert read_typed_answer("ok thanks", question(Ask.NAME_PART), post, CATALOG, "U0SAM") is None
    assert read_typed_answer("  ", question(Ask.NAME_PART), post, CATALOG, "U0SAM") is None
    place = read_typed_answer("my truck", question(Ask.NAME_LOCATION, None, "destination"), post, CATALOG, "U0SAM")
    assert place == TypedAnswer(Ask.NAME_LOCATION, match=Match(MatchStatus.MATCHED, MatchRule.ALIAS, 11))


def test_only_a_question_that_names_one_line_or_one_side_takes_a_typed_answer() -> None:
    assert takes_typed_answer(question(Ask.QUANTITY))
    assert not takes_typed_answer(question(Ask.QUANTITY, None))  # "How many of each item?"
    assert not takes_typed_answer(question(Ask.NAME_PART, None))  # "Which item was moved?" on a photo-only post
    assert takes_typed_answer(question(Ask.NAME_LOCATION, None, "source"))
    assert not takes_typed_answer(question(Ask.NAME_LOCATION, None, None))
    for ask in (Ask.CONFIRM, Ask.CHOOSE_PART, Ask.CONFIRM_LOCATION, Ask.MOVEMENT_TYPE):
        assert not takes_typed_answer(question(ask))  # buttons answer these
    post = movement(LineItem("disc 40", None))
    assert read_typed_answer("3", question(Ask.QUANTITY, None), post, CATALOG, "U0SAM") is None
    assert read_typed_answer("3", question(Ask.QUANTITY, 1), post, CATALOG, "U0SAM") is None  # no such line


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SR5-000456 SR5-000457", ("SR5-000456", "SR5-000457")),
        ("SR5-000456,\nSR5-000457", ("SR5-000456", "SR5-000457")),
        ("AABBCCDDEEFF 00:1a:2b:3c:4d:5e", ("AABBCCDDEEFF", "00:1a:2b:3c:4d:5e")),
        ("1234 a/b.5", ("1234", "a/b.5")),
    ],
)
def test_serial_shaped_tokens_as_many_as_the_quantity_read_as_serials(text: str, expected: tuple[str, ...]) -> None:
    assert read_serials(text, LineItem("disc 40", Decimal(2)), CATALOG, "U0SAM") == expected


@pytest.mark.parametrize(
    "text",
    [
        "ok thanks",  # no digit
        "done done",
        "3 4",  # too short
        "SR5-000456",  # one, for two
        "SR5-000456 SR5-000457 SR5-000458",
        "SR5-000456 sr5-000456",  # twice
        "SR5-000456 and SR5-000457",
        "SR5-000456 U7-Lite",  # a part name
        "SR5-000456 b-12",  # a location name
        "SR5-000456 AP-NV-D40",
        "SR5-000456 " + "A1" * 21,  # 42 characters
    ],
)
def test_anything_else_is_not_serials(text: str) -> None:
    assert read_serials(text, LineItem("disc 40", Decimal(2)), CATALOG, "U0SAM") is None


def test_serials_need_a_whole_quantity() -> None:
    assert read_serials("SR5-000456", LineItem("disc 40", None), CATALOG, "U0SAM") is None
    assert read_serials("SR5-000456", LineItem("cable", Decimal("1.5")), CATALOG, "U0SAM") is None
    post = movement(LineItem("disc 40", Decimal(1)))
    assert read_typed_answer("SR5-000456", question(Ask.SERIALS), post, CATALOG, "U0SAM") == TypedAnswer(
        Ask.SERIALS, serials=("SR5-000456",)
    )


def test_a_typed_name_is_matched_again_on_replay_and_counts_even_when_it_finds_nothing_now() -> None:
    reading = movement(LineItem("ubiquity psu", Decimal(1)))
    found = replay_typed("U7 Lite Access Point", question(Ask.NAME_PART), reading, CATALOG, "U0SAM")
    assert found is not None
    assert found.match is not None
    assert found.match.record == 3
    gone = Catalog(parts=(), locations=CATALOG.locations)
    found = replay_typed("U7 Lite Access Point", question(Ask.NAME_PART), reading, gone, "U0SAM")
    assert found == TypedAnswer(Ask.NAME_PART, match=Match(MatchStatus.UNMATCHED))  # the line is unmatched now
    place = replay_typed("B-12", question(Ask.NAME_LOCATION, None, "source"), reading, CATALOG, "U0SAM")
    assert place is not None
    assert place.match is not None
    assert place.match.record == 14


def test_a_typed_quantity_or_serials_are_read_as_they_were_judged() -> None:
    reading = movement(LineItem("U7 Lite", None))
    assert replay_typed("3", question(Ask.QUANTITY), reading, CATALOG, "U0SAM") == TypedAnswer(
        Ask.QUANTITY, quantity=Decimal(3)
    )
    serials = replay_typed("SN-1A SN-2B", question(Ask.SERIALS), reading, CATALOG, "U0SAM")
    assert serials == TypedAnswer(Ask.SERIALS, serials=("SN-1A", "SN-2B"))
    assert replay_typed("3", question(Ask.QUANTITY, line=2), reading, CATALOG, "U0SAM") is None
    assert replay_typed("3", question(Ask.QUANTITY, line=None), reading, CATALOG, "U0SAM") is None
