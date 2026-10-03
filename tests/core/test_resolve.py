"""Source selection (core/resolve.py): which stock items a line takes, by the documented rule."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from stockthread.core.model import Operation, ReasonCode
from stockthread.core.resolve import (
    DETAIL_BATCH,
    DETAIL_CHOSEN_GONE,
    DETAIL_CHOSEN_SERIALIZED,
    DETAIL_CHOSEN_SHORT,
    DETAIL_COUNTED,
    DETAIL_EMPTIED,
    DETAIL_MIXED,
    DETAIL_NO_STOCK,
    DETAIL_NO_TARGET,
    DETAIL_NONE_CHOSEN,
    DETAIL_SERIAL_COUNT,
    DETAIL_SERIAL_NOT_FOUND,
    DETAIL_SERIALIZED_REMOVE,
    DETAIL_SERIALIZED_TARGET,
    DETAIL_SERIALS_NEEDED,
    DETAIL_SEVERAL_TARGETS,
    DETAIL_SHARED,
    DETAIL_SUPPLIER_PART,
    DETAIL_TOO_FEW,
    MAX_OFFER,
    LineStock,
    StockItem,
    Take,
    pick_source,
    resolve_line,
    resolve_lines,
    stock_items,
    target_item,
)


def item(pk: int, quantity: str = "10", **fields: Any) -> StockItem:
    return StockItem(
        pk,
        Decimal(quantity),
        fields.get("status", 10),
        Decimal(fields.get("allocated", "0")),
        fields.get("serial"),
        fields.get("batch"),
        fields.get("supplier_part"),
    )


def takes(*pairs: tuple[int, str]) -> tuple[Take, ...]:
    return tuple(Take(pk, Decimal(quantity)) for pk, quantity in pairs)


def codes(pick: Any) -> list[tuple[ReasonCode, str]]:
    return [(reason.code, reason.detail) for reason in pick.reasons]


def test_one_item_that_covers_the_quantity_is_preferred_over_a_split_and_the_oldest_of_those() -> None:
    pick = pick_source(Decimal(3), [item(9, "5"), item(4, "2"), item(7, "3")])
    assert pick.takes == takes((7, "3"))  # 4 is older but too small; 7 is older than 9
    assert pick.decided


def test_without_a_covering_item_the_quantity_is_split_oldest_first() -> None:
    pick = pick_source(Decimal(6), [item(3, "4"), item(1, "1"), item(2, "4")])
    assert pick.takes == takes((1, "1"), (2, "4"), (3, "1"))
    assert pick.decided


@pytest.mark.parametrize(
    "excluded",
    [{"status": 50}, {"allocated": "1"}, {"quantity": "0"}],
    ids=["status not OK", "allocated", "empty"],
)
def test_items_not_ok_allocated_or_empty_are_never_picked(excluded: dict[str, Any]) -> None:
    quantity = excluded.pop("quantity", "10")
    pick = pick_source(Decimal(2), [item(1, quantity, **excluded), item(2, "5")])
    assert pick.takes == takes((2, "2"))
    alone = pick_source(Decimal(2), [item(1, quantity, **excluded)])
    assert (alone.takes, codes(alone)) == ((), [(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_NO_STOCK)])


def test_too_little_stock_is_a_reason_and_picks_nothing() -> None:
    pick = pick_source(Decimal(8), [item(1, "3"), item(2, "4")])
    assert (pick.takes, codes(pick)) == ((), [(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW)])
    assert not pick.decided


def test_no_stock_at_all_is_a_reason() -> None:
    pick = pick_source(Decimal(1), [])
    assert codes(pick) == [(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_NO_STOCK)]


@pytest.mark.parametrize(("field", "values", "detail"), [
    ("batch", ("B-1", "B-2"), DETAIL_BATCH),
    ("batch", ("B-1", None), DETAIL_BATCH),
    ("supplier_part", (3, 4), DETAIL_SUPPLIER_PART),
])  # fmt: skip
def test_a_choice_between_items_that_differ_is_a_proposal_with_a_reason(
    field: str, values: tuple[Any, Any], detail: str
) -> None:
    pool = [item(1, "5", **{field: values[0]}), item(2, "5", **{field: values[1]})]
    pick = pick_source(Decimal(2), pool)
    assert pick.takes == takes((1, "2"))  # still proposed, so the reply can show it
    assert codes(pick) == [(ReasonCode.STOCK_CHOICE, detail)]
    assert not pick.decided


def test_items_that_differ_need_no_human_when_the_pick_takes_all_of_them() -> None:
    pick = pick_source(Decimal(10), [item(1, "5", batch="B-1"), item(2, "5", batch="B-2")])
    assert pick.takes == takes((1, "5"), (2, "5"))
    assert pick.decided


def test_items_that_agree_need_no_human() -> None:
    pick = pick_source(
        Decimal(2), [item(1, "5", batch="B-1", supplier_part=3), item(2, "5", batch="B-1", supplier_part=3)]
    )
    assert pick.decided


def test_serialized_stock_is_picked_by_serial_number_as_whole_items() -> None:
    pool = [item(5, "1", serial="SN-3"), item(2, "1", serial="SN-1"), item(4, "1", serial="SN-2")]
    pick = pick_source(Decimal(2), pool, ("SN-3", "SN-1"))
    assert pick.takes == takes((2, "1"), (5, "1"))
    assert pick.decided


@pytest.mark.parametrize(("quantity", "serials", "code", "detail"), [
    ("2", (), ReasonCode.STOCK_CHOICE, DETAIL_SERIALS_NEEDED),
    ("4", (), ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW),
    ("2", ("SN-1",), ReasonCode.STOCK_CHOICE, DETAIL_SERIAL_COUNT),
    ("1.5", (), ReasonCode.STOCK_CHOICE, DETAIL_SERIAL_COUNT),
    ("1", ("SN-9",), ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_SERIAL_NOT_FOUND),
])  # fmt: skip
def test_serialized_stock_without_the_right_serials_is_for_a_human(
    quantity: str, serials: tuple[str, ...], code: ReasonCode, detail: str
) -> None:
    pool = [item(1, "1", serial="SN-1"), item(2, "1", serial="SN-2"), item(3, "1", serial="SN-3")]
    pick = pick_source(Decimal(quantity), pool, serials)
    assert (pick.takes, codes(pick)) == ((), [(code, detail)])


def test_serialized_stock_taken_whole_needs_no_serials() -> None:
    pick = pick_source(Decimal(2), [item(1, "1", serial="SN-1"), item(2, "1", serial="SN-2")])
    assert pick.takes == takes((1, "1"), (2, "1"))
    assert pick.decided


def test_a_serial_for_stock_that_is_not_serialized_is_not_found() -> None:
    pick = pick_source(Decimal(1), [item(1, "5")], ("SN-1",))
    assert codes(pick) == [(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_SERIAL_NOT_FOUND)]


def test_serialized_and_unserialized_stock_of_one_part_is_for_a_human() -> None:
    pick = pick_source(Decimal(1), [item(1, "1", serial="SN-1"), item(2, "5")])
    assert (pick.takes, codes(pick)) == ((), [(ReasonCode.STOCK_CHOICE, DETAIL_MIXED)])


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_a_pick_is_for_a_quantity_above_zero(quantity: str) -> None:
    with pytest.raises(ValueError, match="above zero"):
        pick_source(Decimal(quantity), [item(1)])


def test_an_add_or_a_count_needs_exactly_one_stock_item() -> None:
    assert target_item([item(4, "2", status=50, allocated="2")]) == (4, ())
    assert [(r.code, r.detail) for r in target_item([])[1]] == [(ReasonCode.NO_TARGET_STOCK_ITEM, DETAIL_NO_TARGET)]
    for pool, detail in (
        ([item(1), item(2)], DETAIL_SEVERAL_TARGETS),
        ([item(1, "1", serial="SN-1")], DETAIL_SERIALIZED_TARGET),
    ):
        found, reasons = target_item(pool)
        assert found is None
        assert [(r.code, r.detail) for r in reasons] == [(ReasonCode.STOCK_CHOICE, detail)]


ROW = {"pk": 7, "quantity": 12.5, "status": 10, "allocated": 0, "serial": None, "batch": "", "supplier_part": None}


def test_rows_are_read_with_their_quantities_exact() -> None:
    [read] = stock_items([{**ROW, "quantity": "12.50000", "serial": " SN-1 ", "batch": "B-1"}])
    assert read == StockItem(7, Decimal("12.50000"), 10, Decimal(0), "SN-1", "B-1", None)
    [blank] = stock_items([ROW])
    assert (blank.quantity, blank.serial, blank.batch) == (Decimal("12.5"), None, None)


@pytest.mark.parametrize("change", [
    {"pk": None}, {"pk": True}, {"pk": 0}, {"status": "10"}, {"supplier_part": "3"},
    {"quantity": None}, {"quantity": "x"}, {"quantity": -1}, {"quantity": "Infinity"}, {"quantity": True},
    {"allocated": None}, {"serial": 5}, {"batch": []},
])  # fmt: skip
def test_a_malformed_row_raises_rather_than_moves_stock(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="a stock item"):
        stock_items([{**ROW, **change}])


def test_a_row_without_its_allocation_raises() -> None:
    row = dict(ROW)
    del row["allocated"]
    with pytest.raises(ValueError, match="allocated"):
        stock_items([row])


@pytest.mark.parametrize("operation", [Operation.TRANSFER, Operation.REMOVE])
def test_a_transfer_or_a_remove_picks_its_source(operation: Operation) -> None:
    items = [item(1, "1"), item(2, "5")]
    assert resolve_line(operation, Decimal(2), items) == pick_source(Decimal(2), items)
    serialized = [item(3, "1", serial="A1"), item(4, "1", serial="A2")]
    assert resolve_line(operation, Decimal(1), serialized, ["A2"]).takes == (Take(4, Decimal(1)),)


@pytest.mark.parametrize("operation", [Operation.ADD, Operation.COUNT])
def test_an_add_or_a_count_acts_on_its_one_item_by_the_quantity(operation: Operation) -> None:
    picked = resolve_line(operation, Decimal(4), [item(7, "1", allocated="1")])
    assert (picked.takes, picked.reasons, picked.decided) == ((Take(7, Decimal(4)),), (), True)
    assert resolve_line(operation, Decimal(0), [item(7)]).takes == (Take(7, Decimal(0)),)
    several = resolve_line(operation, Decimal(4), [item(7), item(8)])
    assert (several.takes, [r.detail for r in several.reasons]) == ((), [DETAIL_SEVERAL_TARGETS])
    none = resolve_line(operation, Decimal(4), [])
    assert (none.takes, [r.detail for r in none.reasons]) == ((), [DETAIL_NO_TARGET])


# The lines of one post share the stock (resolve_lines).

TRANSFER, REMOVE, ADD, COUNT = Operation.TRANSFER, Operation.REMOVE, Operation.ADD, Operation.COUNT


def test_two_lines_of_one_part_never_take_the_same_units() -> None:
    stock = [item(57, "3")]
    first, second = resolve_lines([LineStock(TRANSFER, Decimal(2), stock), LineStock(TRANSFER, Decimal(3), stock)])
    assert (first.takes, first.decided) == (takes((57, "2")), True)
    assert second.takes == ()
    assert codes(second) == [(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_SHARED)]


def test_a_later_line_picks_from_what_the_earlier_lines_left() -> None:
    stock = [item(57, "3"), item(60, "5")]
    picks = resolve_lines([LineStock(REMOVE, Decimal(2), stock), LineStock(TRANSFER, Decimal(3), stock)])
    assert [pick.takes for pick in picks] == [takes((57, "2")), takes((60, "3"))]  # 57 alone would have covered 3
    assert all(pick.decided for pick in picks)
    split = resolve_lines([LineStock(TRANSFER, Decimal(2), stock), LineStock(TRANSFER, Decimal(6), stock)])
    assert [pick.takes for pick in split] == [takes((57, "2")), takes((57, "1"), (60, "5"))]
    assert all(pick.decided for pick in split)


def test_lines_that_together_fit_the_stock_need_no_human() -> None:
    stock = [item(57, "5")]
    picks = resolve_lines([LineStock(TRANSFER, Decimal(2), stock), LineStock(TRANSFER, Decimal(3), stock)])
    assert [pick.takes for pick in picks] == [takes((57, "2")), takes((57, "3"))]
    assert all(pick.decided for pick in picks)


def test_a_serial_number_taken_by_one_line_is_not_there_for_the_next() -> None:
    stock = [item(3, "1", serial="A1"), item(4, "1", serial="A2")]
    first, second = resolve_lines(
        [LineStock(TRANSFER, Decimal(1), stock, ["A2"]), LineStock(REMOVE, Decimal(1), stock, ["A2"])]
    )
    assert (first.takes, first.decided) == (takes((4, "1")), True)
    assert second.takes == ()
    assert DETAIL_SHARED in [detail for _, detail in codes(second)]
    rest = resolve_lines([LineStock(TRANSFER, Decimal(1), stock, ["A1"]), LineStock(TRANSFER, Decimal(1), stock)])
    assert [pick.takes for pick in rest] == [takes((3, "1")), takes((4, "1"))]  # every one that is left: decided
    assert all(pick.decided for pick in rest)


def test_a_line_that_fails_on_the_stock_as_read_says_why_and_that_others_share_it() -> None:
    stock = [item(57, "1")]
    picks = resolve_lines([LineStock(TRANSFER, Decimal(1), stock), LineStock(TRANSFER, Decimal(4), stock)])
    assert codes(picks[1]) == [  # 4 never fitted; "no stock item" (what is left) is the other line's doing
        (ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW),
        (ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_SHARED),
    ]
    batches = [item(57, "3", batch="B1"), item(60, "3", batch="B2")]
    picks = resolve_lines([LineStock(TRANSFER, Decimal(2), batches), LineStock(TRANSFER, Decimal(2), batches)])
    assert picks[1].takes == takes((60, "2"))  # a proposal: its takes still count for the lines after it
    assert codes(picks[1]) == [(ReasonCode.STOCK_CHOICE, DETAIL_BATCH)]  # enough is left: only the choice


def test_a_choice_among_what_is_left_is_a_choice_not_a_shortage() -> None:
    stock = [item(41, "5", batch="B-1"), item(57, "5", batch="B-2")]
    first, second = resolve_lines([LineStock(TRANSFER, Decimal(2), stock), LineStock(TRANSFER, Decimal(2), stock)])
    assert (first.takes, codes(first)) == (takes((41, "2")), [(ReasonCode.STOCK_CHOICE, DETAIL_BATCH)])
    assert (second.takes, codes(second)) == (takes((41, "2")), [(ReasonCode.STOCK_CHOICE, DETAIL_BATCH)])
    serials = [item(3, "1", serial="A1"), item(4, "1", serial="A2"), item(5, "1", serial="A3")]
    named, unnamed = resolve_lines(
        [LineStock(TRANSFER, Decimal(1), serials, ["A1"]), LineStock(TRANSFER, Decimal(1), serials)]
    )
    assert named.decided
    assert codes(unnamed) == [(ReasonCode.STOCK_CHOICE, DETAIL_SERIALS_NEEDED)]  # two left for one: serials pick


@pytest.mark.parametrize("operation", [TRANSFER, REMOVE])
def test_an_add_into_an_item_an_earlier_line_takes_in_full_is_for_a_human(operation: Operation) -> None:
    stock = [item(57, "3")]
    take, add = resolve_lines([LineStock(operation, Decimal(3), stock), LineStock(ADD, Decimal(5), stock)])
    assert take.decided
    assert (add.takes, codes(add)) == (takes((57, "5")), [(ReasonCode.STOCK_CHOICE, DETAIL_EMPTIED)])
    two = resolve_lines(
        [
            LineStock(operation, Decimal(1), stock),
            LineStock(operation, Decimal(2), stock),
            LineStock(ADD, Decimal(5), stock),
        ]
    )
    assert codes(two[2]) == [(ReasonCode.STOCK_CHOICE, DETAIL_EMPTIED)]  # together they take it all
    part = resolve_lines([LineStock(operation, Decimal(2), stock), LineStock(ADD, Decimal(5), stock)])
    assert all(pick.decided for pick in part)  # a partial take splits: the item stays where it was


def test_an_add_takes_nothing_from_the_stock_the_other_lines_share() -> None:
    stock = [item(57, "3")]
    picks = resolve_lines([LineStock(ADD, Decimal(5), stock), LineStock(TRANSFER, Decimal(3), stock)])
    assert [pick.takes for pick in picks] == [takes((57, "5")), takes((57, "3"))]
    assert all(pick.decided for pick in picks)  # the transfer reads the stock as it was before the receipt


def test_a_counted_item_another_line_acts_on_is_for_a_human_on_both_lines() -> None:
    stock = [item(57, "3")]
    count, transfer = resolve_lines([LineStock(COUNT, Decimal(4), stock), LineStock(TRANSFER, Decimal(1), stock)])
    assert codes(count) == [(ReasonCode.STOCK_CHOICE, DETAIL_COUNTED)]
    assert codes(transfer) == [(ReasonCode.STOCK_CHOICE, DETAIL_COUNTED)]
    alone = resolve_lines([LineStock(COUNT, Decimal(4), stock), LineStock(TRANSFER, Decimal(1), [item(60, "3")])])
    assert all(pick.decided for pick in alone)


# The stock question: what a pick offers, and the stock items a person chose (design section 4).


def test_a_choice_that_matters_offers_the_items_that_cover_the_line_alone_oldest_first() -> None:
    stock = [item(60, "5", batch="B"), item(57, "1", batch="A"), item(58, "4", batch="A"), item(59, "9", status=55)]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(3), stock)])
    assert codes(pick) == [(ReasonCode.STOCK_CHOICE, DETAIL_BATCH)]
    assert [offered.pk for offered in pick.offer] == [58, 60]  # 57 is too small, 59 is not OK
    assert pick.split == ()


def test_a_choice_that_matters_without_a_covering_item_offers_the_rules_split() -> None:
    stock = [item(57, "2", batch="A"), item(58, "2", batch="B"), item(59, "2", batch="B")]
    (pick,) = resolve_lines([LineStock(REMOVE, Decimal(3), stock)])
    assert pick.offer == ()
    assert pick.split == pick.takes == takes((57, "2"), (58, "1"))


def test_the_offer_is_at_most_max_offer_items() -> None:
    stock = [item(pk, "5", batch=f"B{pk}") for pk in range(1, MAX_OFFER + 3)]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(1), stock)])
    assert [offered.pk for offered in pick.offer] == list(range(1, MAX_OFFER + 1))


def test_the_offer_is_from_what_the_lines_before_left() -> None:
    stock = [item(57, "5", batch="A"), item(58, "5", batch="B")]
    first, second = resolve_lines([LineStock(TRANSFER, Decimal(3), stock), LineStock(TRANSFER, Decimal(3), stock)])
    assert first.takes == takes((57, "3"))
    assert [offered.pk for offered in second.offer] == [58]  # 57 has 2 left
    assert second.offer[0].quantity == Decimal(5)


def test_reasons_a_choice_cannot_settle_offer_nothing() -> None:
    (short,) = resolve_lines([LineStock(TRANSFER, Decimal(9), [item(57, "2", batch="A"), item(58, "2", batch="B")])])
    assert codes(short) == [(ReasonCode.QUANTITY_ABOVE_STOCK, DETAIL_TOO_FEW)]
    assert (short.offer, short.split) == ((), ())
    (serials,) = resolve_lines(
        [LineStock(TRANSFER, Decimal(1), [item(57, "1", serial="1"), item(58, "1", serial="2")])]
    )
    assert codes(serials) == [(ReasonCode.STOCK_CHOICE, DETAIL_SERIALS_NEEDED)]
    assert (serials.offer, serials.split) == ((), ())


def test_several_targets_offer_the_unserialized_items_at_the_place() -> None:
    stock = [item(60, "0", status=55, allocated="3"), item(57, "1", serial="X"), item(58, "4")]
    (pick,) = resolve_lines([LineStock(COUNT, Decimal(3), stock)])
    assert codes(pick) == [(ReasonCode.STOCK_CHOICE, DETAIL_SEVERAL_TARGETS)]
    assert [offered.pk for offered in pick.offer] == [58, 60]  # status and allocation do not matter for a target


def test_chosen_items_are_picked_from_without_asking_about_their_differences() -> None:
    stock = [item(57, "5", batch="A"), item(58, "5", batch="B")]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(3), stock, chosen=(58,))])
    assert (pick.takes, pick.decided, pick.offer) == (takes((58, "3")), True, ())
    (split,) = resolve_lines([LineStock(TRANSFER, Decimal(8), stock, chosen=(57, 58))])
    assert (split.takes, split.decided) == (takes((57, "5"), (58, "3")), True)


def test_chosen_items_that_no_longer_cover_the_line_ask_again_with_the_stock_as_it_is() -> None:
    stock = [item(57, "2", batch="A"), item(58, "5", batch="B"), item(59, "4", batch="B")]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(3), stock, chosen=(57,))])
    assert (pick.takes, codes(pick)) == ((), [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SHORT)])
    assert [offered.pk for offered in pick.offer] == [58, 59]
    (gone,) = resolve_lines([LineStock(TRANSFER, Decimal(3), stock, chosen=(41,))])
    assert codes(gone) == [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SHORT)]


def test_chosen_items_an_earlier_line_takes_no_longer_cover_the_line() -> None:
    stock = [item(57, "5", batch="A"), item(58, "5", batch="B")]
    first, second = resolve_lines(
        [LineStock(TRANSFER, Decimal(4), stock, chosen=(57,)), LineStock(TRANSFER, Decimal(3), stock, chosen=(57,))]
    )
    assert first.takes == takes((57, "4"))
    assert (second.takes, codes(second)) == ((), [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SHORT)])
    assert [offered.pk for offered in second.offer] == [58]


def test_none_of_these_leaves_the_line_without_a_pick_and_offers_nothing() -> None:
    stock = [item(57, "5", batch="A"), item(58, "5", batch="B")]
    for operation in (TRANSFER, COUNT):
        (pick,) = resolve_lines([LineStock(operation, Decimal(3), stock, chosen=())])
        assert (pick.takes, codes(pick)) == ((), [(ReasonCode.STOCK_CHOICE, DETAIL_NONE_CHOSEN)])
        assert (pick.offer, pick.split) == ((), ())


def test_a_chosen_target_is_acted_on_whatever_it_holds_while_it_is_at_the_place_and_unserialized() -> None:
    stock = [item(57, "0", status=55), item(58, "4")]
    (pick,) = resolve_lines([LineStock(COUNT, Decimal(3), stock, chosen=(57,))])
    assert (pick.takes, pick.decided) == (takes((57, "3")), True)
    (gone,) = resolve_lines([LineStock(ADD, Decimal(3), stock, chosen=(41,))])
    assert codes(gone) == [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_GONE)]
    assert [offered.pk for offered in gone.offer] == [57, 58]
    serialized = [item(57, "1", serial="X"), item(58, "4")]
    (now,) = resolve_lines([LineStock(ADD, Decimal(3), serialized, chosen=(57,))])
    assert codes(now) == [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SERIALIZED)]
    assert [offered.pk for offered in now.offer] == [58]


def test_several_items_named_for_a_target_are_not_an_answer() -> None:
    stock = [item(57, "1"), item(58, "4")]
    (pick,) = resolve_lines([LineStock(COUNT, Decimal(3), stock, chosen=(57, 58))])
    assert codes(pick) == [(ReasonCode.STOCK_CHOICE, DETAIL_SEVERAL_TARGETS)]


def test_a_chosen_item_does_not_settle_partly_serialized_stock() -> None:
    stock = [item(41, "5", batch="A"), item(60, "1", serial="S1")]
    for chosen in ((41,), (57,)):
        (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(1), stock, chosen=chosen)])
        assert (pick.takes, codes(pick)) == ((), [(ReasonCode.STOCK_CHOICE, DETAIL_MIXED)])
        assert (pick.offer, pick.split) == ((), ())


def test_the_offer_leaves_out_serialized_allocated_and_not_ok_items() -> None:
    stock = [item(57, "5", batch="A"), item(58, "5", batch="B"), item(59, "5", allocated="1"), item(60, "5", status=55)]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(3), stock, chosen=(41,))])
    assert [offered.pk for offered in pick.offer] == [57, 58]


def test_an_add_is_not_offered_an_item_an_earlier_line_takes_in_full() -> None:
    stock = [item(41, "2"), item(42, "4"), item(43, "1")]
    _, add = resolve_lines([LineStock(TRANSFER, Decimal(2), [item(41, "2")]), LineStock(ADD, Decimal(3), stock)])
    assert codes(add) == [(ReasonCode.STOCK_CHOICE, DETAIL_SEVERAL_TARGETS)]
    assert [offered.pk for offered in add.offer] == [42, 43]
    _, count = resolve_lines([LineStock(TRANSFER, Decimal(2), [item(41, "2")]), LineStock(COUNT, Decimal(3), stock)])
    assert [offered.pk for offered in count.offer] == [41, 42, 43]


def test_serialized_stock_is_never_offered_as_a_choice() -> None:
    stock = [item(60, "1", serial="S1"), item(61, "1", serial="S2")]
    (pick,) = resolve_lines([LineStock(TRANSFER, Decimal(1), stock, chosen=(41,))])
    assert codes(pick) == [(ReasonCode.STOCK_CHOICE, DETAIL_CHOSEN_SHORT)]
    assert (pick.offer, pick.split) == ((), ())  # so the question is "read the stock again"


def test_a_remove_of_serialized_stock_shows_its_items_and_waits_for_a_person() -> None:
    """InvenTree answers a remove on a serialized item with success and changes nothing: never a write."""
    stock = [item(1, "1", serial="SN-1"), item(2, "1", serial="SN-2")]
    for pick in (
        resolve_line(Operation.REMOVE, Decimal(1), stock, ("SN-2",)),
        resolve_lines([LineStock(Operation.REMOVE, Decimal(1), stock, ("SN-2",))])[0],
        resolve_lines([LineStock(Operation.REMOVE, Decimal(2), stock)])[0],
    ):
        assert pick.takes
        assert (ReasonCode.STOCK_CHOICE, DETAIL_SERIALIZED_REMOVE) in codes(pick)
        assert not pick.decided
    moved = resolve_lines([LineStock(Operation.TRANSFER, Decimal(1), stock, ("SN-2",))])[0]
    assert moved.decided  # a transfer moves serialized stock whole
    plain = resolve_lines([LineStock(Operation.REMOVE, Decimal(1), [item(3, "5")])])[0]
    assert plain.decided
