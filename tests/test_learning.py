"""What a confirmed plan teaches (`learning.lessons`, ADR 0012): the poster's own words that a person confirmed,
picked or corrected, each once, when the record is the only one they fit; never an exact, normalized or alias
match, words the model wrote, a text whose meaning depends on the poster, a text that fits other records, or a text
the plan decided two ways."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from views import DESTINATION, SOURCE, view

from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord
from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType
from stockthread.learning import Lesson, lessons
from stockthread.slack.replies import MovementView
from stockthread.store import AliasKind

PART, LOCATION = AliasKind.PART, AliasKind.LOCATION
SAM = "U0SAM0001"
CATALOG = Catalog(
    parts=(
        PartRecord(1, "DISC-40", "Norvane Disc 40 Access Point"),
        PartRecord(2, "DISC-40-PRO", "Norvane Disc 40 Pro Access Point"),
        PartRecord(3, "PC-1", "Patch Cable 1 m"),
        PartRecord(4, "DISC-60", "Norvane Disc 60 Access Point"),
    ),
    locations=(
        LocationRecord(SOURCE, "Main stock"),
        LocationRecord(DESTINATION, "Vehicles/Truck 1"),
        LocationRecord(12, "Vehicles/Truck 2"),
    ),
)


def matched(rule: MatchRule, record: int = 3) -> Match:
    return Match(MatchStatus.MATCHED, rule, record)


def take(text: str, match: Match, **fields: Any) -> MovementView:
    return view(lines=((text, "2"),), items=(match,), **fields)


def taught(*views: MovementView, said: str | None = None, catalog: Catalog = CATALOG) -> list[Lesson]:
    """The lessons of `views`, posted as `said` (by default every line's item text, so each is the poster's)."""
    if said is None:
        said = " and ".join(item.item_text or "" for v in views for item in v.movement.line_items)
    return lessons(list(views), catalog, SAM, [f"took {said} from main stock to my truck"])


@pytest.mark.parametrize("rule", [MatchRule.ANSWER, MatchRule.WORDS, MatchRule.FUZZY])
def test_a_confirmed_pick_or_a_whole_word_or_fuzzy_match_teaches_its_text(rule: MatchRule) -> None:
    assert taught(take("patch cable", matched(rule))) == [Lesson(PART, "patch cable", 3, frozenset({3}), 1)]


@pytest.mark.parametrize("rule", [MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS])
def test_a_match_that_needed_no_confirmation_teaches_nothing(rule: MatchRule) -> None:
    assert taught(take("patch cable", matched(rule))) == []


@pytest.mark.parametrize(
    "match",
    [Match(MatchStatus.AMBIGUOUS, MatchRule.WORDS, None, (1, 2)), Match(MatchStatus.UNMATCHED)],
)
def test_no_match_teaches_nothing(match: Match) -> None:
    assert taught(take("patch cable", match)) == []


@pytest.mark.parametrize(
    "text",
    ["my cable", "this cable", "the usual cable", "same cable", "his cable", "spare one", "40", "", "  ", "--"],
)
def test_a_text_that_depends_on_the_poster_or_names_nothing_is_not_learned(text: str) -> None:
    assert taught(take(text, matched(MatchRule.ANSWER))) == []


def test_a_text_the_poster_did_not_write_is_not_learned() -> None:
    # The model's or a photo's reading of the line, confirmed in the thread: not the poster's words.
    assert taught(take("patch cable", matched(MatchRule.ANSWER)), said="two of the blue leads") == []
    assert taught(take("patch cable", matched(MatchRule.ANSWER)), said="PATCH  CABLE x2") != []


@pytest.mark.parametrize(("text", "record"), [("Disc 40", 1), ("norvane 40", 4), ("norvane", 1)])
def test_a_text_that_fits_any_other_record_is_not_learned(text: str, record: int) -> None:
    # "Disc 40" picked among the 40 and the 40 Pro, or "norvane 40" corrected to the Disc 60 the poster took: the
    # pick says what this post moved, not what the words always mean.
    assert taught(take(text, matched(MatchRule.ANSWER, record))) == []
    assert taught(take(text, matched(MatchRule.EXACT, record), named={0: "DISC-60"})) == []


def test_a_name_typed_in_the_thread_teaches_the_posts_own_text_when_it_fits_nothing_else() -> None:
    corrected = take("ubiquity psu", matched(MatchRule.EXACT, 3), named={0: "PC-1"})
    assert taught(corrected) == [Lesson(PART, "ubiquity psu", 3, frozenset(), 1)]


def test_a_name_typed_for_a_line_the_post_named_nothing_for_teaches_nothing_new() -> None:
    # The record's item text is then the typed name (`worker._typed_line`): an exact match of it needs no alias.
    assert taught(take("PC-1", matched(MatchRule.EXACT, 3), named={0: "pc-1"})) == []


def test_places_are_learned_after_parts_and_my_truck_never() -> None:
    movement = CandidateMovement(
        MovementType.TAKE_TO_VEHICLE, (LineItem("patch cable", Decimal(2)),), "the shop", "my truck"
    )
    picked = view(
        movement=movement,
        items=(matched(MatchRule.ANSWER),),
        source=matched(MatchRule.ANSWER, SOURCE),
        destination=matched(MatchRule.ANSWER, DESTINATION),
    )
    assert lessons([picked], CATALOG, SAM, ["took 2 patch cable from the shop to my truck"]) == [
        Lesson(PART, "patch cable", 3, frozenset({3}), 1),
        Lesson(LOCATION, "the shop", SOURCE, frozenset(), 1),
    ]


def test_a_typed_place_teaches_the_posts_place_text_only_when_it_fits_that_place_alone() -> None:
    def to(text: str) -> MovementView:
        movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem("PC-1", Decimal(2)),), "main stock", text)
        return view(
            movement=movement,
            items=(matched(MatchRule.EXACT, 3),),
            source=matched(MatchRule.EXACT, SOURCE),
            destination=matched(MatchRule.EXACT, DESTINATION),
            named={"destination": "Truck 1"},
        )

    said = ["took 2 PC-1 from main stock to sams van and the truck"]
    assert lessons([to("sams van")], CATALOG, SAM, said) == [Lesson(LOCATION, "sams van", DESTINATION, frozenset(), 1)]
    assert lessons([to("the truck")], CATALOG, SAM, said) == []  # Truck 1 or Truck 2


def test_a_text_is_learned_once_from_the_first_movement_that_decided_it() -> None:
    first = take("patch cable", matched(MatchRule.ANSWER, 3))
    second = take("patch cables", matched(MatchRule.ANSWER, 3))
    assert taught(first, second) == [Lesson(PART, "patch cable", 3, frozenset({3}), 1)]


def test_a_text_the_plan_decided_for_two_records_teaches_nothing() -> None:
    corrected = {0: "a name typed in the thread"}
    one = take("ubiquity psu", matched(MatchRule.EXACT, 1), named=corrected)
    other = take("Ubiquity PSU", matched(MatchRule.EXACT, 4), named=corrected)
    cable = take("patch cable", matched(MatchRule.ANSWER, 3))
    assert taught(one, other, cable) == [Lesson(PART, "patch cable", 3, frozenset({3}), 3)]


def test_the_rivals_are_read_with_the_posters_aliases_only() -> None:
    lent = Catalog(parts=CATALOG.parts, locations=(), part_aliases=(Alias("cable", 1, "U0LUIS0005"),))
    [lesson] = taught(take("cable", matched(MatchRule.ANSWER, 3)), catalog=lent)
    assert lesson.rivals == frozenset({3})


@pytest.mark.parametrize("text", ["the truck", "van", "the vans"])
def test_a_place_named_by_a_vehicle_word_alone_is_never_learned_even_with_one_vehicle(text: str) -> None:
    one = Catalog(parts=CATALOG.parts, locations=CATALOG.locations[:2])
    movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem("PC-1", Decimal(2)),), "main stock", text)
    typed = view(
        movement=movement,
        items=(matched(MatchRule.EXACT, 3),),
        source=matched(MatchRule.EXACT, SOURCE),
        destination=matched(MatchRule.ANSWER, DESTINATION),
    )
    assert lessons([typed], one, SAM, [f"took 2 PC-1 from main stock to {text}"]) == []
