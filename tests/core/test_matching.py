# SPDX-License-Identifier: MIT
"""Matching rules and safety properties that the corpus does not pin down (design section 10)."""

from __future__ import annotations

import time
from dataclasses import replace

from stockthread.core.matching import (
    Alias,
    Catalog,
    LocationRecord,
    PartRecord,
    location_rivals,
    locations_named,
    match_location,
    match_part,
    part_rivals,
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
    assert words("Sam\N{RIGHT SINGLE QUOTATION MARK}s  J-Arm, SFP+!") == ["sam", "j", "arm", "sfp+"]


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


def test_a_plural_form_that_names_no_record_does_not_block_a_match() -> None:
    # "UPS" also folds to "up", which names nothing, so rule 4 still finds part 3.
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
    assert match_location("my vehicle", CATALOG, "U0SAM") == matched(11, MatchRule.ALIAS)  # its path says "Vehicles"


def test_my_vehicle_must_name_the_kind_of_vehicle_assigned() -> None:
    # Sam is assigned a truck. "My trailer" is not Sam's truck, and never authorizes a write to it.
    trailer = LocationRecord(14, "Vehicles/Trailer", "Shared equipment trailer")
    catalog = Catalog(parts=PARTS, locations=(*LOCATIONS, trailer), vehicles={"U0SAM": 11})
    for text in ("my trailer", "my van", "my car"):
        result = match_location(text, catalog, "U0SAM")
        assert result.record != 11, text
        assert not result.can_authorize_a_write, text
    assert match_location("my truck", catalog, "U0SAM") == matched(11, MatchRule.ALIAS)


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
    assert match_part("ups battery", _catalog("UP Battery", "UPS Battery")).status is MatchStatus.AMBIGUOUS
    assert match_part("wire tape", _catalog("Wire Tap", "Wire Tape")) == matched(2, MatchRule.NORMALIZED)
    assert match_part("wire tapes", _catalog("Wire Tap", "Wire Tape")) == matched(2, MatchRule.NORMALIZED)


def test_es_is_folded_only_after_a_hissing_sound_by_an_authorizing_rule() -> None:
    # "tapes" is not "tap" and "pipes" is not "pip": with only the wrong record in the catalog, no write.
    for text, name in (("wire tapes", "Wire Tap"), ("pipes", "Pip"), ("tubes", "Tub"), ("cones", "Con")):
        assert not match_part(text, _catalog(name)).can_authorize_a_write, text
    for text, name in (("boxes", "Box"), ("switches", "Switch"), ("brushes", "Brush"), ("glasses", "Glass")):
        assert match_part(text, _catalog(name)) == matched(1, MatchRule.NORMALIZED), text


def test_a_trailing_plus_names_another_model() -> None:
    # SFP and SFP+, PoE and PoE+ are different products.
    assert not match_part("poe injector", _catalog("PoE+ Injector")).can_authorize_a_write
    assert not match_part("poe+ injector", _catalog("PoE Injector")).can_authorize_a_write
    assert match_part("sfp module", _catalog("SFP+ Module", "SFP Module")) == matched(2, MatchRule.NORMALIZED)
    assert match_part("SFP+ modules", _catalog("SFP+ Module", "SFP Module")) == matched(1, MatchRule.NORMALIZED)
    assert match_part("poe injector", _catalog("PoE+ Injector")) == matched(1, MatchRule.WORDS)  # asked, not assumed


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


# --- learned aliases are asked again when the catalog grows a rival (ADR 0012) ---

PRO = PartRecord(7, "AP-NV-D40P", "Norvane Disc 40 Pro", "ap wifi")


def test_a_learned_alias_is_ambiguous_once_a_new_record_fits_its_text() -> None:
    learned = Alias("disc 40", 1, rivals=frozenset({1}))
    before = Catalog(parts=PARTS, locations=LOCATIONS, part_aliases=(learned,))
    assert match_part("disc 40", before) == matched(1, MatchRule.ALIAS)
    grown = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=(learned,))
    assert match_part("Disc 40", grown) == Match(MatchStatus.AMBIGUOUS, rule=MatchRule.ALIAS, candidates=(1, 7))


