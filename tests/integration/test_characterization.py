"""Characterization tests: server behavior the design relies on (design section 11).

Each test turns one claim from docs/research/inventree.md into a fact for the
pinned version it runs against. When one fails, the design is what changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import RawApi, Scratch


def test_char_over_quantity_transfer(api: RawApi, scratch: Scratch) -> None:
    """A transfer of more than an item holds is not rejected: the whole item moves."""
    item = scratch.stock(5)
    response = api.post(
        "/api/stock/transfer/", {"items": [{"pk": item["pk"], "quantity": 8}], "location": scratch.other, "notes": "t"}
    )
    assert response.status_code == 201, response.text[:500]
    moved = scratch.item(item["pk"])
    assert moved["location"] == scratch.other
    assert float(moved["quantity"]) == 5
    assert api.items(scratch.part, scratch.parent) == []


def test_char_over_quantity_remove(api: RawApi, scratch: Scratch) -> None:
    """A remove of more than an item holds stops at zero and succeeds."""
    item = scratch.stock(5, delete_on_deplete=False)
    response = api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 8}], "notes": "r"})
    assert response.status_code == 201, response.text[:500]
    assert float(scratch.item(item["pk"])["quantity"]) == 0


def test_char_deplete_deletes_history(api: RawApi, scratch: Scratch) -> None:
    """Removing the last unit of an item marked delete_on_deplete deletes it, and its history with it."""
    item = scratch.stock(3, delete_on_deplete=True)
    entries = [row["pk"] for row in api.get("/api/stock/track/", item=item["pk"])]
    assert entries  # creating the item wrote history
    response = api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 3}], "notes": "d"})
    assert response.status_code == 201, response.text[:500]
    assert api.status(f"/api/stock/{item['pk']}/") == 404
    for entry in entries:
        assert api.status(f"/api/stock/track/{entry}/") == 404
    # Recovery cannot ask for the history of a deleted item: the filter refuses an unknown item.
    assert api.status(f"/api/stock/track/?item={item['pk']}") == 400


def test_char_partial_transfer_split(api: RawApi, scratch: Scratch) -> None:
    """A partial transfer keeps the source id with less stock, and creates a new item at the destination."""
    item = scratch.stock(10)
    response = api.post(
        "/api/stock/transfer/", {"items": [{"pk": item["pk"], "quantity": 3}], "location": scratch.other, "notes": "s"}
    )
    assert response.status_code == 201, response.text[:500]
    source = scratch.item(item["pk"])
    assert source["location"] == scratch.parent
    assert float(source["quantity"]) == 7
    [new] = api.items(scratch.part, scratch.other)
    assert new["pk"] != item["pk"]
    assert float(new["quantity"]) == 3


def test_char_multi_item_request_is_atomic(api: RawApi, scratch: Scratch) -> None:
    """One bad line fails the whole request, and nothing moves."""
    good = scratch.stock(5)
    bad = scratch.stock(5)
    lines = [{"pk": good["pk"], "quantity": 2}, {"pk": bad["pk"], "quantity": 0}]
    response = api.post("/api/stock/transfer/", {"items": lines, "location": scratch.other, "notes": "a"})
    assert response.status_code == 400, response.text[:500]
    assert api.items(scratch.part, scratch.other) == []
    assert float(scratch.item(good["pk"])["quantity"]) == 5


def test_char_stock_list_cascade_false(api: RawApi, scratch: Scratch) -> None:
    """A stock list with cascade=false leaves out sub-locations; cascade=true includes them."""
    scratch.stock(6, location=scratch.child)
    assert api.items(scratch.part, scratch.parent, cascade=False) == []
    assert len(api.items(scratch.part, scratch.parent, cascade=True)) == 1


def test_char_error_body_format(api: RawApi, scratch: Scratch) -> None:
    """A 4xx answer from InvenTree is JSON: field errors as an object of lists, a 404 with `detail`."""
    item = scratch.stock(1)
    response = api.post("/api/stock/transfer/", {"items": [{"pk": item["pk"], "quantity": 1}], "notes": "e"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert isinstance(body, dict)
    assert isinstance(body.get("location"), list)

    missing = api.http.get("/api/stock/999999999/")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")
    assert "detail" in missing.json()
