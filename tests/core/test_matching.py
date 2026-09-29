"""Matching rules and safety properties that the corpus does not pin down (design section 10)."""

from __future__ import annotations

from stockthread.core.matching import (
    Alias,
    Catalog,
    LocationRecord,
    PartRecord,
    match_location,
    match_part,
    stale_aliases,
    words,
)
from stockthread.core.model import Match, MatchRule, MatchStatus

PARTS = (
    PartRecord(1, "AP-NV-D40", "Norvane Disc 40 Access Point", "ap wifi"),
    PartRecord(2, "AP-NV-D60", "Norvane Disc 60 Access Point", "ap wifi"),
    PartRecord(3, "UPS-600", "UPS 600 VA", "battery"),
    PartRecord(4, "OLD-D40", "Norvane Disc 40 Kit", "ap", active=False),
    PartRecord(5, "TPL-AP", "Access Point Template", "ap", template=True),
    PartRecord(6, "VIRT-AP", "Access Point Service", "ap", virtual=True),
)
LOCATIONS = (
    LocationRecord(10, "Vehicles", "Company vehicles", structural=True),
    LocationRecord(11, "Vehicles/Truck 1", "Sam's truck"),
    LocationRecord(12, "Vehicles/Truck 2", "Mike's truck"),
    LocationRecord(13, "Warehouse/Main Stock", "Shelving"),
)
CATALOG = Catalog(
    parts=PARTS,
    locations=LOCATIONS,
    part_aliases=(Alias("d40", 1), Alias("old kit", 4)),
    location_aliases=(Alias("shop", 13), Alias("the rig", 12, user="U0MIKE")),
    vehicles={"U0SAM": 11, "U0MIKE": 12, "U0GONE": 10},
)


def matched(record: int, rule: MatchRule) -> Match:
    return Match(MatchStatus.MATCHED, rule=rule, record=record)


def test_words_fold_case_punctuation_and_possessives() -> None:
    assert words("Sam\N{RIGHT SINGLE QUOTATION MARK}s  J-Arm, SFP+!") == ["sam", "j", "arm", "sfp"]


def test_rules_run_in_order() -> None:
    assert match_part("AP-NV-D40", CATALOG) == matched(1, MatchRule.EXACT)
    assert match_part("norvane disc 40 access points", CATALOG) == matched(1, MatchRule.NORMALIZED)
    assert match_part("D40", CATALOG) == matched(1, MatchRule.ALIAS)
    assert match_part("D40s", CATALOG) == matched(1, MatchRule.WORDS)  # a plural with digits never authorizes
    assert match_part("disc 40", CATALOG) == matched(1, MatchRule.WORDS)
    assert match_part("norvain disc 40", CATALOG) == matched(1, MatchRule.FUZZY)


def test_several_hits_are_ambiguous_with_every_candidate() -> None:
    assert match_part("access points", CATALOG) == Match(MatchStatus.AMBIGUOUS, MatchRule.WORDS, candidates=(1, 2))


def test_numbers_are_never_fuzzy() -> None:
    assert match_part("disc 41", CATALOG).status is MatchStatus.UNMATCHED
    assert match_location("truck 7", CATALOG, "U0SAM").status is MatchStatus.UNMATCHED


def test_typo_tolerance_only_for_words_of_four_letters_or_more() -> None:
    assert match_part("norvain disc 40", CATALOG) == matched(1, MatchRule.FUZZY)
    assert match_part("norvane disk 40", CATALOG).status is MatchStatus.UNMATCHED  # "disk" is too far from "disc"
    assert match_part("ab", CATALOG).status is MatchStatus.UNMATCHED


def test_capitalized_words_have_no_plural() -> None:
    assert match_part("UPS", CATALOG) == matched(3, MatchRule.WORDS)


def test_inactive_template_and_virtual_parts_never_match() -> None:
    assert match_part("OLD-D40", CATALOG).status is MatchStatus.UNMATCHED
    assert match_part("old kit", CATALOG).status is MatchStatus.UNMATCHED  # alias to an inactive part
    assert match_part("Access Point Template", CATALOG).status is MatchStatus.UNMATCHED
    assert match_part("access point service", CATALOG).status is MatchStatus.UNMATCHED


def test_nothing_fits_is_unmatched_not_forced() -> None:
    assert match_part("ladder racks", CATALOG).status is MatchStatus.UNMATCHED
    assert match_part("   ", CATALOG).status is MatchStatus.UNMATCHED


