# SPDX-License-Identifier: MIT
"""The receipt in a post's thread once its plan's writes concluded: what it says for each outcome, its links into
InvenTree, and that neither post text nor configuration can become Slack markup in it."""

from __future__ import annotations

from typing import Any

import pytest
from views import DESTINATION, SOURCE, picked, view

from stockthread.core.model import MovementType
from stockthread.core.requests import UNDOABLE, WORKING, RequestState
from stockthread.slack.replies import (
    MAX_BLOCKS,
    MAX_MOVEMENTS,
    MAX_SECTION,
    RECEIPT_WORDS,
    UNDO_ACTION,
    UNDO_CONFIRM,
    receipt,
)

S = RequestState
PARTS = {1: "Norvane Disc 40 Access Point", 2: "UPS 600 VA"}
LOCATIONS = {SOURCE: "Warehouse/Main Stock", DESTINATION: "Vehicles/Truck 1"}
URL = "https://inventree.acme-fiber.test"
MARKER = "0123456789abcdefghjkmn"
SAM = "U0SAM0001"


def text_of(body: dict[str, Any]) -> str:
    texts = []
    for block in body["blocks"]:
        if block["type"] == "section":
            texts.append(block["text"]["text"])
        else:
            texts.extend(element["text"] for element in block["elements"])
    return "\n".join(texts)


def made(states: tuple[RequestState, ...], *, approved_by: str | None = None, url: str = URL) -> dict[str, Any]:
    views = [view(stock=(picked((57, "2")),)) for _ in states]
    return receipt(views, states, [MARKER] * len(states), PARTS, LOCATIONS, inventree_url=url, approved_by=approved_by)


def test_the_receipt_words_every_state_but_those_a_step_is_due_or_in_flight_for() -> None:
    assert set(RECEIPT_WORDS) == set(RequestState) - WORKING


def test_a_take_out_applied_and_read_back() -> None:
    body = made((S.VERIFIED,))
    assert body["text"] == "Applied: 1 movement."
    shown = text_of(body)
    assert shown.startswith("*Applied.* Inventory was changed as below, and read back to check it.")
    assert "Approved by the write policy, with no click." in shown
    assert "*Take to a vehicle*: applied, and read back as planned." in shown
    assert "2 \N{MULTIPLICATION SIGN} *Norvane Disc 40 Access Point* (“Disc 40”) (from stock item #57)" in shown
    assert "From *Warehouse/Main Stock* (“main stock”) to *Vehicles/Truck 1* (“my truck”)" in shown
    assert f"<{URL}/web/stock/location/{SOURCE}|Warehouse/Main Stock>" in shown
    assert f"<{URL}/web/stock/location/{DESTINATION}|Vehicles/Truck 1>" in shown
    assert f"<{URL}/web/stock/item/57|stock item #57>" in shown
    assert f"Marker `{MARKER}`: InvenTree's stock history entries of this request carry it in their notes." in shown


def test_a_refused_write_says_nothing_changed() -> None:
    body = made((S.FAILED,))
    assert body["text"] == "Not applied: 1 movement."
    shown = text_of(body)
    assert shown.startswith("*Not applied.* Approved by the write policy")
    assert "not applied. InvenTree refused it, or showed no change from it when checked." in shown


@pytest.mark.parametrize(
    ("state", "words"),
    [
        (S.PLANNED, "not applied. It was not sent, because an earlier movement did not go through."),
        (S.REJECTED, "not applied. It was rejected."),
        (S.CANCELLED, "not applied. A newer reading of the post replaced it."),
        (S.EXPIRED, "not applied. It expired."),
    ],
)
def test_a_request_never_written_is_not_applied(state: RequestState, words: str) -> None:
    body = made((S.FAILED, state))
    assert body["text"] == "Not applied: 2 movements."
    assert f"*2. Take to a vehicle*: {words}" in text_of(body)


@pytest.mark.parametrize(
    "states",
    [
        (S.VERIFY_FAILED,),
        (S.ACCEPTED,),
        (S.UNDO_FAILED,),
        (S.UNDONE, S.UNDO_FAILED),
        (S.UNDONE, S.VERIFY_FAILED),
        (S.VERIFIED, S.UNDO_FAILED),
        (S.VERIFIED, S.UNDONE, S.FAILED),
        (S.UNDONE, S.PLANNED),
        (S.VERIFIED, S.VERIFY_FAILED),
        (S.VERIFIED, S.ACCEPTED, S.PLANNED),
        (S.FAILED, S.VERIFY_FAILED),
    ],
)
def test_anything_but_all_applied_or_none_asks_for_a_check(states: tuple[RequestState, ...]) -> None:
    body = made(states)
    assert body["text"].startswith("Check this: ")
    assert text_of(body).startswith("*Check this.* Not everything below went as planned.")


