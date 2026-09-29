"""Characterization tests: server behavior the design relies on (design section 11).

Each test turns one claim from docs/research/inventree.md into a fact for the
pinned version it runs against. When one fails, the design is what changes.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

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


DEPLETING_MARKER_FOUND_FROM = 530
"""Measured: API 511 (1.4.3) does not find the marker of a removal that deleted the item, API 530 (1.5.6) does."""


def search_rows(api: RawApi, marker: str) -> list[dict[str, Any]]:
    found = api.get("/api/stock/track/", search=marker)
    return list(found["results"] if isinstance(found, dict) else found)


def search(api: RawApi, marker: str) -> list[str]:
    return [row["notes"] for row in search_rows(api, marker)]


def test_char_deplete_deletes_item_keeps_history(api: RawApi, scratch: Scratch) -> None:
    """Removing the last unit of an item marked delete_on_deplete deletes the item, but not its history.

    The research expected the history to go with the item. It does not. Whether the marker of the
    removal that deleted the item can be found depends on the version, so on older versions recovery
    cannot prove that such a write happened (design section 7: a marker not found proves nothing).
    """
    control = scratch.stock(3)
    control_marker = f"control-{uuid.uuid4().hex}"
    response = api.post(
        "/api/stock/remove/", {"items": [{"pk": control["pk"], "quantity": 1}], "notes": control_marker}
    )
    assert response.status_code == 201, response.text[:500]
    assert search(api, control_marker) == [control_marker]  # search finds the notes of a removal

    item = scratch.stock(3, delete_on_deplete=True)
    entries = [row["pk"] for row in api.get("/api/stock/track/", item=item["pk"])]
    assert entries  # creating the item wrote history
    marker = f"deplete-{uuid.uuid4().hex}"
    response = api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 3}], "notes": marker})
    assert response.status_code == 201, response.text[:500]
    assert api.status(f"/api/stock/{item['pk']}/") == 404
    for entry in entries:
        assert api.status(f"/api/stock/track/{entry}/") == 200
    expected = [marker] if api.get("/api/")["apiVersion"] >= DEPLETING_MARKER_FOUND_FROM else []
    assert search(api, marker) == expected
    # A history query filtered by the deleted item is refused, not answered with an empty list.
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


def test_char_marker_on_every_entry_type(api: RawApi, scratch: Scratch) -> None:
    """The notes of a request reach every history entry it writes: split (both sides), move, add, remove."""
    split = scratch.stock(10)
    marker = f"split-{uuid.uuid4().hex}"
    body = {"items": [{"pk": split["pk"], "quantity": 4}], "location": scratch.other, "notes": marker}
    assert api.post("/api/stock/transfer/", body).status_code == 201
    [new] = api.items(scratch.part, scratch.other)
    rows = search_rows(api, marker)
    assert sorted((row["item"], row["tracking_type"]) for row in rows) == sorted([(split["pk"], 42), (new["pk"], 40)])

    whole = scratch.stock(2)
    steps: list[tuple[str, dict[str, Any], int]] = [
        ("/api/stock/transfer/", {"items": [{"pk": whole["pk"], "quantity": 2}], "location": scratch.child}, 20),
        ("/api/stock/add/", {"items": [{"pk": whole["pk"], "quantity": 3}]}, 11),
        ("/api/stock/remove/", {"items": [{"pk": whole["pk"], "quantity": 1}]}, 12),
    ]
    for path, request, code in steps:
        marker = f"entry-{code}-{uuid.uuid4().hex}"
        assert api.post(path, {**request, "notes": marker}).status_code == 201
        assert [(row["item"], row["tracking_type"]) for row in search_rows(api, marker)] == [(whole["pk"], code)]


def test_char_marker_visible_after_response(api: RawApi, scratch: Scratch) -> None:
    """History is readable as soon as the server has answered a write (single server, no worker)."""
    item = scratch.stock(10)  # enough that each of the five transfers is a split
    for _ in range(5):
        marker = f"visible-{uuid.uuid4().hex}"
        body = {"items": [{"pk": item["pk"], "quantity": 1}], "location": scratch.other, "notes": marker}
        assert api.post("/api/stock/transfer/", body).status_code == 201
        assert search(api, marker) == [marker, marker]  # a partial transfer writes both sides of the split


def test_char_marker_search_is_substring(api: RawApi, scratch: Scratch) -> None:
    """The history search matches substrings of the notes, and it is paginated with limit and offset."""
    item = scratch.stock(10)
    base = f"st-{uuid.uuid4().hex}"
    notes = [f"[{base}a] slack C0FIELD/1727555555.000100 by U0SAM0001", f"[{base}b] slack C0FIELD/1727555555.000200"]
    for text in notes:
        assert (
            api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 1}], "notes": text}).status_code
            == 201
        )
    assert search(api, f"{base}a") == [notes[0]]
    assert sorted(search(api, base)) == sorted(notes)  # a prefix finds both: compare the whole token
    pages = [api.get("/api/stock/track/", search=base, limit=1, offset=offset) for offset in (0, 1)]
    assert [page["count"] for page in pages] == [2, 2]
    assert sorted(row["notes"] for page in pages for row in page["results"]) == sorted(notes)  # no overlap, no gap


def test_char_serialized_quantity_noop(api: RawApi, scratch: Scratch) -> None:
    """Add and remove on a serialized item change nothing and still return success, and they write no history.

    Measured so the marker rule of design section 7 holds for serialized items too: a found marker means a change.
    """
    body = {"part": scratch.trackable_part, "location": scratch.parent, "quantity": 1}
    item = api.create("/api/stock/", {**body, "serial_numbers": f"SN-{uuid.uuid4().hex[:8]}"})
    assert float(item["quantity"]) == 1
    for path in ("/api/stock/add/", "/api/stock/remove/"):
        marker = f"serialized-{uuid.uuid4().hex}"
        response = api.post(path, {"items": [{"pk": item["pk"], "quantity": 1}], "notes": marker})
        assert response.status_code == 201, response.text[:500]
        assert float(scratch.item(item["pk"])["quantity"]) == 1
        assert search_rows(api, marker) == []


def test_char_transfer_does_not_merge(api: RawApi, scratch: Scratch) -> None:
    """A transfer to a location that holds the same part leaves separate items, with and without merge=false.

    The case with the server-wide "merge stock on transfer" setting switched on is not covered yet.
    """
    waiting = scratch.stock(4, location=scratch.other)
    for extra in ({}, {"merge": False}):
        moving = scratch.stock(3)
        line = {"pk": moving["pk"], "quantity": 3, **extra}
        assert api.post("/api/stock/transfer/", {"items": [line], "location": scratch.other}).status_code == 201
        assert float(scratch.item(waiting["pk"])["quantity"]) == 4
        assert scratch.item(moving["pk"])["location"] == scratch.other
    assert sorted(float(row["quantity"]) for row in api.items(scratch.part, scratch.other)) == [3, 3, 4]