def test_my_vehicle_is_the_posters_vehicle() -> None:
    assert match_location("my truck", CATALOG, "U0SAM") == matched(11, MatchRule.ALIAS)
    assert match_location("My Truck", CATALOG, "U0MIKE") == matched(12, MatchRule.ALIAS)
    assert match_location("my truck", CATALOG, "U0NONE").status is MatchStatus.UNMATCHED
    assert match_location("my truck", CATALOG, "U0GONE").status is MatchStatus.UNMATCHED  # structural


def test_possessive_names_resolve_through_the_description() -> None:
    assert match_location("Mikes truck", CATALOG, "U0SAM") == matched(12, MatchRule.WORDS)
    assert match_location("Sam's truck", CATALOG, "U0MIKE") == matched(11, MatchRule.WORDS)


def test_a_person_is_not_a_location() -> None:
    assert match_location("Luis", CATALOG, "U0SAM").status is MatchStatus.UNMATCHED


def test_user_aliases_apply_only_to_their_user() -> None:
    assert match_location("the rig", CATALOG, "U0MIKE") == matched(12, MatchRule.ALIAS)
    assert match_location("the rig", CATALOG, "U0SAM").status is MatchStatus.UNMATCHED
    assert match_location("shop", CATALOG, "U0SAM") == matched(13, MatchRule.ALIAS)


def test_structural_locations_never_match() -> None:
    assert match_location("Vehicles", CATALOG, "U0SAM") == Match(
        MatchStatus.AMBIGUOUS, MatchRule.WORDS, candidates=(11, 12)
    )


def test_a_typo_is_tried_only_when_no_record_has_every_word() -> None:
    # "disc 60" names part 2 without a typo, so the typo allowance never widens it to part 1.
    assert match_part("disc 60", CATALOG) == matched(2, MatchRule.WORDS)


def _catalog(*names: str, ipn: str = "") -> Catalog:
    return Catalog(
        parts=tuple(PartRecord(pk, ipn, name) for pk, name in enumerate(names, start=1)), locations=LOCATIONS
    )


def test_text_without_words_never_matches_a_blank_ipn() -> None:
    catalog = _catalog("Disc 40")
    for text in ("-", "???", "'s", "\N{DELIVERY TRUCK}", ""):
        assert match_part(text, catalog).status is MatchStatus.UNMATCHED, text
    assert match_location("---", CATALOG, "U0SAM").status is MatchStatus.UNMATCHED


def test_a_letter_after_digits_is_never_folded_by_an_authorizing_rule() -> None:
    catalog = _catalog("Disc 40")
    for text in ("Disc 40S", "disc 40s", "disc 40es"):
        result = match_part(text, catalog)
        assert not result.can_authorize_a_write, text


def test_plural_collisions_are_ambiguous() -> None:
    assert match_part("wire tapes", _catalog("Wire Tap", "Wire Tape")).status is MatchStatus.AMBIGUOUS
    assert match_part("ups battery", _catalog("UP Battery", "UPS Battery")).status is MatchStatus.AMBIGUOUS
    assert match_part("wire tape", _catalog("Wire Tap", "Wire Tape")) == matched(2, MatchRule.NORMALIZED)


def test_letters_outside_ascii_are_words() -> None:
    catalog = _catalog("10\N{MICRO SIGN}F Cap", "10 Pipe")
    assert match_part("10 F Cap", catalog).status is MatchStatus.UNMATCHED
    assert match_part("\N{LATIN CAPITAL LETTER O WITH STROKE}10 pipe", catalog).status is MatchStatus.UNMATCHED
    assert match_part("10\N{GREEK SMALL LETTER MU}f cap", catalog) == matched(1, MatchRule.NORMALIZED)


def test_part_aliases_of_another_user_do_not_apply() -> None:
    catalog = Catalog(parts=PARTS, locations=LOCATIONS, part_aliases=(Alias("my spare", 2, user="U0SAM"),))
    assert match_part("my spare", catalog, poster="U0SAM") == matched(2, MatchRule.ALIAS)
    assert match_part("my spare", catalog, poster="U0MIKE").status is MatchStatus.UNMATCHED
    assert match_part("my spare", catalog).status is MatchStatus.UNMATCHED


def test_aliases_to_records_that_cannot_match_are_reported() -> None:
    assert stale_aliases(CATALOG) == [Alias("old kit", 4)]