@pytest.mark.parametrize(
    "states",
    [
        (S.UNDONE,),
        (S.UNDONE, S.UNDONE),
        (S.UNDONE, S.FAILED),
        (S.CANCELLED, S.UNDONE),
        (S.UNDONE, S.REJECTED),
        (S.EXPIRED, S.UNDONE),
    ],
)
def test_an_undo_of_everything_applied_says_undone(states: tuple[RequestState, ...]) -> None:
    body = made(states)
    assert body["text"].startswith("Undone: ")
    shown = text_of(body)
    assert shown.startswith("*Undone.* What was applied below was put back, and read back to check it.")
    assert "*: undone." in shown


@pytest.mark.parametrize("states", [(S.VERIFIED, S.UNDONE), (S.UNDONE, S.VERIFIED, S.VERIFIED)])
def test_an_undo_of_some_movements_says_the_rest_stays_applied(states: tuple[RequestState, ...]) -> None:
    body = made(states)
    assert body["text"].startswith("Partly undone: ")
    shown = text_of(body)
    assert shown.startswith("*Partly undone.* What is marked undone below was put back; the rest stays applied.")
    assert "*: undone." in shown
    assert "*: applied, and read back as planned." in shown


@pytest.mark.parametrize(
    "states",
    [
        (S.VERIFIED, S.PLANNED),
        (S.VERIFIED, S.VERIFIED, S.EXPIRED),
        (S.CANCELLED, S.VERIFIED),
        (S.VERIFIED, S.REJECTED),
    ],
)
def test_a_plan_applied_only_in_part_says_so_and_how_to_record_the_rest(states: tuple[RequestState, ...]) -> None:
    body = made(states)
    assert body["text"] == f"Partly applied: {len(states)} movements."
    assert text_of(body).startswith(
        "*Partly applied.* What is marked applied below changed inventory, and it was read back to check it."
        " The rest was not applied: to record it, post it again. Approved by the write policy"
    )


@pytest.mark.parametrize("states", [(S.VERIFIED, S.FAILED), (S.VERIFIED, S.FAILED, S.PLANNED), (S.FAILED, S.VERIFIED)])
def test_a_plan_applied_in_part_with_a_failed_write_does_not_invite_a_second_post(
    states: tuple[RequestState, ...],
) -> None:
    """A `failed` request concluded by recovery may still land (ADR 0015), and a new post would write it again."""
    body = made(states)
    assert body["text"] == f"Partly applied: {len(states)} movements."
    shown = text_of(body)
    assert shown.startswith(
        "*Partly applied.* What is marked applied below changed inventory, and it was read back to check it."
        " The rest was not applied. Check InvenTree before recording any of it again: a write that showed no change"
        " may still land later. Approved by the write policy"
    )
    assert "post it again" not in shown


@pytest.mark.parametrize(
    ("states", "words"),
    [
        ((S.PLANNED,), "not applied. Stock changed since the plan, so it waits for review."),
        ((S.PLANNED, S.PLANNED), "not applied. Stock changed since the plan, so it waits for review."),
        ((S.VERIFIED, S.PLANNED), "not applied. It was not sent."),
        ((S.UNDONE, S.PLANNED), "not applied. It was not sent."),  # the first went through: it was undone later
        ((S.EXPIRED, S.PLANNED), "not applied. It was not sent."),
        ((S.FAILED, S.PLANNED), "not applied. It was not sent, because an earlier movement did not go through."),
        ((S.VERIFY_FAILED, S.PLANNED), "not applied. It was not sent, because an earlier movement did not go through."),
        ((S.ACCEPTED, S.PLANNED), "not applied. It was not sent, because an earlier movement did not go through."),
    ],
)
def test_a_movement_sent_back_unsent_waits_for_review_only_when_its_whole_plan_does(
    states: tuple[RequestState, ...], words: str
) -> None:
    """A plan is approved whole: once one of its requests is not planned, a click can approve none of the rest."""
    shown = text_of(made(states))
    title = "Take to a vehicle" if len(states) == 1 else f"{len(states)}. Take to a vehicle"
    assert f"*{title}*: {words}" in shown


