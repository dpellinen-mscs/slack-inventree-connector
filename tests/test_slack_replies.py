"""The dry-run reply: what it shows, and that post text can never become Slack markup."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

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
from stockthread.slack.replies import MAX_CANDIDATES, MAX_SECTION, REASONS, MovementView, dry_run_reply, escape

PARTS = {1: "Norvane Disc 40 Access Point", 2: "Norvane Disc 60 Access Point", 3: "UPS 600 VA"}
LOCATIONS = {10: "Warehouse/Main Stock", 11: "Vehicles/Truck 1"}


def matched(pk: int) -> Match:
    return Match(MatchStatus.MATCHED, MatchRule.EXACT, pk)


def view(item: str = "Disc 40", reasons: tuple[ReviewReason, ...] = (), **fields: Any) -> MovementView:
    movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem(item, Decimal(2)),), "main stock", "my truck")
    values: dict[str, Any] = {
        "movement": movement,
        "items": (matched(1),),
        "source": matched(10),
        "destination": matched(11),
        "reasons": reasons,
    }
    values.update(fields)
    return MovementView(**values)


def text_of(body: dict[str, Any]) -> str:
    return "\n".join(block["text"]["text"] for block in body["blocks"])


def test_a_clean_movement() -> None:
    body = dry_run_reply([view()], PARTS, LOCATIONS)
    assert body["text"] == "Dry run: 1 movement, nothing changed."
    shown = text_of(body)
    assert "Nothing was changed in inventory" in shown
    assert "*Take to a vehicle*" in shown
    assert "2 \N{MULTIPLICATION SIGN} *Norvane Disc 40 Access Point* (“Disc 40”)" in shown
    assert "From *Warehouse/Main Stock* (“main stock”) to *Vehicles/Truck 1* (“my truck”)" in shown
    assert "Nothing needs a human" in shown


def test_every_reason_is_listed_once_in_words() -> None:
    reasons = (
        ReviewReason(ReasonCode.IMPLIED_SOURCE, ReasonSource.RULE),
        ReviewReason(ReasonCode.IMPLIED_SOURCE, ReasonSource.MODEL),
        ReviewReason(ReasonCode.QUANTITY_ABOVE_LIMIT, ReasonSource.RULE, "line 1"),
    )
    shown = text_of(dry_run_reply([view(reasons=reasons)], PARTS, LOCATIONS))
    assert shown.count(REASONS[ReasonCode.IMPLIED_SOURCE]) == 1
    assert REASONS[ReasonCode.QUANTITY_ABOVE_LIMIT] in shown
    assert "Nothing needs a human" not in shown


def test_every_reason_code_has_words() -> None:
    assert set(REASONS) == set(ReasonCode)


def test_ambiguous_and_unmatched_lines() -> None:
    many = Match(MatchStatus.AMBIGUOUS, MatchRule.FUZZY, candidates=(1, 2, 3, 4, 5, 6, 7))
    shown = text_of(dry_run_reply([view("aps", items=(many,), source=Match(MatchStatus.UNMATCHED))], PARTS, LOCATIONS))
    assert (
        "“aps”: 7 parts match (Norvane Disc 40 Access Point, Norvane Disc 60 Access Point, UPS 600 VA, #4, #5" in shown
    )
    assert f"and {7 - MAX_CANDIDATES} more" in shown
    assert "“main stock”: no location found" in shown


@pytest.mark.parametrize(
    "hostile",
    ["<!channel> 2 Disc 40", "<https://example.invalid|Disc 40>", "<@U0DANA0004>", "Disc &lt;40", "Disc\n40\n<!here>"],
)
def test_post_text_never_becomes_slack_markup(hostile: str) -> None:
    body = dry_run_reply([view(hostile)], PARTS, LOCATIONS)
    shown = text_of(body)
    assert "<" not in shown
    assert ">" not in shown
    assert escape(hostile) in shown


def test_catalog_names_are_escaped_too() -> None:
    shown = text_of(dry_run_reply([view()], {1: "Cable <!everyone>"}, LOCATIONS))
    assert "&lt;!everyone&gt;" in shown


def test_a_long_reply_stays_inside_slack_limits() -> None:
    items = tuple(LineItem(f"item {n} " + "x" * 80, Decimal(1)) for n in range(60))
    movement = CandidateMovement(MovementType.RECEIVE, items, None, "receiving")
    long = MovementView(movement, tuple(Match(MatchStatus.UNMATCHED) for _ in items), None, None, ())
    body = dry_run_reply([long], PARTS, LOCATIONS)
    assert all(len(block["text"]["text"]) <= MAX_SECTION for block in body["blocks"])
    assert text_of(body).count("…") == 1


def test_quantities_are_shown_as_written() -> None:
    movement = CandidateMovement(
        MovementType.CONSUME, (LineItem("blue screws", Decimal("0.5"), "box"), LineItem("cable", None)), None, "site"
    )
    shown = text_of(dry_run_reply([MovementView(movement, (matched(3), matched(3)), None, None, ())], PARTS, LOCATIONS))
    assert "0.5 box \N{MULTIPLICATION SIGN}" in shown
    assert "? \N{MULTIPLICATION SIGN}" in shown
    assert "From (not stated)" in shown


def test_several_movements_are_numbered() -> None:
    body = dry_run_reply([view(), view()], PARTS, LOCATIONS)
    assert body["text"] == "Dry run: 2 movements, nothing changed."
    assert "*1. Take to a vehicle*" in text_of(body)


def test_a_reply_needs_a_movement_and_one_match_per_line() -> None:
    with pytest.raises(ValueError, match="at least one"):
        dry_run_reply([], PARTS, LOCATIONS)
    with pytest.raises(ValueError, match="one match per line"):
        view(items=())
