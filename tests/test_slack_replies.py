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
from stockthread.core.resolve import SourcePick, Take
from stockthread.slack.ingest import DropReason
from stockthread.slack.replies import (
    APPROVERS_ONLY,
    CLICK_FEEDBACK,
    MAX_CANDIDATES,
    MAX_MOVEMENTS,
    MAX_SECTION,
    NO_APPROVERS,
    REASONS,
    MovementView,
    click_feedback,
    dry_run_reply,
    escape,
    question_closed,
    without_question,
)
from stockthread.store import Answered

COULD_APPLY = "With writes on, this could apply."

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


def test_the_stock_a_line_acts_on_is_shown_by_its_inventree_id() -> None:
    one = SourcePick((Take(57, Decimal(2)),))
    split = SourcePick((Take(41, Decimal(1)), Take(57, Decimal("1.5"))))
    assert "*Norvane Disc 40 Access Point* (“Disc 40”) (from stock item #57)" in text_of(
        dry_run_reply([view(stock=(one,))], PARTS, LOCATIONS)
    )
    assert "(from stock items #41: 1, #57: 1.5)" in text_of(dry_run_reply([view(stock=(split,))], PARTS, LOCATIONS))
    received = CandidateMovement(MovementType.RECEIVE, (LineItem("Disc 40", Decimal(2)),), None, "main stock")
    assert "(into stock item #57)" in text_of(dry_run_reply([view(movement=received, stock=(one,))], PARTS, LOCATIONS))
    for unread in ((), (None,), (SourcePick((), (ReviewReason(ReasonCode.STOCK_CHOICE, ReasonSource.RULE),)),)):
        assert "stock item" not in text_of(dry_run_reply([view(stock=unread)], PARTS, LOCATIONS))


def test_a_view_has_one_stock_pick_per_line_or_none() -> None:
    with pytest.raises(ValueError, match="one stock pick per line"):
        view(stock=(None, None))


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
    assert COULD_APPLY not in text_of(body)
    assert REASONS[ReasonCode.PART_UNMATCHED] in text_of(body)


def test_quantities_are_shown_as_written() -> None:
    movement = CandidateMovement(
        MovementType.CONSUME, (LineItem("blue screws", Decimal("0.5"), "box"), LineItem("cable", None)), None, "site"
    )
    shown = text_of(dry_run_reply([MovementView(movement, (matched(3), matched(3)), None, None, ())], PARTS, LOCATIONS))
    assert "0.5 box \N{MULTIPLICATION SIGN}" in shown
    assert "? \N{MULTIPLICATION SIGN}" in shown
    assert "From (not stated)" in shown
    assert f"{REASONS[ReasonCode.QUANTITY_MISSING]} (line 2)" in shown
    assert f"{REASONS[ReasonCode.LOCATION_UNMATCHED]} (destination)" in shown
    assert COULD_APPLY not in shown


def test_several_movements_are_numbered() -> None:
    body = dry_run_reply([view(), view()], PARTS, LOCATIONS)
    assert body["text"] == "Dry run: 2 movements, nothing changed."
    assert "*1. Take to a vehicle*" in text_of(body)


def test_a_reply_needs_a_movement_and_one_match_per_line() -> None:
    with pytest.raises(ValueError, match="at least one"):
        dry_run_reply([], PARTS, LOCATIONS)
    with pytest.raises(ValueError, match="one match per line"):
        view(items=())


@pytest.mark.parametrize(
    "fields",
    [
        {"items": (Match(MatchStatus.UNMATCHED),)},
        {"items": (Match(MatchStatus.AMBIGUOUS, MatchRule.FUZZY, candidates=(1, 2)),)},
        {"items": (Match(MatchStatus.MATCHED, MatchRule.FUZZY, 1),)},
        {"source": Match(MatchStatus.MATCHED, MatchRule.FUZZY, 10)},
        {"destination": Match(MatchStatus.UNMATCHED)},
        {"source": None},
    ],
)
def test_the_reply_never_says_could_apply_next_to_what_needs_a_human(fields: dict[str, Any]) -> None:
    shown = text_of(dry_run_reply([view(**fields)], PARTS, LOCATIONS))  # the caller passed no reasons
    assert COULD_APPLY not in shown
    assert "Needs a human before it could apply" in shown


