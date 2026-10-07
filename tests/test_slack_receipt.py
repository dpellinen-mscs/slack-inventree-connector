"""The receipt in a post's thread once its plan's writes concluded: what it says for each outcome, its links into
InvenTree, and that neither post text nor configuration can become Slack markup in it."""

from __future__ import annotations

from typing import Any

import pytest
from views import DESTINATION, SOURCE, picked, view

from stockthread.core.model import MovementType
from stockthread.core.requests import RECEIPT_STATES, RequestState
from stockthread.slack.replies import MAX_BLOCKS, MAX_MOVEMENTS, RECEIPT_WORDS, receipt

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


def test_the_receipt_words_every_state_that_makes_a_plan_due_its_receipt_and_no_other() -> None:
    assert set(RECEIPT_WORDS) == RECEIPT_STATES


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
    assert f"Marker `{MARKER}` leads the notes of each stock history entry this wrote." in shown


def test_a_refused_write_says_nothing_changed() -> None:
    body = made((S.FAILED,))
    assert body["text"] == "Not applied: 1 movement."
    shown = text_of(body)
    assert shown.startswith("*Not applied.* InvenTree shows no change from it.")
    assert "not applied. InvenTree refused it, or shows no change from it." in shown
    assert f"Marker `{MARKER}`: no stock history entry carries it." in shown


@pytest.mark.parametrize(
    "states", [(S.VERIFY_FAILED,), (S.VERIFIED, S.VERIFY_FAILED), (S.VERIFIED, S.FAILED), (S.FAILED, S.VERIFY_FAILED)]
)
def test_anything_but_all_applied_or_none_asks_for_a_check(states: tuple[RequestState, ...]) -> None:
    body = made(states)
    assert body["text"].startswith("Check this: ")
    assert text_of(body).startswith("*Check this.* Not everything below went as planned.")


def test_a_read_back_that_did_not_match_says_to_check_inventree() -> None:
    shown = text_of(made((S.VERIFY_FAILED,)))
    assert "written, but reading it back did not match the plan. Check it in InvenTree." in shown


def test_the_receipt_names_the_approver_who_clicked() -> None:
    assert f"Approved by <@{SAM}>." in text_of(made((S.VERIFIED,), approved_by=SAM))
    assert "Approved by a click." in text_of(made((S.VERIFIED,), approved_by="<!channel>"))
    assert "<!channel>" not in text_of(made((S.VERIFIED,), approved_by="<!channel>"))


def test_several_movements_are_numbered_with_their_own_state() -> None:
    shown = text_of(made((S.VERIFIED, S.FAILED)))
    assert "*1. Take to a vehicle*: applied, and read back as planned." in shown
    assert "*2. Take to a vehicle*: not applied." in shown


def test_a_receipt_reports_only_concluded_writes_one_state_and_marker_per_movement() -> None:
    for state in set(RequestState) - RECEIPT_STATES:
        with pytest.raises(ValueError, match="concluded"):
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
