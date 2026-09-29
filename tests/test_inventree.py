import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.inventree import (
    MAX_API_VERSION,
    MIN_API_VERSION,
    PAGE_SIZE,
    InvenTreeError,
    InvenTreeReader,
    ReadOnlyViolation,
    ServerInfo,
    check_version,
)

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
    params = dict(recorder.requests[0].url.params)
    assert params == {
        "part": "7",
        "location": "12",
        "cascade": "false",
        "in_stock": "true",
        "limit": "100",
        "offset": "0",
    }


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
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("second_count", [PAGE_SIZE + 1, PAGE_SIZE + 5])
async def test_list_all_refuses_a_count_that_changes_between_pages(second_count: int) -> None:
    counts = iter([PAGE_SIZE + 3, second_count])

    def handler(request: httpx.Request) -> httpx.Response:
        count = next(counts)
        size = PAGE_SIZE if request.url.params["offset"] == "0" else 3
        return httpx.Response(200, json={"count": count, "results": [{"pk": 1}] * size})

    client, _ = reader(handler)
    async with client:
        with pytest.raises(InvenTreeError, match="changed while it was read"):
            await client.parts()


@pytest.mark.parametrize(
    "body", [[{"pk": 1}], {"results": []}, {"count": "1", "results": []}, {"count": 1, "results": [1]}]
)
async def test_list_all_refuses_a_body_that_is_not_a_page(body: object) -> None:
    client, _ = reader(respond(body=body))
    async with client:
        with pytest.raises(InvenTreeError):
            await client.list_all("/api/part/")


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