def test_a_movement_after_one_that_went_wrong_says_so_whatever_came_between() -> None:
    shown = text_of(made((S.VERIFIED, S.FAILED, S.PLANNED)))
    assert "*1. Take to a vehicle*: applied, and read back as planned." in shown
    assert "*2. Take to a vehicle*: not applied. InvenTree refused it" in shown
    assert (
        "*3. Take to a vehicle*: not applied. It was not sent, because an earlier movement did not go through." in shown
    )
    assert "waits for review" not in shown


def test_a_read_back_that_did_not_match_says_to_check_inventree() -> None:
    shown = text_of(made((S.VERIFY_FAILED,)))
    assert "the check of this write found a problem. Look at it in InvenTree before relying on it." in shown
    assert "written," not in shown  # the check may have found the problem before anything was sent


def test_the_receipt_names_the_approver_who_clicked() -> None:
    assert f"Approved by <@{SAM}>." in text_of(made((S.VERIFIED,), approved_by=SAM))
    assert "Approved by a click." in text_of(made((S.VERIFIED,), approved_by="<!channel>"))
    assert "<!channel>" not in text_of(made((S.VERIFIED,), approved_by="<!channel>"))


def test_several_movements_are_numbered_with_their_own_state() -> None:
    shown = text_of(made((S.VERIFIED, S.FAILED)))
    assert "*1. Take to a vehicle*: applied, and read back as planned." in shown
    assert "*2. Take to a vehicle*: not applied." in shown


def test_a_receipt_reports_only_concluded_writes_one_state_and_marker_per_movement() -> None:
    for state in WORKING:
        with pytest.raises(ValueError, match="due or in flight"):
            made((state,))
    with pytest.raises(ValueError, match="one state and one marker"):
        receipt([view()], [S.VERIFIED, S.VERIFIED], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None)
    with pytest.raises(ValueError, match="one state and one marker"):
        receipt([view()], [S.VERIFIED], [], PARTS, LOCATIONS, inventree_url=URL, approved_by=None)
    with pytest.raises(ValueError, match="at least one"):
        receipt([], [], [], PARTS, LOCATIONS, inventree_url=URL, approved_by=None)


def test_a_url_with_a_path_links_below_it_and_cannot_end_the_link() -> None:
    shown = text_of(made((S.VERIFIED,), url="https://acme-fiber.test/inv/"))
    assert "<https://acme-fiber.test/inv/web/stock/item/57|stock item #57>" in shown
    shown = text_of(made((S.VERIFIED,), url="https://acme-fiber.test/a|b><!channel>&x"))
    assert "<!channel>" not in shown
    assert "https://acme-fiber.test/a%7Cb%3E%3C!channel%3E%26x/web/stock/item/57|" in shown


def test_post_text_and_catalog_names_are_escaped_in_the_receipt() -> None:
    sneaky = view(lines=(("<!channel> AP", "1"),), stock=(picked((57, "1")),))
    body = receipt(
        [sneaky],
        [S.VERIFIED],
        [MARKER],
        {1: "Disc <@U0LUIS001>"},
        {SOURCE: "Main|<https://evil.test>"},
        inventree_url=URL,
        approved_by=None,
    )
    shown = text_of(body)
    assert "<!channel>" not in shown
    assert "<@U0LUIS001>" not in shown
    assert "<https://evil.test>" not in shown
    assert "|Main|&lt;https://evil.test&gt;>" in shown


def test_a_use_up_links_its_source_and_a_count_its_item() -> None:
    used = view(MovementType.CONSUME, stock=(picked((57, "2")),))
    shown = text_of(receipt([used], [S.VERIFIED], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None))
    assert f"/web/stock/location/{SOURCE}|" in shown
    assert "/web/stock/location/" + str(DESTINATION) not in shown
    counted = view(MovementType.COUNT, stock=(picked((57, "5")),))
    shown = text_of(receipt([counted], [S.VERIFIED], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None))
    assert "(counts stock item #57)" in shown
    assert f"<{URL}/web/stock/item/57|stock item #57>" in shown


def test_a_receipt_of_many_movements_fits_in_one_message() -> None:
    states = (S.VERIFIED,) * (MAX_MOVEMENTS + 3)
    body = made(states)
    assert len(body["blocks"]) <= MAX_BLOCKS
    assert body["text"] == f"Applied: {MAX_MOVEMENTS + 3} movements."
    assert "…and 3 more movements, not shown here." in text_of(body)


