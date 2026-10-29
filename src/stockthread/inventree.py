# SPDX-License-Identifier: MIT
"""Read-only InvenTree REST client.

The connector reads parts, locations and stock through this client. It can
only send GET: it has no method for anything else, and a request hook refuses
any other method before it leaves the process. Stock writes have their own
client, `writer.py`, sent only behind the write gate (design section 6).

Errors never repeat a response body or the token. A body can hold inventory or
customer data, and the token is a secret.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import Marker, is_marker_id, marked

MIN_API_VERSION = 511
"""InvenTree 1.4.3, the oldest release CI tests. (1.4.4 has the same API version but no Docker image.)"""
MAX_API_VERSION = 561
"""InvenTree 1.6.3, the newest release CI tests."""

DEPLETING_MARKER_FOUND_FROM = 530
"""Measured (`test_char_deplete_deletes_item_keeps_history`): API 511 (1.4.3) does not find the marker of a removal
that deleted its item; API 530 (1.5.6) does. Below it, step 2 may excuse such a marker by the item's 404."""

PAGE_SIZE = 100
MAX_PAGES = 1000
"""A list longer than this many pages is refused: it would be a server bug or a loop."""
MAX_ATTEMPTS = 3
"""Attempts for one read that gets HTTP 429. Only reads are ever retried."""
MAX_RETRY_AFTER_SECONDS = 30.0
_API_PATH = re.compile(r"/api/(?:[A-Za-z0-9_-]+/)*")
"""Plain segments only: no dots, no percent escapes, no query. Every InvenTree path ends with a slash."""


class InvenTreeError(Exception):
    """A read failed. The message is safe to log: it holds no body and no secret."""


class ReadOnlyViolation(InvenTreeError):
    """Something tried to send a request other than GET through the read-only client."""


@dataclass(frozen=True)
class ServerInfo:
    version: str
    api_version: int


@dataclass(frozen=True)
class VersionCheck:
    info: ServerInfo
    writes_allowed: bool
    message: str


def check_version(info: ServerInfo) -> VersionCheck:
    """Writes need an API version that CI has tested. Reads, and so dry-runs, work on any version."""
    tested = f"API {MIN_API_VERSION} to {MAX_API_VERSION}"
    if MIN_API_VERSION <= info.api_version <= MAX_API_VERSION:
        return VersionCheck(info, True, f"InvenTree {info.version} (API {info.api_version}) is tested.")
    return VersionCheck(
        info,
        False,
        f"InvenTree {info.version} (API {info.api_version}) is outside the tested range ({tested}). "
        "Stock writes stay off; dry-runs still work.",
    )


async def _only_get(request: httpx.Request) -> None:
    if request.method != "GET":
        raise ReadOnlyViolation(f"the read-only InvenTree client refused a {request.method} request")


