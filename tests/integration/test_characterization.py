"""Characterization tests: server behavior the design relies on (design section 11).

Each test turns one claim from docs/research/inventree.md into a fact for the
pinned version it runs against. When one fails, the design is what changes.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest

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

    With the server-wide setting switched on: `test_char_transfer_merge_setting_on`.
    """
    waiting = scratch.stock(4, location=scratch.other)
    for extra in ({}, {"merge": False}):
        moving = scratch.stock(3)
        line = {"pk": moving["pk"], "quantity": 3, **extra}
        assert api.post("/api/stock/transfer/", {"items": [line], "location": scratch.other}).status_code == 201
        assert float(scratch.item(waiting["pk"])["quantity"]) == 4
        assert scratch.item(moving["pk"])["location"] == scratch.other
    assert sorted(float(row["quantity"]) for row in api.items(scratch.part, scratch.other)) == [3, 3, 4]


MERGE_SETTING = "/api/settings/global/STOCK_MERGE_ON_TRANSFER/"
MERGE_FROM = 530
"""Measured: API 511 (1.4.3) has no merge setting and ignores `merge` on a transfer line; API 530 (1.5.6) has both.

The research puts the `merge` field at API 512 (1.5.0). Versions 512 to 529 are not measured, so 530 is the first
measured version, not the true boundary.
"""


def test_char_transfer_merge_setting_on(api: RawApi, scratch: Scratch) -> None:
    """The server-wide "merge stock on transfer" setting does not change a transfer that omits `merge`.

    Only an explicit `merge: true` merges (API 530): the moving item is deleted, and its history entry, with the
    notes, is written on the destination item. The connector sends `merge: false` on every line (design section 5).
    """
    if api.get("/api/")["apiVersion"] < MERGE_FROM:
        assert api.status(MERGE_SETTING) == 404
        waiting = scratch.stock(4, location=scratch.other)
        moving = scratch.stock(3)
        line = {"pk": moving["pk"], "quantity": 3, "merge": True}
        assert api.post("/api/stock/transfer/", {"items": [line], "location": scratch.other}).status_code == 201
        assert float(scratch.item(waiting["pk"])["quantity"]) == 4  # merge is ignored
        return
    switched = api.http.patch(MERGE_SETTING, json={"value": True})
    switched.raise_for_status()
    try:
        waiting = scratch.stock(4, location=scratch.other)
        for extra in ({}, {"merge": False}):
            moving = scratch.stock(3)
            line = {"pk": moving["pk"], "quantity": 3, **extra}
            assert api.post("/api/stock/transfer/", {"items": [line], "location": scratch.other}).status_code == 201
            assert float(scratch.item(waiting["pk"])["quantity"]) == 4
            assert scratch.item(moving["pk"])["location"] == scratch.other
        moving = scratch.stock(3)
        marker = f"merge-{uuid.uuid4().hex}"
        line = {"pk": moving["pk"], "quantity": 3, "merge": True}
        body = {"items": [line], "location": scratch.other, "notes": marker}
        assert api.post("/api/stock/transfer/", body).status_code == 201
        assert api.status(f"/api/stock/{moving['pk']}/") == 404
        assert float(scratch.item(waiting["pk"])["quantity"]) == 7
        assert [row["item"] for row in search_rows(api, marker)] == [waiting["pk"]]
    finally:
        api.http.patch(MERGE_SETTING, json={"value": False}).raise_for_status()


DB = os.environ.get("STOCKTHREAD_IT_DB", "sqlite")
"""The database behind the test server: `sqlite` or `postgres` (set by start-inventree.sh)."""
NOTES_LIMIT = 512
"""Measured: history notes are `varchar(512)`. PostgreSQL enforces it, SQLite does not."""
OVERLONG_COMMITS_FROM = 530
"""Measured on PostgreSQL: API 530 (1.5.6) commits the stock change of a request whose notes are too long, and
answers 500. API 511 (1.4.3) changes nothing."""
LOST_UPDATE_BEFORE = 530
"""Measured on PostgreSQL: API 511 (1.4.3) lets two concurrent splits of one item both succeed, creating stock.
API 530 (1.5.6) serializes them."""


def test_char_notes_length(api: RawApi, scratch: Scratch) -> None:
    """Notes up to 512 characters are stored whole. Longer notes: SQLite stores them, PostgreSQL answers 500.

    The connector keeps its notes under 200 characters (design section 7).
    """
    item = scratch.stock(5)
    prefix = f"long-{uuid.uuid4().hex}-"
    notes = prefix + "n" * (NOTES_LIMIT - len(prefix))
    body = {"items": [{"pk": item["pk"], "quantity": 1}], "location": scratch.other, "notes": notes}
    assert api.post("/api/stock/transfer/", body).status_code == 201
    assert set(search(api, prefix)) == {notes}  # a partial transfer writes two entries
    if DB == "sqlite":
        prefix = f"longer-{uuid.uuid4().hex}-"
        notes = prefix + "n" * 20_000
        body = {"items": [{"pk": item["pk"], "quantity": 1}], "location": scratch.other, "notes": notes}
        assert api.post("/api/stock/transfer/", body).status_code == 201
        assert set(search(api, prefix)) == {notes}  # a partial transfer writes two entries


