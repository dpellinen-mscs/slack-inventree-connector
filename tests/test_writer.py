import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import certifi
import httpx
import pytest
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import PATHS, WriteRequest, build_request
from stockthread.inventree import InvenTreeReader
from stockthread.writer import InvenTreeWriter, Outcome, Sent, WriteRefused, _check

pytestmark = pytest.mark.anyio

TOKEN = "inv-synthetic-token-0123456789"
SETTINGS = InvenTreeSettings(url="https://tools.acme-fiber.example/inventree/")
MARKER = Marker("k7m2q9x4t1b8w3n6p5r0zd", AttemptKind.APPLY, 1)
LINK = "https://acme-fiber.slack.com/archives/C0ACME0001/p1700000000000100"
REQUEST = build_request(
    Operation.TRANSFER, [Take(57, Decimal(2))], marker=MARKER, user="U0SAM0001", link=LINK, location=11
)
SECRET_ITEM = "customer 12 Elm St"
"""Text a body could carry, which no message may repeat."""

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def writer(handler: Handler, seen: list[httpx.Request]) -> InvenTreeWriter:
    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return InvenTreeWriter(SETTINGS, SecretStr(TOKEN), transport=httpx.MockTransport(record))


async def send(handler: Handler, request: WriteRequest = REQUEST) -> tuple[Sent, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    async with writer(handler, seen) as client:
        sent = await client.send(request)
    return sent, seen


def answer(status: int, body: object, content_type: str = "application/json") -> Handler:
    text = body if isinstance(body, str) else json.dumps(body)
    return lambda request: httpx.Response(status, content=text.encode(), headers={"content-type": content_type})


async def test_a_request_is_posted_once_as_built_with_the_token() -> None:
    _, [request] = await send(answer(201, {**REQUEST.body}))
    assert request.method == "POST"
    assert str(request.url) == "https://tools.acme-fiber.example/inventree/api/stock/transfer/"
    assert request.headers["Authorization"] == f"Token {TOKEN}"
    assert json.loads(request.content) == REQUEST.body


@pytest.mark.parametrize("operation", list(Operation))
async def test_a_201_that_repeats_the_notes_is_accepted_and_keeps_the_answer(operation: Operation) -> None:
    location = 11 if operation is Operation.TRANSFER else None
    request = build_request(operation, [Take(57, Decimal(2))], marker=MARKER, user="U0SAM0001", location=location)
    echoed = {"items": [], "notes": request.body["notes"]}
    sent, _ = await send(answer(201, echoed), request)
    assert (sent.outcome, sent.status, sent.answer) == (Outcome.ACCEPTED, 201, echoed)


@pytest.mark.parametrize(
    ("status", "body", "fields"),
    [
        (400, {"location": ["This field is required."]}, ("location",)),
        (400, {"items": [{"pk": ["Invalid pk"]}, {}]}, ("items",)),
        (400, {"non_field_errors": ["x"], "notes": ["y"]}, ("non_field_errors", "notes")),
        (400, {"items": {"0": {"quantity": ["Too much"]}}}, ("items",)),
        (404, {"detail": "Not found."}, ()),
        (401, {"detail": "Invalid token."}, ()),
        (403, {"detail": "You do not have permission to perform this action."}, ()),
    ],
)
async def test_a_4xx_in_inventrees_error_format_is_a_known_refusal(
    status: int, body: object, fields: tuple[str, ...]
) -> None:
    sent, _ = await send(answer(status, body))
    assert (sent.outcome, sent.status, sent.fields, sent.answer) == (Outcome.REFUSED, status, fields, None)


@pytest.mark.parametrize(
    ("status", "body", "content_type"),
    [
        (201, "<html>ok</html>", "text/html"),  # not InvenTree's answer, even with a 2xx
        (200, {}, "application/json"),  # a gateway's "ok" that never forwarded the write
        (202, {"notes": "NOTES"}, "application/json"),
        (200, {"notes": "NOTES"}, "application/json"),
        (201, {}, "application/json"),
        (201, {"notes": "[sic:k7m2q9x4t1b8w3n6p5r0zd:a02] U0SAM0001"}, "application/json"),  # someone else's notes
        (201, {"notes": "NOTES"}, "application/json-seq"),
        (201, "[" * 100_000 + "]" * 100_000, "application/json"),  # nested past the recursion limit
        (201, '{"items": [', "application/json"),  # a cut-off body
        (201, [], "application/json"),
        (201, {"notes": "x"}, "text/plain"),
        (204, "", "application/json"),
        (302, "", "text/html"),
        (400, "<html>Bad Request</html>", "text/html"),  # a proxy's page
        (400, {}, "application/json"),
        (400, ["x"], "application/json"),
        (400, {"location": "required"}, "application/json"),  # not a list or an object
        (400, {"errors": [{"code": "blocked"}]}, "application/json"),  # a gateway's or a WAF's format
        (400, {"location": ["x"], "Elm St 12": ["y"]}, "application/json"),
        (403, {"error": {"code": "waf"}}, "application/json"),
        (403, {"detail": "blocked", "code": "waf"}, "application/json"),
        (400, {"detail": "Bad request"}, "application/json"),
        (409, {"detail": "conflict"}, "application/json"),
        (400, {"detail": ["x"]}, "application/json"),
        (404, "<html>Not Found</html>", "text/html"),
        (408, {"detail": "timeout"}, "application/json"),
        (429, {"detail": "slow down"}, "application/json"),
        (499, {"detail": "closed"}, "application/json"),
        (500, {"detail": "error"}, "application/json"),
        (502, "<html>Bad Gateway</html>", "text/html"),
        (503, {"detail": "unavailable"}, "application/json"),
    ],
)
async def test_any_other_answer_is_unknown(status: int, body: object, content_type: str) -> None:
    if isinstance(body, dict) and body.get("notes") == "NOTES":
        body = {**body, "notes": REQUEST.body["notes"]}
    sent, seen = await send(answer(status, body, content_type))
    assert (sent.outcome, sent.status, sent.answer, sent.fields) == (Outcome.UNKNOWN, status, None, ())
    assert len(seen) == 1  # never retried


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.RemoteProtocolError("r"), httpx.WriteError("w")]
)
async def test_a_failure_before_any_answer_is_unknown(error: httpx.HTTPError) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    sent, seen = await send(fail)
    assert (sent.outcome, sent.status) == (Outcome.UNKNOWN, None)
    assert sent.why == f"POST /api/stock/transfer/ failed: {type(error).__name__}"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "handler",
    [
        answer(201, {"notes": SECRET_ITEM}),
        answer(400, {"items": [SECRET_ITEM], SECRET_ITEM: ["x"]}),
        answer(502, f"<html>{SECRET_ITEM} {TOKEN}</html>", "text/html"),
    ],
)
async def test_no_message_repeats_the_body_or_the_token(handler: Handler) -> None:
    sent, _ = await send(handler)
    assert SECRET_ITEM not in sent.why + " ".join(sent.fields)
    assert TOKEN not in sent.why + " ".join(sent.fields)


