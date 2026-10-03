"""The one InvenTree client that can change stock (design section 7, step 4 of the apply protocol).

It sends one built `WriteRequest` once and says what InvenTree answered. It never retries: a retry happens only as
a new attempt, approved by a human, with a new marker. `send` refuses a request whose body is not exactly the one
`build_request` makes from it (so its notes lead with a marker and fit, and every write can be found in the stock
history), and a request hook lets nothing out but a POST of a marked body to one of the four stock adjustment paths
of the configured server. Proxies from the environment are ignored: the token and the writes go to that server only.

An outcome is known only when InvenTree itself answered (design section 7, "Unknown outcomes and recovery"):

- `accepted`: HTTP 201 with a JSON object that repeats the request's notes, as all four endpoints do
  (`tests/integration/test_sent_writes.py`). Read-back still decides whether it did what it said.
- `refused`: a 400 whose JSON object names only fields of these endpoints, or a 401, 403, 404 or 405 whose JSON
  object is a `detail` string alone. Nothing was written: all lines of a request succeed or fail together
  (`test_char_multi_item_request_is_atomic`).
- `unknown`: everything else. A timeout, a lost connection, a redirect, any 5xx, any other 2xx or 4xx, and any
  answer whose body is not InvenTree's, such as a proxy's or a gateway's answer or a cut-off body. Recovery settles
  it.

Nothing here judges whether a write may be sent: the caller sends only the requests of an allowed verdict
(`gate.judge`). Messages never repeat a response body or the token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import TracebackType
from typing import Any

import httpx
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import Marker
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import PATHS, WriteRequest, build_request

FIELDS = frozenset({"items", "location", "notes", "non_field_errors"})
"""The fields the four stock adjustment endpoints can name in a 400. Any other key is not InvenTree's answer."""
DETAIL_STATUSES = frozenset({401, 403, 404, 405})
"""Statuses InvenTree answers with `{"detail": "..."}` before its view runs: token, role, path, method."""


class WriteRefused(Exception):
    """The writer refused to send a request. Nothing was sent."""


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Sent:
    """What came of sending one request."""

    outcome: Outcome
    why: str
    """One line that is safe to log: no body, no token."""
    status: int | None = None
    """The HTTP status, if an answer came."""
    answer: dict[str, Any] | None = None
    """The decoded body of an accepted request, for the audit trail."""
    fields: tuple[str, ...] = ()
    """The fields InvenTree named in a refusal, out of `FIELDS`."""


class InvenTreeWriter:
    """POST-only access to the four stock adjustment endpoints, with token authentication."""

    def __init__(
        self, settings: InvenTreeSettings, token: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            headers={"Authorization": f"Token {token.get_secret_value()}", "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            follow_redirects=False,  # a redirect could carry the token, or the write, somewhere else
            trust_env=False,  # no proxy from the environment between the connector and InvenTree
            event_hooks={"request": [self._only_stock_writes]},
            transport=transport,
        )
        base = self._http.base_url
        self._allowed = {
            (base.scheme, base.host, base.port, base.raw_path + path.removeprefix("/").encode())
            for path in PATHS.values()
        }

    async def __aenter__(self) -> InvenTreeWriter:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, request: WriteRequest) -> Sent:
        """Send `request` once. Raise `WriteRefused`, sending nothing, if it is not a request `build_request` makes."""
        _check(request)
        try:
            response = await self._http.post(request.path, json=dict(request.body))
        except WriteRefused:
            raise
        except httpx.HTTPError as exc:
            # Sent or not, the server may have run it. The exception text can quote the URL; the type is enough.
            return Sent(Outcome.UNKNOWN, f"POST {request.path} failed: {type(exc).__name__}")
        return _outcome(request, response)

    async def _only_stock_writes(self, request: httpx.Request) -> None:
        """A second guard under `send`, for any future code: a marked POST to a stock path of this server only."""
        url = request.url
        # `raw_path` holds the query too, and stays percent-encoded: only the exact paths are in `_allowed`.
        if request.method != "POST" or (url.scheme, url.host, url.port, url.raw_path) not in self._allowed:
            raise WriteRefused(f"the InvenTree writer refused a {request.method} request outside the stock paths")
        try:
            notes = json.loads(request.content).get("notes")
            Marker.parse(notes.split(" ", 1)[0])
        except (ValueError, AttributeError):
            raise WriteRefused("the InvenTree writer refused a body whose notes do not lead with a marker") from None


def _check(request: WriteRequest) -> None:
    """`request` is exactly what `build_request` makes of its own operation, lines, notes and location."""
    if not isinstance(request.operation, Operation) or PATHS[request.operation] != request.path:
        raise WriteRefused("a request goes to the path of its operation")
    try:
        rebuilt = _rebuild(request)
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        raise WriteRefused("the request is not one build_request makes") from None
    # Compared as JSON, which tells `false` from `0` and `"2"` from `2`, as InvenTree would.
    if json.dumps(rebuilt.body, sort_keys=True) != json.dumps(dict(request.body), sort_keys=True):
        raise WriteRefused("the request is not one build_request makes")


def _rebuild(request: WriteRequest) -> WriteRequest:
    body = request.body
    notes = body["notes"].split(" ")
    if not 2 <= len(notes) <= 3:
        raise ValueError("notes")
    lines = body["items"]
    takes = []
    for line in lines:
        if type(line["pk"]) is not int or type(line["quantity"]) is not str:
            raise ValueError("line")
        takes.append(Take(line["pk"], Decimal(line["quantity"])))
    link = notes[2] if len(notes) == 3 else None
    marker = Marker.parse(notes[0])
    return build_request(
        request.operation, takes, marker=marker, user=notes[1], link=link, location=body.get("location")
    )


def _outcome(request: WriteRequest, response: httpx.Response) -> Sent:
    path, status = request.path, response.status_code
    body = _json_object(response)
    if status == httpx.codes.CREATED and body is not None and body.get("notes") == request.body["notes"]:
        return Sent(Outcome.ACCEPTED, f"POST {path} accepted (HTTP {status})", status, answer=body)
    if body is not None and _refusal(status, body):
        fields = tuple(sorted(key for key in body if key in FIELDS))
        return Sent(Outcome.REFUSED, f"InvenTree refused POST {path} (HTTP {status})", status, fields=fields)
    return Sent(Outcome.UNKNOWN, f"POST {path} answered HTTP {status}, which does not settle it", status)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return None
    try:
        body = response.json()
    except (ValueError, RecursionError):  # anything that is not plain JSON is not InvenTree's answer
        return None
    return body if isinstance(body, dict) else None


def _refusal(status: int, body: dict[str, Any]) -> bool:
    """InvenTree's error format (`test_char_error_body_format`): on a 400, fields of these endpoints, each naming a
    list or an object of errors (a list line's errors nest under `items`); else a `detail` string alone."""
    if status == httpx.codes.BAD_REQUEST:
        return bool(body) and all(key in FIELDS and isinstance(value, list | dict) for key, value in body.items())
    return status in DETAIL_STATUSES and set(body) == {"detail"} and isinstance(body["detail"], str)
