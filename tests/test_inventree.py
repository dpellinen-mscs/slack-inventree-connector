# SPDX-License-Identifier: MIT
import itertools
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker
from stockthread.inventree import (
    MAX_API_VERSION,
    MAX_LIST_READS,
    MIN_API_VERSION,
    PAGE_SIZE,
    InvenTreeError,
    InvenTreeReader,
    MarkedEntry,
    ReadOnlyViolation,
    ServerInfo,
    check_version,
    in_stock_but_empty,
)
from stockthread.writer import InvenTreeWriter

pytestmark = pytest.mark.anyio

TOKEN = "inv-synthetic-token-0123456789"
SETTINGS = InvenTreeSettings(url="https://tools.acme-fiber.example/inventree/")
ROOT = {"server": "InvenTree", "version": "1.5.6", "apiVersion": 530, "instance": "Acme Fiber"}

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Recorder:
    """A mock transport that records every request it receives."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def reader(handler: Handler, sleeps: list[float] | None = None) -> tuple[InvenTreeReader, Recorder]:
    recorder = Recorder(handler)

    async def sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)

    client = InvenTreeReader(SETTINGS, SecretStr(TOKEN), transport=httpx.MockTransport(recorder), sleep=sleep)
    return client, recorder


def respond(status: int = 200, body: object = None, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx.Response(status, json=body, headers=headers)


async def test_server_info_reads_the_api_root_with_the_token() -> None:
    client, recorder = reader(respond(body=ROOT))
    async with client:
        assert await client.server_info() == ServerInfo(version="1.5.6", api_version=530)
    [request] = recorder.requests
    assert str(request.url) == "https://tools.acme-fiber.example/inventree/api/"
    assert request.headers["Authorization"] == f"Token {TOKEN}"


@pytest.mark.parametrize(
    "body",
    [
        {**ROOT, "server": "Acme Fiber ERP"},
        {"detail": "not found"},
        [ROOT],
        {**ROOT, "apiVersion": "530"},
        {**ROOT, "apiVersion": True},
        {**ROOT, "version": None},
    ],
)
async def test_server_info_refuses_a_server_that_is_not_inventree(body: object) -> None:
    client, _ = reader(respond(body=body))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.server_info()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_only_get_ever_leaves_the_process(method: str) -> None:
    client, recorder = reader(respond(body={}))
    async with client:
        with pytest.raises(ReadOnlyViolation):
            await client._http.request(method, "/api/stock/transfer/", json={"items": []})
    assert recorder.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "api/part/",
        "/admin/",
        "/api/part",
        "/api/../admin/",
        "/api/%2e%2e/admin/",
        "/api/.%2E/x/",
        "/api/%2F%2E%2E%2Fx/",
        "/api/./",
        "/api/.",
        "/api/part\\..\\admin/",
        "//elsewhere.example/api/",
        "https://elsewhere.example/api/",
        "/api/part/?x=1",
        "/api/#x",
        "/api/\x00/",
        "/api/\ud800/",
    ],
)
async def test_paths_outside_the_api_are_refused(path: str) -> None:
    client, recorder = reader(respond(body={}))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.get(path)
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("status", "text"),
    [(401, "refused the token"), (403, "refused the token"), (404, "HTTP 404"), (500, "HTTP 500"), (302, "HTTP 302")],
)
async def test_errors_name_the_status_but_never_the_body_or_token(status: int, text: str) -> None:
    body = {"detail": "Sam's truck, customer 12 Elm St"}
    client, _ = reader(respond(status, body, headers={"Location": "https://elsewhere.example/"}))
    async with client:
        with pytest.raises(InvenTreeError) as info:
            await client.get("/api/part/")
    message = str(info.value)
    assert text in message
    assert "Elm" not in message
    assert TOKEN not in message


async def test_a_redirect_is_not_followed() -> None:
    client, recorder = reader(respond(302, headers={"Location": "https://elsewhere.example/api/part/"}))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.get("/api/part/")
    assert len(recorder.requests) == 1


async def test_a_network_error_names_only_its_type() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    client, _ = reader(fail)
    async with client:
        with pytest.raises(InvenTreeError) as info:
            await client.get("/api/part/")
    assert str(info.value) == "GET /api/part/ failed: ConnectError"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


async def test_a_body_that_is_not_json_is_an_error() -> None:
    client, _ = reader(lambda request: httpx.Response(200, text="<html>login</html>"))
    async with client:
        with pytest.raises(InvenTreeError, match="did not return JSON"):
            await client.get("/api/part/")


async def test_429_is_retried_with_the_server_delay_and_then_gives_up() -> None:
    sleeps: list[float] = []
    client, recorder = reader(respond(429, {}, headers={"Retry-After": "7"}), sleeps)
    async with client:
        with pytest.raises(InvenTreeError, match="HTTP 429"):
            await client.get("/api/part/")
    assert len(recorder.requests) == 3
    assert sleeps == [7.0, 7.0]


@pytest.mark.parametrize(
    ("header", "expected"), [("", [1.0]), ("soon", [1.0]), ("-5", [1.0]), ("nan", [1.0]), ("9999", [30.0])]
)
async def test_429_delay_is_sane(header: str, expected: list[float]) -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200, json=[])])
    sleeps: list[float] = []
    client, _ = reader(lambda request: next(answers), sleeps)
    async with client:
        assert await client.get("/api/part/") == []
    assert sleeps == expected


async def test_booleans_are_sent_the_way_django_filters_read_them() -> None:
    client, recorder = reader(respond(body={"count": 0, "results": []}))
    async with client:
        await client.stock_items(part=7, location=12)
    in_stock, depleted = (dict(request.url.params) for request in recorder.requests)
    place = {"part": "7", "location": "12", "cascade": "false", "limit": "100", "offset": "0"}
    assert in_stock == {**place, "in_stock": "true"}
    assert depleted == {**place, "depleted": "true"}


def stock_rows(in_stock: list[object], depleted: list[object]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        rows = depleted if request.url.params.get("depleted") == "true" else in_stock
        # The rows are of the pair the tests read (part 7, location 12) unless a row says otherwise.
        rows = [{"part": 7, "location": 12, **row} if isinstance(row, dict) else row for row in rows]
        return httpx.Response(200, json={"count": len(rows), "results": rows})

    return handler


EMPTY = {"quantity": 0, "status": 10, "customer": None, "belongs_to": None, "sales_order": None, "consumed_by": None}
EMPTY_IN_STOCK = {**EMPTY, "is_building": False}


async def test_stock_items_are_those_in_stock_and_those_depleted_by_pk() -> None:
    rows = stock_rows([{"pk": 9, "quantity": 5}, {"pk": 3, "quantity": 1}], [{"pk": 4, **EMPTY_IN_STOCK}])
    client, _ = reader(rows)
    async with client:
        assert [row["pk"] for row in await client.stock_items(part=7, location=12)] == [3, 4, 9]


@pytest.mark.parametrize("status", [10, 50, 55])
def test_an_empty_item_with_an_available_status_is_in_stock_but_empty(status: int) -> None:
    assert in_stock_but_empty({**EMPTY_IN_STOCK, "status": status})


@pytest.mark.parametrize(
    "row",
    [
        {**EMPTY_IN_STOCK, "status": 70},  # lost
        {**EMPTY_IN_STOCK, "status": 60},  # destroyed
        {**EMPTY_IN_STOCK, "status": "10"},
        {**EMPTY_IN_STOCK, "customer": 3},
        {**EMPTY_IN_STOCK, "belongs_to": 8},
        {**EMPTY_IN_STOCK, "sales_order": 2},
        {**EMPTY_IN_STOCK, "consumed_by": 5},
        {**EMPTY_IN_STOCK, "is_building": True},
        EMPTY,  # no is_building: a field missing fails
        {key: value for key, value in EMPTY_IN_STOCK.items() if key != "consumed_by"},
        {key: value for key, value in EMPTY_IN_STOCK.items() if key != "status"},
    ],
)
def test_an_empty_item_lost_sold_installed_or_unknown_is_not_read(row: dict[str, object]) -> None:
    assert not in_stock_but_empty(row)


async def test_stock_items_leave_out_a_depleted_item_that_is_not_in_stock_but_empty() -> None:
    lost = {"pk": 4, **EMPTY_IN_STOCK, "status": 70}
    client, _ = reader(stock_rows([{"pk": 9, "quantity": 5}], [lost, {"pk": 6, **EMPTY_IN_STOCK}]))
    async with client:
        assert [row["pk"] for row in await client.stock_items(part=7, location=12)] == [6, 9]


@pytest.mark.parametrize(
    ("in_stock", "depleted"),
    [
        ([{"pk": 4, "quantity": 1}], [{"pk": 4, **EMPTY_IN_STOCK}]),
        ([{"quantity": 1}], []),
        ([{"pk": "4"}], []),
        ([], [EMPTY_IN_STOCK]),
    ],
)
async def test_stock_items_refuse_a_row_read_twice_or_without_a_pk(
    in_stock: list[object], depleted: list[object]
) -> None:
    client, _ = reader(stock_rows(in_stock, depleted))
    async with client:
        with pytest.raises(InvenTreeError, match="without a pk"):
            await client.stock_items(part=7, location=12)


@pytest.mark.parametrize(
    "row",
    [
        {"pk": 4, "quantity": 1, "part": 8},
        {"pk": 4, "quantity": 1, "location": 13},
        {"pk": 4, "quantity": 1, "location": None},
        {"pk": 4, "quantity": 1, "part": "7"},
    ],
)
async def test_stock_items_refuse_an_item_of_another_part_or_location(row: dict[str, object]) -> None:
    client, _ = reader(stock_rows([{"pk": 9, "quantity": 5}, row], []))
    async with client:
        with pytest.raises(InvenTreeError, match="another part or location"):
            await client.stock_items(part=7, location=12)


async def test_stock_items_refuse_a_depleted_item_of_another_location() -> None:
    client, _ = reader(stock_rows([], [{"pk": 4, **EMPTY_IN_STOCK, "location": 13}]))
    async with client:
        with pytest.raises(InvenTreeError, match="another part or location"):
            await client.stock_items(part=7, location=12)


def paged(total: int) -> Handler:
    rows = [{"pk": pk} for pk in range(total)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"count": total, "results": rows[offset : offset + limit]})

    return handler


@pytest.mark.parametrize("total", [0, 1, PAGE_SIZE, PAGE_SIZE + 1, 3 * PAGE_SIZE - 1])
async def test_list_all_reads_every_page(total: int) -> None:
    client, recorder = reader(paged(total))
    async with client:
        rows = await client.locations()
    assert [row["pk"] for row in rows] == list(range(total))
    assert len(recorder.requests) == max(1, -(-total // PAGE_SIZE))


async def test_list_all_refuses_an_empty_page_before_the_count_is_reached() -> None:
    # Rows deleted between two pages shift the offsets. A short list must not pass for the whole catalog.
    client, recorder = reader(respond(body={"count": 50, "results": []}))
    async with client:
        with pytest.raises(InvenTreeError, match="changed while it was read"):
            await client.parts()
    assert len(recorder.requests) == MAX_LIST_READS
    assert {request.url.params["offset"] for request in recorder.requests} == {"0"}


@pytest.mark.parametrize("step", [-2, 2])
async def test_list_all_refuses_a_count_that_changes_between_pages(step: int) -> None:
    counts = itertools.count(PAGE_SIZE + 30, step)  # every read is two pages, and every page a new count

    def handler(request: httpx.Request) -> httpx.Response:
        count = next(counts)
        size = PAGE_SIZE if request.url.params["offset"] == "0" else 3
        return httpx.Response(200, json={"count": count, "results": [{"pk": 1}] * size})

    client, recorder = reader(handler)
    async with client:
        with pytest.raises(InvenTreeError, match="changed while it was read"):
            await client.parts()
    assert len(recorder.requests) == 2 * MAX_LIST_READS


async def test_list_all_reads_a_list_that_changed_again_from_the_start() -> None:
    # A transfer lands between pages 1 and 2 of the first read (one row gone); the second read is whole. Its rows
    # alone come back, never a mix of the two reads.
    first = [{"pk": pk} for pk in range(PAGE_SIZE)], [{"pk": PAGE_SIZE + 1}]
    second = [{"pk": pk} for pk in range(1, PAGE_SIZE + 1)], [{"pk": PAGE_SIZE + 1}]
    counts = iter([PAGE_SIZE + 2, PAGE_SIZE + 1, PAGE_SIZE + 1, PAGE_SIZE + 1])
    rows_read = iter([*first, *second])
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params["offset"])
        return httpx.Response(200, json={"count": next(counts), "results": next(rows_read)})

    client, _ = reader(handler)
    async with client:
        rows = await client.parts()
    assert [row["pk"] for row in rows] == list(range(1, PAGE_SIZE + 2))
    assert offsets == ["0", str(PAGE_SIZE), "0", str(PAGE_SIZE)]


async def test_list_all_reads_again_when_the_count_disagrees_with_an_empty_first_page() -> None:
    # The count and the rows are two queries: a write that commits between them gives a count with no rows.
    pages = iter([{"count": 1, "results": []}, {"count": 0, "results": []}])
    client, recorder = reader(lambda request: httpx.Response(200, json=next(pages)))
    async with client:
        assert await client.list_all("/api/stock/") == []
    assert len(recorder.requests) == 2


async def test_list_all_does_not_read_again_for_a_list_it_cannot_trust() -> None:
    # Only a list that changed is read again; more rows than the count is not a race the next read fixes.
    client, recorder = reader(respond(body={"count": 1, "results": [{"pk": 1}, {"pk": 2}]}))
    async with client:
        with pytest.raises(InvenTreeError, match="rows for a count of"):
            await client.list_all("/api/stock/")
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "body", [[{"pk": 1}], {"results": []}, {"count": "1", "results": []}, {"count": 1, "results": [1]}]
)
async def test_list_all_refuses_a_body_that_is_not_a_page(body: object) -> None:
    client, _ = reader(respond(body=body))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.list_all("/api/part/")


@pytest.mark.parametrize("count", [-1, -50])
async def test_list_all_refuses_a_negative_count(count: int) -> None:
    # Review of #116: a count of -1 with no rows once passed for a whole, empty list, so an empty history.
    client, _ = reader(respond(body={"count": count, "results": []}))
    async with client:
        with pytest.raises(InvenTreeError, match="negative count"):
            await client.list_all("/api/stock/track/")


@pytest.mark.parametrize(("count", "rows"), [(0, 1), (2, 3), (PAGE_SIZE + 1, PAGE_SIZE + 2)])
async def test_list_all_refuses_more_rows_than_the_count(count: int, rows: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        page = [{"pk": pk} for pk in range(offset, min(rows, offset + PAGE_SIZE))]
        return httpx.Response(200, json={"count": count, "results": page})

    client, _ = reader(handler)
    async with client:
        with pytest.raises(InvenTreeError, match="rows for a count of"):
            await client.list_all("/api/stock/track/")


async def test_list_all_gives_up_on_a_server_that_never_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stockthread.inventree.MAX_PAGES", 3)
    client, recorder = reader(respond(body={"count": 10**9, "results": [{"pk": 1}]}))
    async with client:
        with pytest.raises(InvenTreeError, match="more than 3 pages"):
            await client.list_all("/api/part/")
    assert len(recorder.requests) == 3


@pytest.mark.parametrize(
    ("api_version", "allowed"),
    [(MIN_API_VERSION - 1, False), (MIN_API_VERSION, True), (MAX_API_VERSION, True), (MAX_API_VERSION + 1, False)],
)
def test_writes_need_a_tested_api_version(api_version: int, allowed: bool) -> None:
    result = check_version(ServerInfo(version="1.x", api_version=api_version))
    assert result.writes_allowed is allowed
    assert ("dry-runs still work" in result.message) is not allowed


def test_the_token_is_not_in_the_reader_repr() -> None:
    client = InvenTreeReader(SETTINGS, SecretStr(TOKEN))
    assert TOKEN not in repr(client)
    assert TOKEN not in json.dumps(vars(client), default=repr)


MARKER_ID = "k7m2q9x4t1b8w3n6p5r0zd"


def history_pages(rows: list[dict[str, object]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/stock/track/")
        assert request.url.params["search"] == MARKER_ID
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"count": len(rows), "results": rows[offset : offset + limit]})

    return handler


async def test_marker_entries_reads_every_page_and_keeps_only_whole_markers_of_the_id() -> None:
    filler = [{"pk": n, "item": 7, "notes": f"see [sic:{MARKER_ID}:a01]", "deltas": {}} for n in range(PAGE_SIZE)]
    ours: list[dict[str, object]] = [
        {
            "pk": 500,
            "item": 7,
            "notes": f"[sic:{MARKER_ID}:a01] U0SAM0001",
            "deltas": {"removed": 2},
            "tracking_type": 12,
        },
        {"pk": 501, "item": 8, "notes": f"[sic:{MARKER_ID}:u01] U0LUIS001", "deltas": None},
        {"pk": 503, "item": None, "notes": f"[sic:{MARKER_ID}:a02]", "deltas": {"removed": 1}},  # item deleted
        {"pk": 502, "item": 9, "notes": f"[sic:{MARKER_ID}x:a01] U0SAM0001", "deltas": {}},
    ]
    client, recorder = reader(history_pages(filler + ours))
    async with client:
        found = await client.marker_entries(MARKER_ID)
    assert found == [
        MarkedEntry(500, 7, Marker(MARKER_ID, AttemptKind.APPLY, 1), {"removed": 2}, 12),
        MarkedEntry(501, 8, Marker(MARKER_ID, AttemptKind.UNDO, 1), {}),
        MarkedEntry(503, None, Marker(MARKER_ID, AttemptKind.APPLY, 2), {"removed": 1}),
    ]
    assert len(recorder.requests) == 2


async def test_marker_entries_refuses_an_id_that_is_not_one_without_asking() -> None:
    client, recorder = reader(respond(body={"count": 0, "results": []}))
    async with client:
        with pytest.raises(ValueError, match="not a marker id"):
            await client.marker_entries("x")
    assert recorder.requests == []


@pytest.mark.parametrize(
    "row",
    [
        {"pk": "1", "item": 7, "deltas": {}},
        {"pk": True, "item": 7, "deltas": {}},
        {"pk": 1, "item": "7", "deltas": {}},
        {"pk": 1, "item": 7, "deltas": [1]},
        {"pk": 1, "item": 7, "deltas": {}, "tracking_type": "12"},
    ],
)
async def test_marker_entries_refuses_an_entry_of_ours_it_cannot_read(row: dict[str, object]) -> None:
    client, _ = reader(history_pages([{**row, "notes": f"[sic:{MARKER_ID}:a01]"}]))
    async with client:
        with pytest.raises(InvenTreeError, match=r"no valid pk, item, deltas or type|pk that is not a number"):
            await client.marker_entries(MARKER_ID)


@pytest.mark.parametrize("pk", [[1], {}, "1", 1.0])
async def test_marker_entries_refuses_any_entry_whose_pk_is_not_a_number(pk: object) -> None:
    """Checked before the repeat check, which could not hash [1] or {}; for any entry, ours or not."""
    client, _ = reader(history_pages([{"pk": pk, "item": 7, "notes": "x"}]))
    async with client:
        with pytest.raises(InvenTreeError, match="pk that is not a number"):
            await client.marker_entries(MARKER_ID)


async def test_marker_entries_refuses_an_entry_that_comes_back_twice() -> None:
    rows = [{"pk": n, "notes": "x"} for n in range(PAGE_SIZE)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        page = rows if offset == 0 else [rows[-1]]  # the order shifted: the last row again, another one skipped
        return httpx.Response(200, json={"count": PAGE_SIZE + 1, "results": page})

    client, _ = reader(handler)
    async with client:
        with pytest.raises(InvenTreeError, match="came back twice"):
            await client.marker_entries(MARKER_ID)


# Is a stock item gone? (design section 5, Depletion)


async def test_stock_item_gone_is_inventrees_own_404() -> None:
    client, recorder = reader(respond(404, {"detail": "No StockItem matches the given query."}))
    async with client:
        assert await client.stock_item_gone(7) is True
    [request] = recorder.requests
    assert request.url.path == "/inventree/api/stock/7/"


async def test_a_stock_item_that_answers_is_not_gone() -> None:
    client, _ = reader(respond(body={"pk": 7, "quantity": 3}))
    async with client:
        assert await client.stock_item_gone(7) is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="<html>Not Found</html>"),  # a proxy's page
        httpx.Response(404, json={"detail": "gone", "code": 1}),
        httpx.Response(404, json={"detail": None}),
        httpx.Response(404, json=["gone"]),
        httpx.Response(400, json={"detail": "bad"}),
        httpx.Response(500, json={"detail": "oops"}),
        httpx.Response(200, json={"pk": 8}),  # another item
        httpx.Response(200, json=[{"pk": 7}]),
    ],
)
async def test_only_inventree_may_say_a_stock_item_is_gone(response: httpx.Response) -> None:
    client, _ = reader(lambda request: response)
    async with client:
        with pytest.raises(InvenTreeError):
            await client.stock_item_gone(7)


async def test_stock_item_gone_retries_a_429_like_any_read() -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(404, json={"detail": "x"})])
    sleeps: list[float] = []
    client, _ = reader(lambda request: next(answers), sleeps)
    async with client:
        assert await client.stock_item_gone(7) is True
    assert sleeps == [1.0]


@pytest.mark.parametrize("pk", [0, -1, True, "7", 7.0])
async def test_stock_item_gone_refuses_a_pk_that_is_not_one_without_asking(pk: object) -> None:
    client, recorder = reader(respond(404, {"detail": "x"}))
    async with client:
        with pytest.raises(ValueError, match="positive number"):
            await client.stock_item_gone(pk)  # type: ignore[arg-type]
    assert recorder.requests == []


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"])
async def test_reader_and_writer_take_the_same_route_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # A proxy that only the reader used could answer the marker search or an item's 404 for a server the writer
    # then writes to directly (review of PR #102).
    monkeypatch.setenv(variable, "http://proxy.example:3128")
    settings = SETTINGS
    async with InvenTreeReader(settings, SecretStr("t")) as read, InvenTreeWriter(settings, SecretStr("t")) as write:
        for client in (read._http, write._http):
            assert client.trust_env is False
            assert client._mounts == {}


def item_history(rows: list[dict[str, object]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/stock/track/")
        assert request.url.params["item"] == "7"
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"count": len(rows), "results": rows[offset : offset + limit]})

    return handler


async def test_item_entries_reads_every_page_of_one_items_history() -> None:
    rows: list[dict[str, object]] = [{"pk": n, "item": 7, "notes": "x"} for n in range(PAGE_SIZE + 2)]
    client, recorder = reader(item_history(rows))
    async with client:
        assert await client.item_entries(7) == list(range(PAGE_SIZE + 2))
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [{"pk": "1", "item": 7}],
        [{"pk": 1, "item": 8}],  # the server ignored the filter
        [{"pk": 1}],
        [{"pk": 1, "item": 7}, {"pk": 1, "item": 7}],
    ],
)
async def test_item_entries_refuses_a_history_it_cannot_trust(rows: list[dict[str, object]]) -> None:
    client, _ = reader(item_history(rows))
    async with client:
        with pytest.raises(InvenTreeError, match=r"not a number|another stock item|came back twice"):
            await client.item_entries(7)


@pytest.mark.parametrize("pk", [0, -1, True, "7"])
async def test_item_entries_refuses_a_pk_that_is_not_one_without_asking(pk: object) -> None:
    client, recorder = reader(respond(body={"count": 0, "results": []}))
    async with client:
        with pytest.raises(ValueError, match="positive number"):
            await client.item_entries(pk)  # type: ignore[arg-type]
    assert recorder.requests == []


@pytest.mark.parametrize(("units", "read"), [("m", "m"), (" ft ", "ft"), ("", ""), (None, "")])
async def test_part_units_reads_the_parts_units_now(units: object, read: str) -> None:
    client, recorder = reader(respond(body={"pk": 3, "name": "Drop cable", "units": units}))
    async with client:
        assert await client.part_units(3) == read
    assert [request.url.path.endswith("/api/part/3/") for request in recorder.requests] == [True]


@pytest.mark.parametrize(
    "body", [{"pk": 4, "units": "m"}, {"pk": "3", "units": "m"}, {"pk": True, "units": "m"}, {"pk": 3},
             {"pk": 3, "units": 1}, [{"pk": 3, "units": "m"}], None],
)  # fmt: skip
async def test_part_units_refuse_anything_but_that_part_with_text_or_null_units(body: object) -> None:
    client, _ = reader(respond(body=body))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.part_units(3)


async def test_part_units_refuse_a_pk_that_is_not_a_part_pk() -> None:
    client, _ = reader(respond(body={"pk": 3, "units": "m"}))
    async with client:
        with pytest.raises(ValueError, match="positive"):
            await client.part_units(0)