def test_a_learned_alias_confirmed_against_a_record_stays_matched() -> None:
    learned = Alias("disc 40", 1, rivals=frozenset({1, 7}))
    grown = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=(learned,))
    assert match_part("disc 40", grown) == matched(1, MatchRule.ALIAS)


def test_a_rival_found_only_by_the_fuzzy_rule_counts() -> None:
    learned = Alias("norvane disc", 1, rivals=frozenset({1, 2}))
    typo = PartRecord(8, "AP-NR-DSC", "Norvana Disc Mount", "")
    grown = Catalog(parts=(*PARTS, typo), locations=LOCATIONS, part_aliases=(learned,))
    assert match_part("norvane disc", grown) == Match(MatchStatus.AMBIGUOUS, rule=MatchRule.ALIAS, candidates=(1, 8))


def test_an_alias_an_admin_typed_is_not_rechecked() -> None:
    typed = Alias("disc 40", 1)
    grown = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=(typed,))
    assert match_part("disc 40", grown) == matched(1, MatchRule.ALIAS)


def test_a_learned_location_alias_is_rechecked_too() -> None:
    learned = Alias("truck", 11, rivals=frozenset({11, 12}))
    grown = Catalog(
        parts=PARTS, locations=(*LOCATIONS, LocationRecord(14, "Vehicles/Truck 3", "")), location_aliases=(learned,)
    )
    assert match_location("truck", grown, poster="U0SAM") == Match(
        MatchStatus.AMBIGUOUS, rule=MatchRule.ALIAS, candidates=(11, 14)
    )


def test_an_admin_alias_among_the_hits_vouches_for_the_text() -> None:
    aliases = (Alias("disc 40", 1), Alias("disc 40", 1, user="U0SAM", rivals=frozenset({1})))
    grown = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=aliases)
    assert match_part("disc 40", grown, poster="U0SAM") == matched(1, MatchRule.ALIAS)


def test_only_the_poster_s_learned_aliases_are_rechecked() -> None:
    mine = Alias("disc 40", 1, user="U0SAM", rivals=frozenset({1}))
    grown = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=(mine,))
    assert match_part("disc 40", grown, poster="U0SAM").status is MatchStatus.AMBIGUOUS
    assert match_part("disc 40", grown, poster="U0MIKE").rule is MatchRule.WORDS  # not Sam's alias: ambiguous words
    assert match_part("disc 40", grown, poster="U0MIKE").status is MatchStatus.AMBIGUOUS


def test_rivals_are_what_the_whole_word_and_fuzzy_rules_fit_now() -> None:
    typo = PartRecord(8, "AP-NR-DSC", "Norvana Disc Mount", "")
    catalog = Catalog(parts=(*PARTS, typo), locations=LOCATIONS)
    assert part_rivals("norvane disc", catalog) == {1, 2, 8}  # 8 by the fuzzy rule; 4 is inactive
    learned = Alias("norvane disc", 1, rivals=part_rivals("norvane disc", catalog))
    assert match_part("norvane disc", replace(catalog, part_aliases=(learned,))) == matched(1, MatchRule.ALIAS)
    assert location_rivals("truck", CATALOG, "U0SAM") == {11, 12}  # 10 is structural


def test_a_record_that_could_not_match_when_confirmed_is_a_new_rival_once_it_can() -> None:
    inactive = PartRecord(7, "AP-NV-D40P", "Norvane Disc 40 Pro", "", active=False)
    then = Catalog(parts=(*PARTS, inactive), locations=LOCATIONS)
    learned = Alias("disc 40", 1, rivals=part_rivals("disc 40", then))
    now = Catalog(parts=(*PARTS, PRO), locations=LOCATIONS, part_aliases=(learned,))
    assert match_part("disc 40", now).candidates == (1, 7)


def test_a_location_is_named_by_its_name_or_an_alias_word_for_word() -> None:
    assert locations_named("pulled 2 D40s truck 2 to my van", CATALOG, "U0SAM") == {12}
    assert locations_named("van 3 gave me 2, main stock got the rest", CATALOG, "U0SAM") == {13}
    assert locations_named("grabbed 2 at the shop", CATALOG, "U0SAM") == {13}  # an alias anyone may use
    assert locations_named("2 off the rig", CATALOG, "U0MIKE") == {12}  # Mike's own alias
    assert locations_named("2 off the rig", CATALOG, "U0SAM") == frozenset()  # not Sam's
    assert locations_named("Truck-2 and TRUCK 1", CATALOG, "U0SAM") == {11, 12}