@pytest.mark.parametrize(
    "request_",
    [
        WriteRequest(Operation.TRANSFER, PATHS[Operation.REMOVE], REQUEST.body),
        WriteRequest(Operation.REMOVE, "/api/stock/", REQUEST.body),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": "U0SAM0001 " + str(MARKER)}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": str(MARKER) + "x"}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": ""}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {key: v for key, v in REQUEST.body.items() if key != "notes"}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": None}),
        WriteRequest(
            Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": f"{MARKER} U0SAM0001 {LINK}{'x' * 600}"}
        ),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": f"{MARKER} U0SAM0001 {LINK} more"}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "notes": f"{MARKER}  U0SAM0001"}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "extra": 1}),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "extra": Decimal(1)}),  # not JSON
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "location": True}),
        WriteRequest(
            Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": [{"pk": 57, "quantity": 2.0, "merge": False}]}
        ),
        WriteRequest(
            Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": [{"pk": 57, "quantity": "2.0", "merge": False}]}
        ),
        WriteRequest(
            Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": [{"pk": 57, "quantity": "2", "merge": 0}]}
        ),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": [{"pk": 57, "quantity": "2"}]}),
        WriteRequest(
            Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": [{"pk": True, "quantity": "2", "merge": False}]}
        ),
        WriteRequest(Operation.TRANSFER, REQUEST.path, {**REQUEST.body, "items": []}),
        WriteRequest("transfer", REQUEST.path, REQUEST.body),  # type: ignore[arg-type]
        WriteRequest(Operation.REMOVE, PATHS[Operation.REMOVE], REQUEST.body),  # a remove takes no location
    ],
)
async def test_a_request_build_request_does_not_make_is_refused_before_anything_is_sent(
    request_: WriteRequest,
) -> None:
    seen: list[httpx.Request] = []
    async with writer(answer(201, {}), seen) as client:
        with pytest.raises(WriteRefused):
            await client.send(request_)
    assert seen == []