def test_a_missing_source_is_a_reason_only_when_the_movement_needs_one() -> None:
    movement = CandidateMovement(MovementType.RECEIVE, (LineItem("Disc 40", Decimal(2)),), None, "receiving")
    shown = text_of(dry_run_reply([MovementView(movement, (matched(1),), None, matched(10), ())], PARTS, LOCATIONS))
    assert COULD_APPLY in shown
    take = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem("Disc 40", Decimal(2)),), None, "my truck")
    shown = text_of(dry_run_reply([MovementView(take, (matched(1),), None, matched(11), ())], PARTS, LOCATIONS))
    assert REASONS[ReasonCode.IMPLIED_SOURCE] in shown
    assert COULD_APPLY not in shown


def test_a_location_resolved_without_words_is_shown() -> None:
    movement = CandidateMovement(MovementType.TAKE_TO_VEHICLE, (LineItem("Disc 40", Decimal(2)),), None, "my truck")
    one = MovementView(movement, (matched(1),), matched(10), matched(11), ())
    assert "From *Warehouse/Main Stock* ((not stated))" in text_of(dry_run_reply([one], PARTS, LOCATIONS))


def test_many_movements_stay_inside_the_block_limit() -> None:
    body = dry_run_reply([view()] * (MAX_MOVEMENTS + 15), PARTS, LOCATIONS)
    assert len(body["blocks"]) <= 50
    assert "…and 15 more movements, not shown here." in text_of(body)
    assert body["text"] == f"Dry run: {MAX_MOVEMENTS + 15} movements, nothing changed."


def test_a_reason_says_which_line_or_side() -> None:
    reasons = (
        ReviewReason(ReasonCode.QUANTITY_ABOVE_LIMIT, ReasonSource.RULE, "line 2"),
        ReviewReason(ReasonCode.QUANTITY_ABOVE_LIMIT, ReasonSource.MODEL, "line 1"),
    )
    shown = text_of(dry_run_reply([view(reasons=reasons)], PARTS, LOCATIONS))
    assert f"{REASONS[ReasonCode.QUANTITY_ABOVE_LIMIT]} (line 1, line 2)" in shown


def test_every_outcome_of_an_answer_click_tells_the_clicker_something() -> None:
    outcomes = {"answered", "unknown_button", "other_message", "stale", "already_answered", "expired", "not_allowed"}
    outcomes |= {answered.value for answered in Answered}
    for outcome in outcomes:
        text = click_feedback(outcome, approver_only=False, approvers=True, expiry_hours=24)
        assert text, outcome
        assert not set(text) & set("{}<>*_`"), outcome  # plain words: no placeholder left, no markup


def test_the_clicker_is_told_nothing_about_a_payload_the_ingest_checks_drop() -> None:
    for outcome in [*(reason.value for reason in DropReason), "not_a_click", "action_not_handled_yet"]:
        assert click_feedback(outcome, approver_only=False, approvers=True, expiry_hours=24) is None, outcome


def test_click_feedback_names_the_expiry_and_who_may_answer() -> None:
    assert "for 36 hours after" in str(click_feedback("expired", approver_only=False, approvers=True, expiry_hours=36))
    assert "for 1 hour after" in str(click_feedback("expired", approver_only=False, approvers=True, expiry_hours=1))
    assert click_feedback("not_allowed", approver_only=True, approvers=False, expiry_hours=24) == NO_APPROVERS
    assert click_feedback("not_allowed", approver_only=True, approvers=True, expiry_hours=24) == APPROVERS_ONLY
    assert (
        click_feedback("not_allowed", approver_only=False, approvers=True, expiry_hours=24)
        == CLICK_FEEDBACK["not_allowed"]
    )
    assert click_feedback("stale", approver_only=True, approvers=True, expiry_hours=24) == CLICK_FEEDBACK["stale"]


def test_without_question_keeps_what_comes_before_the_marked_divider() -> None:
    reply = [{"type": "section", "block_id": "a"}, {"type": "divider"}, {"type": "section", "block_id": "b"}]
    question = [{"type": "divider", "block_id": "question"}, {"type": "section"}, {"type": "actions"}]
    assert without_question([*reply, *question]) == reply
    assert without_question(reply) is None  # an unmarked divider is part of the reply
    assert without_question([]) is None
    assert without_question([*reply, "junk", *question]) is None


def test_a_closed_question_says_who_answered_only_by_a_slack_user_id() -> None:
    def text(by: str | None) -> str:
        return str(question_closed(by)["elements"][0]["text"])

    assert text(None) == "This question was replaced by a newer reply in this thread."
    assert text("U0SAM0001") == "This question was answered by <@U0SAM0001>."
    assert text("<!channel>") == "This question was answered."
    assert text("U0SAM0001> <!here") == "This question was answered."
