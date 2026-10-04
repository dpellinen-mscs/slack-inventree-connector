"""The stock reader the worker uses for source selection (`run.inventree_stock`), against a real server."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stockthread.config import Settings, parse_settings
from stockthread.core.model import Operation, ReasonCode
from stockthread.core.resolve import SourcePick, StockItem, Take, resolve_line
from stockthread.inventree import InvenTreeError
from stockthread.run import inventree_stock

if TYPE_CHECKING:
    from conftest import RawApi, Scratch


def settings(token: str | None = None) -> Settings:
    if token is None:
        token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    data = {
        "slack": {"workspace_id": "T0ACME001", "app_id": "A0ACME001", "channels": ["C0ACME001"]},
        "inventree": {"url": os.environ["STOCKTHREAD_IT_URL"]},
    }
    env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
    return parse_settings(data, env)


def test_reads_each_pair_as_checked_stock_items(api: RawApi, scratch: Scratch) -> None:
    part = api.create("/api/part/", {"name": f"Stock read {uuid.uuid4().hex[:8]}", "description": "x"})["pk"]
    here = api.create("/api/stock/", {"part": part, "location": scratch.parent, "quantity": 5, "batch": "B-1"})
    there = api.create("/api/stock/", {"part": part, "location": scratch.other, "quantity": 2})
    api.create("/api/stock/", {"part": part, "location": scratch.child, "quantity": 7})  # a sub-location: left out
    read = inventree_stock(settings())
    empty = api.create("/api/part/", {"name": f"No stock {uuid.uuid4().hex[:8]}", "description": "x"})["pk"]
    found = read([(part, scratch.parent), (part, scratch.other), (empty, scratch.parent)])
    assert found[(part, scratch.parent)] == [StockItem(here["pk"], Decimal(5), 10, Decimal(0), None, "B-1", None)]
    assert [item.pk for item in found[(part, scratch.other)]] == [there["pk"]]
    assert found[(empty, scratch.parent)] == []


def test_a_refused_token_is_an_inventree_error(scratch: Scratch) -> None:
    read = inventree_stock(settings("not-a-token"))
    with pytest.raises(InvenTreeError, match="refused the token"):
        read([(scratch.part, scratch.parent)])


def test_char_depleted_items_are_read(api: RawApi, scratch: Scratch) -> None:
    """An item emptied by a remove (and kept: delete_on_deplete off) is left out by InvenTree's `in_stock` filter and
    given by its `depleted` filter. The reader reads both, so a receive or a count finds the empty item as its
    target, and an empty item next to a full one is a choice, not the full one (audit #107)."""
    emptied = scratch.stock(2, delete_on_deplete=False)
    response = api.post("/api/stock/remove/", {"items": [{"pk": emptied["pk"], "quantity": 2}], "notes": "r"})
    assert response.status_code == 201, response.text[:500]
    assert api.get("/api/stock/", part=scratch.part, location=scratch.parent, in_stock="true") == []
    read = inventree_stock(settings())
    (only,) = read([(scratch.part, scratch.parent)])[(scratch.part, scratch.parent)]
    assert (only.pk, only.quantity) == (emptied["pk"], Decimal(0))
    for operation in (Operation.ADD, Operation.COUNT):
        assert resolve_line(operation, Decimal(3), [only]) == SourcePick((Take(emptied["pk"], Decimal(3)),))
    full = scratch.stock(5)
    both = read([(scratch.part, scratch.parent)])[(scratch.part, scratch.parent)]
    assert [item.pk for item in both] == sorted([emptied["pk"], full["pk"]])
    assert [reason.code for reason in resolve_line(Operation.ADD, Decimal(3), both).reasons] == [
        ReasonCode.STOCK_CHOICE
    ]
    assert resolve_line(Operation.REMOVE, Decimal(1), both).takes == (
        Take(full["pk"], Decimal(1)),
    )  # never the empty one
