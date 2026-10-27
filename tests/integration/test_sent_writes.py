# SPDX-License-Identifier: MIT
"""The writer against a real server (design section 7, step 4): each built request is accepted and its answer kept,
a request InvenTree refuses is a known refusal that changed nothing, and every failure the client cannot read is
unknown, even when the server committed the write."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from faultproxy import Fault, FaultProxy, Rule
from pydantic import SecretStr

from stockthread.config import InvenTreeSettings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Operation
from stockthread.core.resolve import Take
from stockthread.core.write_request import PATHS, WriteRequest, build_request
from stockthread.writer import InvenTreeWriter, Outcome

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import RawApi, Scratch

pytestmark = pytest.mark.anyio

LINK = "https://acme-fiber.slack.com/archives/C0ACME0001/p1700000000000100"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def proxy(api: RawApi) -> Iterator[FaultProxy]:
    with FaultProxy(str(api.http.base_url)) as running:
        yield running


def make_writer(url: str | None = None, timeout: int = 10) -> InvenTreeWriter:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    settings = InvenTreeSettings(url=url or os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=timeout)
    return InvenTreeWriter(settings, SecretStr(token))


def build(operation: Operation, takes: list[Take], location: int | None = None) -> WriteRequest:
    marker = Marker(new_marker_id(), AttemptKind.APPLY, 1)
    return build_request(operation, takes, marker=marker, user="U0SAM0001", link=LINK, location=location)


def quantity(scratch: Scratch, pk: int) -> Decimal:
    return Decimal(str(scratch.item(pk)["quantity"]))


async def test_every_built_request_is_accepted_with_its_answer(api: RawApi, scratch: Scratch) -> None:
    item = scratch.stock(10)
    requests = [
        build(Operation.TRANSFER, [Take(item["pk"], Decimal(3))], location=scratch.other),
        build(Operation.REMOVE, [Take(item["pk"], Decimal(2))]),
        build(Operation.ADD, [Take(item["pk"], Decimal(1))]),
        build(Operation.COUNT, [Take(item["pk"], Decimal(4))]),
    ]
    async with make_writer() as writer:
        sent = [await writer.send(request) for request in requests]
    assert [(each.outcome, each.status) for each in sent] == [(Outcome.ACCEPTED, 201)] * 4
    assert all(
        each.answer is not None and each.answer.get("notes") == r.body["notes"]
        for each, r in zip(sent, requests, strict=True)
    )
    assert quantity(scratch, item["pk"]) == 4
    assert sum(Decimal(str(row["quantity"])) for row in api.items(scratch.part, scratch.other)) == 3


async def test_a_request_inventree_refuses_is_a_known_refusal_that_changed_nothing(scratch: Scratch) -> None:
    item = scratch.stock(5)
    good = build(Operation.REMOVE, [Take(item["pk"], Decimal(1))])
    lines = [*good.body["items"], {"pk": 999999999, "quantity": "1"}]  # the second line names no stock item
    async with make_writer() as writer:
        sent = await writer.send(WriteRequest(Operation.REMOVE, good.path, {**good.body, "items": lines}))
    assert (sent.outcome, sent.status, sent.fields) == (Outcome.REFUSED, 400, ("items",))
    assert quantity(scratch, item["pk"]) == 5


async def test_a_transfer_to_a_location_that_does_not_exist_is_refused_by_field(scratch: Scratch) -> None:
    item = scratch.stock(5)
    request = build(Operation.TRANSFER, [Take(item["pk"], Decimal(1))], location=999999999)
    async with make_writer() as writer:
        sent = await writer.send(request)
    assert (sent.outcome, sent.status, sent.fields) == (Outcome.REFUSED, 400, ("location",))
    assert quantity(scratch, item["pk"]) == 5


@pytest.mark.parametrize(
    "rule",
    [
        Rule("POST", PATHS[Operation.TRANSFER], Fault.DELAY, seconds=3.0),
        Rule("POST", PATHS[Operation.TRANSFER], Fault.DROP_AFTER),
        Rule("POST", PATHS[Operation.TRANSFER], Fault.FAIL_AFTER, status=502),
        Rule("POST", PATHS[Operation.TRANSFER], Fault.FAIL_AFTER, status=404),
        Rule("POST", PATHS[Operation.TRANSFER], Fault.GARBLE),
    ],
    ids=lambda rule: f"{rule.fault.value}-{rule.status}",
)
async def test_an_answer_the_client_cannot_read_is_unknown_though_the_write_committed(
    scratch: Scratch, proxy: FaultProxy, rule: Rule
) -> None:
    item = scratch.stock(5)
    request = build(Operation.TRANSFER, [Take(item["pk"], Decimal(2))], location=scratch.other)
    proxy.inject(rule)
    async with make_writer(proxy.url, timeout=1) as writer:
        sent = await writer.send(request)
    assert sent.outcome is Outcome.UNKNOWN
    assert [entry.upstream_status for entry in proxy.wait_for(1)] == [201]
    assert quantity(scratch, item["pk"]) == 3


async def test_a_request_that_never_reached_the_server_is_still_unknown(scratch: Scratch, proxy: FaultProxy) -> None:
    """The client cannot tell a connection dropped before forwarding from one dropped after."""
    item = scratch.stock(5)
    proxy.inject(Rule("POST", PATHS[Operation.REMOVE], Fault.DROP_BEFORE))
    async with make_writer(proxy.url) as writer:
        sent = await writer.send(build(Operation.REMOVE, [Take(item["pk"], Decimal(1))]))
    assert sent.outcome is Outcome.UNKNOWN
    assert quantity(scratch, item["pk"]) == 5