@pytest.mark.skipif(DB != "postgres", reason="SQLite does not enforce the notes length")
@pytest.mark.parametrize("path", ["/api/stock/transfer/", "/api/stock/add/", "/api/stock/remove/"])
def test_char_overlong_notes_write_without_history(api: RawApi, scratch: Scratch, path: str) -> None:
    """Notes over the limit give a 500. On API 530 (1.5.6) the stock change is still committed, without the history
    entry that would carry the marker. On API 511 (1.4.3) nothing changes.

    This is why a 5xx is an unknown outcome, and why a marker that is not found proves nothing (design section 7).
    """
    item = scratch.stock(10)
    marker = f"overlong-{uuid.uuid4().hex}-"
    body: dict[str, Any] = {"items": [{"pk": item["pk"], "quantity": 1}], "notes": marker + "n" * NOTES_LIMIT}
    if path.endswith("transfer/"):
        body["location"] = scratch.other
    history_before = len(api.get("/api/stock/track/", item=item["pk"]))
    response = api.post(path, body)
    assert response.status_code == 500
    assert search_rows(api, marker) == []
    assert len(api.get("/api/stock/track/", item=item["pk"])) == history_before
    committed = {"/api/stock/transfer/": 9, "/api/stock/add/": 11, "/api/stock/remove/": 9}[path]
    expected = committed if api.get("/api/")["apiVersion"] >= OVERLONG_COMMITS_FROM else 10
    assert float(scratch.item(item["pk"])["quantity"]) == expected


def test_char_concurrent_transfers_same_item(api: RawApi, scratch: Scratch) -> None:
    """Two simultaneous transfers of 4 from an item of 5 never move more than the item held, but they do not
    always move what they asked for.

    - SQLite (one server process): when the requests collide, one answers 201 and the other a 5xx that wrote
      nothing. When the server handles them one after the other, the outcome is that of PostgreSQL 1.5.6.
    - PostgreSQL, API 530 (1.5.6): both answer 201. The second moves the 1 that is left, because a transfer of
      more than an item holds moves the whole item (`test_char_over_quantity_transfer`). Only the read-back of
      the history deltas can see that (design section 7).
    - PostgreSQL, API 511 (1.4.3): both answer 201 and both split 4 off the item, which keeps 1: stock grows from
      5 to 9 (a lost update). Measured 3 times out of 3; the serialized outcome of 1.5.6 is accepted too.
    """
    item = scratch.stock(5)
    destinations = {"first": scratch.child, "second": scratch.other}
    markers = {name: f"race-{name}-{uuid.uuid4().hex}" for name in destinations}
    statuses: dict[str, int] = {}
    barrier = threading.Barrier(len(destinations))

    def transfer(name: str) -> None:
        body = {"items": [{"pk": item["pk"], "quantity": 4}], "location": destinations[name], "notes": markers[name]}
        with httpx.Client(base_url=api.http.base_url, headers=api.http.headers, timeout=60) as client:
            barrier.wait()
            statuses[name] = client.post("/api/stock/transfer/", json=body).status_code

    threads = [threading.Thread(target=transfer, args=(name,)) for name in destinations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    def held(location: int) -> float:
        return sum(float(row["quantity"]) for row in api.items(scratch.part, location))

    moved = {name: held(location) for name, location in destinations.items()}
    left = held(scratch.parent)
    for name, status in statuses.items():
        assert (search(api, markers[name]) != []) == (status == 201), statuses
        if status == 201:  # every write leaves an entry on the source item, which read-back relies on (section 7)
            assert item["pk"] in {row["item"] for row in search_rows(api, markers[name])}
    lost_update = DB == "postgres" and api.get("/api/")["apiVersion"] < LOST_UPDATE_BEFORE
    if lost_update and sorted(moved.values()) == [4, 4]:
        assert left == 1
        assert sorted(statuses.values()) == [201, 201], statuses
        return
    assert left + sum(moved.values()) == 5
    for name, status in statuses.items():
        if status == 201:
            assert 0 < moved[name] <= 4, moved
        else:
            assert status >= 500, statuses
            assert moved[name] == 0
    if DB == "postgres" or sorted(statuses.values()) == [201, 201]:
        assert sorted(statuses.values()) == [201, 201], statuses
        assert sorted(moved.values()) == [1, 4]
    else:
        assert sorted(moved.values()) == [0, 4]