def test_a_movement_too_long_for_its_links_keeps_its_lines_and_drops_the_links() -> None:
    lines = tuple((f"Part {n} with a rather long description", "1") for n in range(25))
    long = view(lines=lines, stock=tuple(picked((100 + n, "1")) for n in range(25)))
    body = receipt([long], [S.VERIFIED], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None)
    section = body["blocks"][1]["text"]["text"]
    assert len(section) <= MAX_SECTION
    assert "/web/stock/" not in section
    assert section.endswith("(“my truck”)")  # whole: nothing was cut
    assert "<" not in section.replace("&lt;", "")


def test_a_url_that_is_not_ascii_gets_no_links() -> None:
    shown = text_of(made((S.VERIFIED,), url="https://lager.bücher.test"))
    assert "/web/stock/" not in shown
    assert "In InvenTree" not in shown


def test_an_undoable_movement_carries_an_undo_button_that_asks_to_confirm() -> None:
    views = [view(stock=(picked((57, "2")),)), view(stock=(picked((58, "1")),))]
    body = receipt(
        views, [S.VERIFIED, S.FAILED], [MARKER, MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None,
        undo=[MARKER, None],
    )  # fmt: skip
    buttons = [block["accessory"] for block in body["blocks"] if "accessory" in block]
    assert len(buttons) == 1
    assert body["blocks"][1]["accessory"] is buttons[0]  # on the first movement's section
    assert buttons[0]["action_id"] == UNDO_ACTION
    assert buttons[0]["value"] == MARKER
    assert buttons[0]["text"]["text"] == "Undo"
    assert buttons[0]["confirm"] == UNDO_CONFIRM
    assert len(body["blocks"]) == len(made((S.VERIFIED, S.FAILED))["blocks"])  # no block of its own


def test_no_undo_button_unless_asked_for() -> None:
    assert not [block for block in made((S.VERIFIED,))["blocks"] if "accessory" in block]


def test_an_undo_button_needs_a_marker_id_and_a_state_an_undo_may_start_from() -> None:
    def with_undo(state: RequestState, value: str | None) -> dict[str, Any]:
        return receipt([view()], [state], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None, undo=[value])

    for state in UNDOABLE:
        assert "accessory" in with_undo(state, MARKER)["blocks"][1]
    for state in set(RECEIPT_WORDS) - UNDOABLE:
        with pytest.raises(ValueError, match="undo may start from"):
            with_undo(state, MARKER)
    with pytest.raises(ValueError, match="marker id"):
        with_undo(S.VERIFIED, "<!channel>")
    with pytest.raises(ValueError, match="one undo value"):
        receipt([view()], [S.VERIFIED], [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=None, undo=[])


def test_a_receipt_of_many_undoable_movements_still_fits_in_one_message() -> None:
    count = MAX_MOVEMENTS + 3
    body = receipt(
        [view(stock=(picked((57, "2")),))] * count, [S.VERIFIED] * count, [MARKER] * count, PARTS, LOCATIONS,
        inventree_url=URL, approved_by=None, undo=[MARKER] * count,
    )  # fmt: skip
    assert len(body["blocks"]) <= MAX_BLOCKS
    assert len([block for block in body["blocks"] if "accessory" in block]) == MAX_MOVEMENTS


def test_the_aliases_a_verified_plan_taught_are_named_last_with_their_text_escaped() -> None:
    views = [view(stock=(picked((57, "2")),))]
    learned = [("norvane 40", "Norvane Disc 40 Access Point"), ("<!here> bay", "Warehouse/Bay *2*")]
    body = receipt(views, (S.VERIFIED,), [MARKER], PARTS, LOCATIONS, inventree_url=URL, approved_by=SAM,
                   learned=learned)  # fmt: skip
    note = body["blocks"][-1]
    assert note["type"] == "context"
    assert note["elements"][0]["text"] == (
        "Learned from this Apply: “norvane 40” means Norvane Disc 40 Access Point; “&lt;!here&gt; bay” means"
        " Warehouse/Bay *2*."
    )


@pytest.mark.parametrize("state", sorted(set(RECEIPT_WORDS) - {S.VERIFIED}))
def test_no_alias_is_named_unless_every_movement_is_verified(state: RequestState) -> None:
    views = [view(stock=(picked((57, "2")),))] * 2
    body = receipt(views, (S.VERIFIED, state), [MARKER] * 2, PARTS, LOCATIONS, inventree_url=URL, approved_by=SAM,
                   learned=[("norvane 40", "Norvane Disc 40 Access Point")])  # fmt: skip
    assert "Learned" not in text_of(body)
