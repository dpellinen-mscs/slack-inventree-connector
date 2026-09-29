"""Characterization through the fault proxy: what the server did when the client saw a failure (design section 7).

The connector treats every one of these client-side results as an unknown outcome. These tests show why:
the server committed the write each time, and only the marker in the history tells.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from faultproxy import Fault, FaultProxy, Rule

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

TRANSFER = "/api/stock/transfer/"


@pytest.fixture
def proxy(api: RawApi) -> Iterator[FaultProxy]:
    with FaultProxy(str(api.http.base_url)) as running:
        yield running


def _through(proxy: FaultProxy, api: RawApi, body: dict[str, Any], timeout: float) -> int | str:
    """Send one transfer through the proxy and describe what the client saw."""
    with httpx.Client(base_url=proxy.url, headers=api.http.headers, timeout=timeout) as client:
        try:
            response = client.post(TRANSFER, json=body)
        except httpx.TimeoutException:
            return "timeout"
        except httpx.TransportError:
            return "connection lost"
    try:
        response.json()
    except ValueError:
        return f"{response.status_code} without JSON"
    return response.status_code


@pytest.mark.parametrize(
    ("rule", "seen"),
    [
        (Rule("POST", TRANSFER, Fault.DELAY, seconds=3.0), "timeout"),
        (Rule("POST", TRANSFER, Fault.DROP_AFTER), "connection lost"),
        (Rule("POST", TRANSFER, Fault.FAIL_AFTER, status=502), "502 without JSON"),
        (Rule("POST", TRANSFER, Fault.GARBLE), "201 without JSON"),
    ],
    ids=lambda value: value.fault.value if isinstance(value, Rule) else None,
)
def test_char_failure_seen_by_client_hides_committed_transfer(
    api: RawApi, scratch: Scratch, proxy: FaultProxy, rule: Rule, seen: str
) -> None:
    """A timeout, a lost connection, a proxy error page or a cut body can each hide a transfer that committed."""
    item = scratch.stock(5)
    marker = f"fault-{uuid.uuid4().hex}"
    body = {"items": [{"pk": item["pk"], "quantity": 2}], "location": scratch.other, "notes": marker}
    proxy.inject(rule)
    assert _through(proxy, api, body, timeout=1.0) == seen
    assert [entry.upstream_status for entry in proxy.wait_for(1)] == [201]
    assert float(scratch.item(item["pk"])["quantity"]) == 3
    assert sum(float(row["quantity"]) for row in api.items(scratch.part, scratch.other)) == 2
    rows = api.get("/api/stock/track/", search=marker)
    assert (rows["results"] if isinstance(rows, dict) else rows) != []


def test_char_failure_before_forward_writes_nothing(api: RawApi, scratch: Scratch, proxy: FaultProxy) -> None:
    """The control case: a request that never reached the server changed nothing and left no marker."""
    item = scratch.stock(5)
    marker = f"fault-{uuid.uuid4().hex}"
    body = {"items": [{"pk": item["pk"], "quantity": 2}], "location": scratch.other, "notes": marker}
    proxy.inject(Rule("POST", TRANSFER, Fault.DROP_BEFORE))
    assert _through(proxy, api, body, timeout=5.0) == "connection lost"
    assert [entry.upstream_status for entry in proxy.wait_for(1)] == [None]
    assert float(scratch.item(item["pk"])["quantity"]) == 5
    rows = api.get("/api/stock/track/", search=marker)
    assert (rows["results"] if isinstance(rows, dict) else rows) == []