class InvenTreeReader:
    """GET-only access to the InvenTree REST API with token authentication."""

    def __init__(
        self,
        settings: InvenTreeSettings,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            headers={"Authorization": f"Token {token.get_secret_value()}", "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            follow_redirects=False,  # a redirect could carry the token somewhere else
            # No proxy from the environment, as for the writer: the reads that clear a write (a marker search, an
            # item's 404) must reach the server the write goes to, not a proxy's answer. CAs as the environment says.
            trust_env=False,
            verify=httpx.create_ssl_context(trust_env=True),
            event_hooks={"request": [_only_get]},
            transport=transport,
        )

    async def __aenter__(self) -> InvenTreeReader:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def server_info(self) -> ServerInfo:
        """Read the server name and versions from the API root."""
        data = await self.get("/api/")
        if not isinstance(data, dict) or str(data.get("server", "")).lower() != "inventree":
            raise InvenTreeError("the server at the configured URL is not InvenTree")
        api_version, version = data.get("apiVersion"), data.get("version")
        if type(api_version) is not int or not isinstance(version, str):
            raise InvenTreeError("the InvenTree API root has no valid version fields")
        return ServerInfo(version=version, api_version=api_version)

    async def get(self, path: str, params: Mapping[str, str | int | bool] | None = None) -> Any:
        """GET one API path, such as `/api/part/`, and return the decoded JSON."""
        return _decode(path, await self._response(path, params))

    async def _response(self, path: str, params: Mapping[str, str | int | bool] | None = None) -> httpx.Response:
        if not _API_PATH.fullmatch(path):
            raise InvenTreeError(f"refused to read {path!r}: only plain paths below /api/, ending in a slash")
        query = {key: str(value).lower() if isinstance(value, bool) else value for key, value in (params or {}).items()}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.get(path, params=query)
            except httpx.HTTPError as exc:
                # The exception text can quote the URL; the type is enough to act on.
                raise InvenTreeError(f"GET {path} failed: {type(exc).__name__}") from None
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS and attempt < MAX_ATTEMPTS:
                await self._sleep(_retry_after(response, attempt))
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def list_all(self, path: str, params: Mapping[str, str | int | bool] | None = None) -> list[dict[str, Any]]:
        """GET every page of a list endpoint."""
        results: list[dict[str, Any]] = []
        count: int | None = None
        for _ in range(MAX_PAGES):
            page = await self.get(path, {**(params or {}), "limit": PAGE_SIZE, "offset": len(results)})
            if (
                not isinstance(page, dict)
                or type(page.get("count")) is not int
                or not isinstance(page.get("results"), list)
            ):
                raise InvenTreeError(f"GET {path} did not return a paginated list")
            if page["count"] < 0:
                raise InvenTreeError(f"GET {path} gave a negative count")
            batch = page["results"]
            if not all(isinstance(entry, dict) for entry in batch):
                raise InvenTreeError(f"GET {path} returned a list entry that is not an object")
            # Offset paging skips or repeats rows when the list changes between pages. A changed count or an early
            # empty page shows that happened, and a partial list must never pass for the whole one.
            if (count is not None and page["count"] != count) or (not batch and len(results) < page["count"]):
                raise InvenTreeError(f"GET {path}: the list changed while it was read")
            count = page["count"]
            results.extend(batch)
            if len(results) >= count:
                # More rows than the count is as wrong as fewer: the list is not the one the count describes.
                if len(results) != count:
                    raise InvenTreeError(f"GET {path} gave {len(results)} rows for a count of {count}")
                return results
        raise InvenTreeError(f"GET {path} returned more than {MAX_PAGES} pages")

    async def marker_entries(self, marker_id: str) -> list[MarkedEntry]:
        """Every stock history entry written by any attempt (apply or undo) of the request with `marker_id`.

        Reads every page, and compares the whole marker because the server search matches substrings (design
        section 7, "The marker"). A found entry proves the write happened; an empty list proves nothing.
        """
        if not is_marker_id(marker_id):
            raise ValueError("not a marker id")
        rows = await self.list_all("/api/stock/track/", {"search": marker_id})
        # Offset paging over rows that tie on the ordering can repeat one row and skip another with the same count.
        # A repeated pk proves a skip, and a skipped entry could be the one that proves the write.
        pks = [row.get("pk") for row in rows]
        if not all(pk is None or isinstance(pk, int) for pk in pks):
            raise InvenTreeError("GET /api/stock/track/: an entry has a pk that is not a number")
        if len(set(pks)) != len(rows):
            raise InvenTreeError("GET /api/stock/track/: an entry came back twice, so another may have been skipped")
        found: list[MarkedEntry] = []
        for row in rows:
            marker = marked(row.get("notes"), marker_id)
            if marker is None:
                continue
            pk, item, deltas, kind = row.get("pk"), row.get("item"), row.get("deltas"), row.get("tracking_type")
            if (
                type(pk) is not int
                or (item is not None and type(item) is not int)
                or (deltas is not None and not isinstance(deltas, dict))
                or (kind is not None and type(kind) is not int)
            ):
                raise InvenTreeError("a stock history entry has no valid pk, item, deltas or type")
            found.append(MarkedEntry(pk, item, marker, deltas or {}, kind))
        return found

    async def item_entries(self, pk: int) -> list[int]:
        """The pk of every stock history entry of stock item `pk`, all pages (the read-back's third check, design
        section 7). Raises on an entry without a number pk, one of another item (a filter the server ignored), or one
        that came back twice (a skip may hide another)."""
        if type(pk) is not int or pk < 1:
            raise ValueError("a stock item pk is a positive number")
        rows = await self.list_all("/api/stock/track/", {"item": pk})
        pks: list[int] = []
        for row in rows:
            entry = row.get("pk")
            if type(entry) is not int:
                raise InvenTreeError("GET /api/stock/track/: an entry has a pk that is not a number")
            if type(row.get("item")) is not int or row.get("item") != pk:
                raise InvenTreeError("GET /api/stock/track/: an entry of another stock item came back")
            pks.append(entry)
        if len(set(pks)) != len(pks):
            raise InvenTreeError("GET /api/stock/track/: an entry came back twice, so another may have been skipped")
        return pks

    async def stock_item_gone(self, pk: int) -> bool:
        """True if InvenTree answers that stock item `pk` does not exist: its own 404, a `detail` string alone. False if
        it answers with the item. Anything else (a proxy's 404, another status) raises: "gone" excuses a missing
        marker (design section 5, Depletion), so only InvenTree may say it."""
        if type(pk) is not int or pk < 1:
            raise ValueError("a stock item pk is a positive number")
        path = f"/api/stock/{pk}/"
        response = await self._response(path)
        if response.status_code == httpx.codes.NOT_FOUND:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and set(body) == {"detail"} and isinstance(body["detail"], str):
                return True
            raise InvenTreeError(f"GET {path} returned a 404 that is not InvenTree's")
        item = _decode(path, response)
        if not isinstance(item, dict) or item.get("pk") != pk:
            raise InvenTreeError(f"GET {path} did not return that stock item")
        return False

    async def user_roles(self) -> dict[str, Any]:
        """The roles of the token's user (`roles`: role name -> permissions or null, `is_staff`, `is_superuser`)."""
        data = await self.get("/api/user/roles/")
        if not isinstance(data, dict):
            raise InvenTreeError("GET /api/user/roles/ did not return an object")
        return data

    async def token_expiry(self) -> date | None:
        """The day the token this reader sends expires, or None when it never does. Read from `GET
        /api/user/tokens/`, the user's own tokens, where InvenTree marks the one that authenticated the request
        `in_use` (`test_char_token_expiry`). Never `GET /api/user/token/`: that creates a new token. Nothing else of
        the rows is kept: they carry the user's email and the token's masked text."""
        rows = await self.get("/api/user/tokens/")
        if not isinstance(rows, list):
            raise InvenTreeError("GET /api/user/tokens/ did not return a list")
        in_use = [row for row in rows if isinstance(row, dict) and row.get("in_use") is True]
        if len(in_use) != 1:
            raise InvenTreeError(f"GET /api/user/tokens/ marked {len(in_use)} tokens in use, not 1")
        expiry = in_use[0].get("expiry", "")
        if expiry is None:  # only an explicit null: a server without the field says nothing about the expiry
            return None
        if isinstance(expiry, str):
            try:
                return date.fromisoformat(expiry)
            except ValueError:
                pass
        raise InvenTreeError("GET /api/user/tokens/ gave the token in use no valid expiry date")

    async def locations(self) -> list[dict[str, Any]]:
        return await self.list_all("/api/stock/location/")

    async def parts(self) -> list[dict[str, Any]]:
        return await self.list_all("/api/part/", {"active": True})

    async def part_units(self, pk: int) -> str:
        """The units part `pk` is stocked in now (`Part.units`, stripped; "" for none), read for the write that
        carries a length as a bare quantity (`precondition.check`). Anything but that part with a text or null
        `units` raises."""
        if type(pk) is not int or pk < 1:
            raise ValueError("a part pk is a positive number")
        path = f"/api/part/{pk}/"
        part = await self.get(path)
        if not isinstance(part, dict) or part.get("pk") != pk or type(part.get("pk")) is not int:
            raise InvenTreeError(f"GET {path} did not return that part")
        units = part.get("units", False)
        if units is not None and not isinstance(units, str):
            raise InvenTreeError(f"GET {path} returned a part without valid units")
        return (units or "").strip()

    async def stock_items(self, part: int, location: int) -> list[dict[str, Any]]:
        """Stock items of one part in one location, without its sub-locations, by pk: those in stock and those
        depleted to zero that would be in stock but for their quantity (`in_stock_but_empty`). An add or a count acts
        on the item that is there, even an empty one, so an empty item must not hide (`core.resolve.target_item`); a
        pick never takes from it. InvenTree's `in_stock` filter leaves depleted items out, and its `depleted` filter
        gives every item at zero, lost or sold ones too (`test_char_depleted_items_are_read`)."""
        place = {"part": part, "location": location, "cascade": False}
        in_stock = await self.list_all("/api/stock/", {**place, "in_stock": True})
        depleted = await self.list_all("/api/stock/", {**place, "depleted": True})
        rows = [*in_stock, *(row for row in depleted if in_stock_but_empty(row))]
        pks = [row.get("pk") for row in rows]
        if not all(type(pk) is int for pk in pks) or len(set(pks)) != len(pks):
            raise InvenTreeError("GET /api/stock/ gave a stock item without a pk, or one both in stock and depleted")
        # A filter the server ignored would make another part's or place's item a pick, a precondition or a target.
        if any(type(row.get("part")) is not int or row["part"] != part for row in rows) or any(
            type(row.get("location")) is not int or row["location"] != location for row in rows
        ):
            raise InvenTreeError("GET /api/stock/ gave a stock item of another part or location")
        return sorted(rows, key=lambda row: int(row["pk"]))


AVAILABLE_STATUS = frozenset({10, 50, 55})
"""InvenTree's available stock status codes (OK, attention needed, damaged): its `in_stock` filter's statuses."""
NOT_IN_STOCK_LINKS = ("customer", "belongs_to", "sales_order", "consumed_by")
"""Links InvenTree's `in_stock` filter requires to be empty: an item sent to a customer, installed in another item,
shipped on an order or consumed by a build is not in stock."""


def in_stock_but_empty(row: Mapping[str, Any]) -> bool:
    """Whether a depleted stock item row passes InvenTree's `in_stock` filter apart from its quantity: an available
    status, none of `NOT_IN_STOCK_LINKS`, not being built. A row that lacks one of these fields fails, so an item
    lost, sold or installed and then emptied never becomes an add or count target (review of #112)."""
    return (
        row.get("status") in AVAILABLE_STATUS
        and all(link in row and row[link] is None for link in NOT_IN_STOCK_LINKS)
        and row.get("is_building") is False
    )


@dataclass(frozen=True)
class MarkedEntry:
    """One stock history entry whose notes lead with a marker of the searched id."""

    pk: int
    item: int | None
    """The stock item the entry belongs to (None if the server did not say)."""
    marker: Marker
    deltas: Mapping[str, Any]
    tracking_type: int | None = None
    """InvenTree's entry type code (`readback`), None if the server did not say."""


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        seconds = 2.0 ** (attempt - 1)
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 2.0 ** (attempt - 1)
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _decode(path: str, response: httpx.Response) -> Any:
    status = response.status_code
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise InvenTreeError(f"InvenTree refused the token for GET {path} (HTTP {status})")
    if status != httpx.codes.OK:
        raise InvenTreeError(f"GET {path} returned HTTP {status}")
    try:
        return response.json()
    except ValueError:
        raise InvenTreeError(f"GET {path} did not return JSON") from None
