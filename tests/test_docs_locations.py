# SPDX-License-Identifier: MIT
"""The claims of docs/locations.md, checked against the matcher on the layout it shows."""

from __future__ import annotations

import pytest

from stockthread.catalog import assign_vehicles
from stockthread.core import extract_rules
from stockthread.core.matching import Catalog, LocationRecord, match_location
from stockthread.core.model import MatchStatus, MessageInput

SAM, LUIS, ANA = "U0SAM0001", "U0LUIS0005", "U0ANA0003"
LAYOUT = (
    LocationRecord(1, "Warehouse", structural=True),
    LocationRecord(2, "Warehouse/Main Stock"),
    LocationRecord(3, "Warehouse/Receiving"),
    LocationRecord(4, "Vehicles", structural=True),
    LocationRecord(5, "Vehicles/Truck 1", "Sam's truck"),
    LocationRecord(6, "Vehicles/Van 2", "Luis's van"),
    LocationRecord(7, "Sites", structural=True),
    LocationRecord(8, "Sites/Smith job", "Install on Elm Street"),
)
VEHICLES = {SAM: "Vehicles/Truck 1", LUIS: "Vehicles/Van 2", ANA: "Vehicles/Truck 9"}


def catalog(locations: tuple[LocationRecord, ...] = LAYOUT) -> Catalog:
    return assign_vehicles(Catalog(parts=(), locations=locations), VEHICLES)[0]


def place(text: str) -> str:
    """The place as the extractor hands it to the matcher (articles dropped)."""
    post = f"took 2 Disc 40 from {text} to Smith job"
    [movement] = extract_rules.extract([MessageInput("1790000000.000100", post)]).movements
    assert movement.source_text is not None
    return movement.source_text


@pytest.mark.parametrize(
    ("text", "user", "record"),
    [
        ("Truck 1", SAM, 5),
        ("Vehicles/Truck 1", SAM, 5),
        ("main stock", SAM, 2),
        ("Sam's truck", LUIS, 5),
        ("Elm Street", SAM, 8),
        ("recieving", SAM, 3),
        ("my truck", SAM, 5),
        ("my van", LUIS, 6),
    ],
)
def test_the_examples_that_match(text: str, user: str, record: int) -> None:
    match = match_location(place(text), catalog(), user)
    assert (match.status, match.record) == (MatchStatus.MATCHED, record)


@pytest.mark.parametrize(
    ("text", "user", "candidates"),
    [("the warehouse", SAM, (2, 3)), ("truck", SAM, ()), ("vehicles", SAM, (5, 6))],
)
def test_the_examples_that_are_asked_as_a_choice(text: str, user: str, candidates: tuple[int, ...]) -> None:
    trucks = (*LAYOUT, LocationRecord(9, "Vehicles/Truck 3"))  # "truck" alone, with two trucks
    match = match_location(place(text), catalog(trucks if text == "truck" else LAYOUT), user)
    assert match.status is MatchStatus.AMBIGUOUS
    assert set(candidates) <= set(match.candidates)


@pytest.mark.parametrize(("text", "user"), [("truck 7", SAM), ("my van", SAM), ("my truck", ANA), ("Vehicles", ANA)])
def test_the_examples_that_match_nothing(text: str, user: str) -> None:
    locations = tuple(loc for loc in LAYOUT if loc.pk not in (5, 6)) if text == "Vehicles" else LAYOUT
    assert match_location(place(text), catalog(locations), user).status is MatchStatus.UNMATCHED


def test_a_place_named_warehouse_wins_over_the_shared_word() -> None:
    layout = (LocationRecord(1, "Warehouse"), LocationRecord(2, "Warehouse/Receiving"))
    match = match_location(place("the warehouse"), catalog(layout), SAM)
    assert (match.status, match.record) == (MatchStatus.MATCHED, 1)


def test_digits_are_told_apart() -> None:
    layout = (LocationRecord(1, "Truck 1"), LocationRecord(2, "Truck 11"), LocationRecord(3, "Truck A2"))
    assert match_location("truck 1", catalog(layout), SAM).record == 1
    assert match_location("truck 11", catalog(layout), SAM).record == 2
    assert match_location("truck a", catalog(layout), SAM).status is MatchStatus.UNMATCHED


def test_a_path_that_is_not_found_leaves_its_person_without_a_vehicle() -> None:
    _, missing = assign_vehicles(Catalog(parts=(), locations=LAYOUT), VEHICLES)
    assert missing == (ANA,)
