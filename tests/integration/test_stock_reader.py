"""The stock reader the worker uses for source selection (`run.inventree_stock`), against a real server."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stockthread.config import Settings, parse_settings
from stockthread.core.resolve import StockItem
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
