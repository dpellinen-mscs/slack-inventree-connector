# SPDX-License-Identifier: MIT
"""Characterization tests: the limits a request meets on the server (design section 11, issue #220).

Quantity precision, structural locations, a malformed history filter, and the web links in receipts.
When one fails, the design is what changes.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from stockthread.core.write_request import QUANTITY_DIGITS, QUANTITY_PLACES

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

# The largest quantity the builder sends, and the smallest step it can express.
LARGEST = Decimal(10) ** (QUANTITY_DIGITS - QUANTITY_PLACES) - 1
STEP = Decimal(1).scaleb(-QUANTITY_PLACES)
DB = os.environ.get("STOCKTHREAD_IT_DB", "sqlite")
OVERFLOW_STORED_FROM = 530
"""From this API version, SQLite stores an item total past 15 digits (and then cannot read the item)."""
LINE_ERRORS_KEYED_FROM = 561
"""Measured: API 561 (1.6.3) keys a 400's line errors by line index (`{"0": {...}}`); 530 and 511 list them."""


def _add(api: RawApi, pk: int, quantity: Decimal) -> tuple[int, object]:
    response = api.post("/api/stock/add/", {"items": [{"pk": pk, "quantity": str(quantity)}], "notes": "limits"})
    return response.status_code, response.json()


def test_char_quantity_precision_is_refused_not_rounded(api: RawApi, scratch: Scratch) -> None:
    """A line with more than 5 decimal places, or more than 10 digits before the point, is a 400 on that line.

    Nothing is rounded and nothing changes. 5 places and 10 digits are accepted.
    """
    item = scratch.stock(1)
    keyed = api.get("/api/")["apiVersion"] >= LINE_ERRORS_KEYED_FROM
    error = {"quantity": ["Ensure that there are no more than 5 decimal places."]}
    places = {"items": {"0": error} if keyed else [error]}
    assert _add(api, item["pk"], STEP / 10) == (400, places)
    status, body = _add(api, item["pk"], LARGEST + 1)
    assert status == 400
    assert isinstance(body, dict)
    assert list(body) == ["items"]
    assert "10 digits before the decimal point" in body["items"]["0" if keyed else 0]["quantity"][0]
    assert float(scratch.item(item["pk"])["quantity"]) == 1
    for path, line in (("/api/stock/count/", "1.000001"), ("/api/stock/remove/", "0.000001")):
        body = {"items": [{"pk": item["pk"], "quantity": line}], "notes": "limits"}
        response = api.post(path, body)
        assert (response.status_code, response.json()) == (400, places), path
    assert _add(api, item["pk"], STEP)[0] == 201
    assert Decimal(str(scratch.item(item["pk"])["quantity"])) == 1 + STEP


def test_char_an_add_past_the_stored_digits_breaks_the_item(api: RawApi, scratch: Scratch) -> None:
    """Each line is checked alone, not the total: an add that takes an item past 15 digits passes the checks.

    - SQLite, API 530 (1.5.6): it answers 201, and after it every read of the item answers 500, so the item can no
      longer be read back or moved.
    - PostgreSQL (numeric overflow) and SQLite, API 511 (1.4.3): it answers 500 and writes nothing, no history entry
      either, which the connector cannot tell from a write that ran.

    The item lives in a location of its own, so no other test lists it.
    """
    alone = int(api.create("/api/stock/location/", {"name": f"overflow-{uuid.uuid4().hex[:8]}"})["pk"])
    item = scratch.stock(1, location=alone)
    assert _add(api, item["pk"], STEP)[0] == 201
    history = len(api.get("/api/stock/track/", item=item["pk"]))
    marker = f"overflow-{uuid.uuid4().hex}"
    body = {"items": [{"pk": item["pk"], "quantity": str(LARGEST)}], "notes": marker}
    response = api.post("/api/stock/add/", body)  # the total, 10000000000.00001, has 16 digits
    if DB == "sqlite" and api.get("/api/")["apiVersion"] >= OVERFLOW_STORED_FROM:
        assert response.status_code == 201
        assert api.status(f"/api/stock/{item['pk']}/") == 500
        return
    assert response.status_code == 500
    assert Decimal(str(scratch.item(item["pk"])["quantity"])) == 1 + STEP
    assert len(api.get("/api/stock/track/", item=item["pk"])) == history
    assert api.get("/api/stock/track/", search=marker) == []


def test_char_structural_locations_hold_no_stock(api: RawApi, scratch: Scratch) -> None:
    """A transfer into a structural location is a 400 on `location`, whole or partial, and moves nothing."""
    structural = api.create("/api/stock/location/", {"name": f"structural-{uuid.uuid4().hex[:8]}", "structural": True})[
        "pk"
    ]
    item = scratch.stock(5)
    for quantity in (1, 5):
        body = {"items": [{"pk": item["pk"], "quantity": quantity}], "location": structural, "notes": "s"}
        response = api.post("/api/stock/transfer/", body)
        assert response.status_code == 400
        assert list(response.json()) == ["location"]
    after = scratch.item(item["pk"])
    assert (float(after["quantity"]), after["location"]) == (5, scratch.parent)
    assert api.get("/api/stock/", location=structural) == []
    created = api.post("/api/stock/", {"part": scratch.part, "location": structural, "quantity": 1})
    assert created.status_code == 400
    assert list(created.json()) == ["location"]


def test_char_history_filter_refuses_a_malformed_id(api: RawApi) -> None:
    """A history query filtered by an id that is not a stock item (malformed, negative) is a 400 on `item`."""
    for value in ("abc", "-1", "1.5"):
        response = api.http.get("/api/stock/track/", params={"item": value})
        assert response.status_code == 400, value
        assert list(response.json()) == ["item"], value


def test_char_web_links_are_served_by_the_ui(api: RawApi, scratch: Scratch) -> None:
    """The receipt links `/web/stock/location/<pk>` and `/web/stock/item/<pk>` answer the web UI's page.

    So does any other path under `/web/`: the UI routes in the browser, so the server cannot confirm a route.
    """
    item = scratch.stock(1)
    tag = uuid.uuid4().hex
    for path in (f"/web/stock/location/{scratch.parent}", f"/web/stock/item/{item['pk']}", f"/web/no-such-{tag}"):
        response = api.http.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert "<title>InvenTree</title>" in response.text, path