def test_the_request_built_is_one_send_accepts() -> None:
    """The control for the refusals above: `build_request` output passes the same check."""
    _check(REQUEST)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/stock/transfer/"),
        ("DELETE", "/api/stock/transfer/"),
        ("POST", "/api/stock/"),
        ("POST", "/api/stock/1/"),
        ("POST", "https://elsewhere.example/inventree/api/stock/transfer/"),  # the token to another host
        ("POST", "http://tools.acme-fiber.example/inventree/api/stock/transfer/"),
        ("POST", "https://tools.acme-fiber.example:8443/inventree/api/stock/transfer/"),
        ("POST", "https://tools.acme-fiber.example/api/stock/transfer/"),  # outside the base path
        ("POST", "/x/api/stock/transfer/"),
        ("POST", "/api/stock/1/api/stock/transfer/"),
        ("POST", "/api/stock/transfer"),
        ("POST", "/api/stock/transfer///"),
        ("POST", "/api/stock/transfer/?a=1"),
        ("POST", "/api/stock/%74ransfer/"),
    ],
)
async def test_the_hook_lets_nothing_out_but_a_post_to_a_stock_path_of_the_server(method: str, url: str) -> None:
    """A second guard under `send`: no future method of this client can reach anything else."""
    seen: list[httpx.Request] = []
    async with writer(answer(201, {}), seen) as client:
        with pytest.raises(WriteRefused):
            await client._http.request(method, url, json=dict(REQUEST.body))
    assert seen == []


@pytest.mark.parametrize("body", [{"items": []}, {"notes": "U0SAM0001"}, {"notes": 1}, [1], None])
async def test_the_hook_lets_no_body_out_whose_notes_do_not_lead_with_a_marker(body: object) -> None:
    seen: list[httpx.Request] = []
    async with writer(answer(201, {}), seen) as client:
        with pytest.raises(WriteRefused):
            await client._http.post("/api/stock/transfer/", json=body)
    assert seen == []


async def test_the_hook_lets_a_marked_post_to_each_stock_path_out() -> None:
    seen: list[httpx.Request] = []
    async with writer(answer(201, {}), seen) as client:
        for path in PATHS.values():
            await client._http.post(path, json=dict(REQUEST.body))
    assert len(seen) == len(PATHS)


async def test_proxies_from_the_environment_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    client = InvenTreeWriter(SETTINGS, SecretStr(TOKEN))
    async with client:
        http = client._http
        assert http._transport_for_url(httpx.URL(SETTINGS.url)) is http._transport


def _ca_certs(client: httpx.AsyncClient) -> list[object]:
    return client._transport._pool._ssl_context.get_ca_certs()  # type: ignore[attr-defined,no-any-return]


async def test_the_writer_trusts_the_ca_file_from_the_environment_as_the_reader_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # One CA out of certifi's bundle stands in for an internal CA: a context that loaded the file holds it alone.
    bundle = Path(certifi.where()).read_text()
    end = "-----END CERTIFICATE-----"
    ca_file = tmp_path / "internal-ca.pem"
    ca_file.write_text(bundle[bundle.index("-----BEGIN CERTIFICATE-----") : bundle.index(end) + len(end)] + "\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    async with (
        InvenTreeWriter(SETTINGS, SecretStr(TOKEN)) as client,
        InvenTreeReader(SETTINGS, SecretStr(TOKEN)) as reader,
    ):
        writer_cas = _ca_certs(client._http)
        assert len(writer_cas) == 1
        assert writer_cas == _ca_certs(reader._http)