def test_a_location_is_named_only_by_its_whole_name() -> None:
    assert locations_named("2 D40s in the truck", CATALOG, "U0SAM") == frozenset()  # "truck" names no one truck
    assert locations_named("truck 12 got 2", CATALOG, "U0SAM") == frozenset()
    assert locations_named("2 from the vehicles", CATALOG, "U0SAM") == frozenset()  # structural: holds no stock
    assert locations_named("workshop vac", CATALOG, "U0SAM") == frozenset()
    assert locations_named("Sam's truck: 2 D40s. Truck\n2 is full", CATALOG, "U0SAM") == frozenset()


def test_my_vehicle_names_the_poster_s_vehicle() -> None:
    assert locations_named("2 D40s my truck to the Smith job", CATALOG, "U0SAM") == {11}
    assert locations_named("2 D40s my van to the Smith job", CATALOG, "U0SAM") == frozenset()  # Sam has a truck
    assert locations_named("2 D40s my truck to the Smith job", CATALOG, "U0NOBODY") == frozenset()


def test_a_name_with_no_word_of_three_letters_and_no_letters_with_digits_names_nothing() -> None:
    shelves = (LocationRecord(20, "Shelves/B"), LocationRecord(21, "Shelves/B 4"), LocationRecord(22, "Bin 4"))
    catalog = Catalog(parts=PARTS, locations=shelves)
    assert locations_named("plan b 4 to bin 4", catalog, "U0SAM") == {22}


def test_a_location_is_named_by_its_full_path() -> None:
    shelves = (LocationRecord(20, "Shelves/B"), LocationRecord(21, "Shelves/B 4"), LocationRecord(22, "Bin 4"))
    catalog = Catalog(parts=PARTS, locations=shelves)
    assert locations_named("pulled 2 ONTs shelves B to bin 4", catalog, "U0SAM") == {20, 22}
    assert locations_named("pulled 2 ONTs Shelves/B 4 to bin 4", catalog, "U0SAM") == {20, 21, 22}  # B is in B 4
    assert locations_named("2 from the warehouse main stock", CATALOG, "U0SAM") == {13}
    assert locations_named("2 D40s vehicles truck 2", CATALOG, "U0SAM") == {12}


def test_letters_and_digits_may_be_written_apart_or_together() -> None:
    places = (
        LocationRecord(30, "Vehicles/Van3"),
        LocationRecord(31, "Vehicles/Van 4"),
        LocationRecord(32, "Bins/A1"),
        LocationRecord(33, "Bins/R3-S2"),
    )
    catalog = Catalog(parts=PARTS, locations=places)
    assert locations_named("pulled 2 ONTs van 3 to my truck", catalog, "U0SAM") == {30}
    assert locations_named("pulled 2 ONTs van3 to my truck", catalog, "U0SAM") == {30}
    assert locations_named("pulled 2 ONTs van4 to my truck", catalog, "U0SAM") == {31}
    assert locations_named("2 ONTs A1 to van 4", catalog, "U0SAM") == {31, 32}
    assert locations_named("a 1 inch drill bit", catalog, "U0SAM") == frozenset()  # "A1" is not written apart
    assert locations_named("2 ONTs r3-s2 to van 4", catalog, "U0SAM") == {31, 33}
    assert locations_named("to van 4 from r3-s2 a1", catalog, "U0SAM") == {31, 32, 33}  # one-word names at the end


def test_naming_is_fast_on_a_large_catalog_and_a_long_post() -> None:
    many = tuple(LocationRecord(pk, f"Aisle {pk}/Shelf {pk}") for pk in range(1000, 2000))
    aliases = tuple(Alias(f"bay {pk} top", pk) for pk in range(1000, 2000))
    catalog = Catalog(parts=PARTS, locations=many, location_aliases=aliases)
    text = "grabbed 2 ONTs from shelf 1999 and some more " * 500
    started = time.monotonic()
    assert locations_named(text, catalog, "U0SAM") == {1999}
    assert time.monotonic() - started < 1.0
