"""The read-only client against a real server, seeded from eval/catalog."""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from stockthread.catalog import load_catalog
from stockthread.config import InvenTreeSettings
from stockthread.core.model import ReasonCode
from stockthread.core.resolve import DETAIL_BATCH, Take, pick_source, stock_items
from stockthread.inventree import InvenTreeError, InvenTreeReader, ReadOnlyViolation, check_version

if TYPE_CHECKING:
    from conftest import Catalog, RawApi, Scratch

pytestmark = pytest.mark.anyio

URL_ENV = "STOCKTHREAD_IT_URL"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_reader(token: str | None = None) -> InvenTreeReader:
    url = os.environ[URL_ENV]
    if token is None:
        token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    return InvenTreeReader(InvenTreeSettings(url=url), SecretStr(token))


async def test_the_pinned_server_is_in_the_tested_range() -> None:
    async with make_reader() as reader:
        result = check_version(await reader.server_info())
    assert result.writes_allowed, result.message


async def test_reads_the_seeded_catalog(catalog: Catalog) -> None:
    async with make_reader() as reader:
        locations = {row["pk"]: row for row in await reader.locations()}
        parts = {row["pk"]: row for row in await reader.parts()}
    for path, pk in catalog.locations.items():
        assert locations[pk]["name"] == path.rpartition("/")[2]
    expected = json.loads((Path(__file__).resolve().parents[2] / "eval/catalog/parts.json").read_text())["parts"]
    assert {parts[catalog.parts[entry["ipn"]]]["IPN"] for entry in expected} == {entry["ipn"] for entry in expected}


async def test_the_matcher_catalog_holds_the_seeded_parts_and_locations(catalog: Catalog) -> None:
    async with make_reader() as reader:
        loaded = await load_catalog(reader)
    parts = {part.pk: part for part in loaded.parts}
    locations = {location.pk: location for location in loaded.locations}
    for ipn, pk in catalog.parts.items():
        assert parts[pk].ipn == ipn
        assert (parts[pk].active, parts[pk].template, parts[pk].virtual) == (True, False, False)
    for path, pk in catalog.locations.items():
        assert locations[pk].path == path  # pathstring is the full path, joined with "/"
    assert locations[catalog.locations["Warehouse"]].structural


async def test_a_part_s_stocking_unit_is_read(api: RawApi) -> None:
    """InvenTree's `units` is read as the matcher's `PartRecord.units`; a part with none reads as ""."""
    tag = uuid.uuid4().hex[:8]
    metres = api.create("/api/part/", {"name": f"Scratch cable {tag}", "description": "test", "units": "m"})
    plain = api.create("/api/part/", {"name": f"Scratch plain {tag}", "description": "test"})
    async with make_reader() as reader:
        parts = {part.pk: part for part in (await load_catalog(reader)).parts}
    assert (parts[int(metres["pk"])].units, parts[int(plain["pk"])].units) == ("m", "")


async def test_stock_items_leave_out_sub_locations(scratch: Scratch) -> None:
    here = scratch.stock(4)
    scratch.stock(6, location=scratch.child)
    async with make_reader() as reader:
        rows = await reader.stock_items(part=scratch.part, location=scratch.parent)
    assert [row["pk"] for row in rows] == [here["pk"]]


async def test_the_roles_of_the_token_user_are_read() -> None:
    async with make_reader() as reader:
        roles = await reader.user_roles()
    assert roles["is_superuser"] is True  # the CI token is the admin's
    assert roles["roles"]["stock"] == ["view", "add", "change", "delete"]


async def test_a_wrong_token_is_refused() -> None:
    async with make_reader(token="inv-not-a-real-token") as reader:
        with pytest.raises(InvenTreeError, match="refused the token"):
            await reader.locations()


async def test_the_reader_cannot_write(api: RawApi, scratch: Scratch) -> None:
    item = scratch.stock(5)
    async with make_reader() as reader:
        with pytest.raises(ReadOnlyViolation):
            await reader._http.post(
                "/api/stock/remove/", json={"items": [{"pk": item["pk"], "quantity": 5}], "notes": "must not happen"}
            )
    assert float(scratch.item(item["pk"])["quantity"]) == 5


async def test_source_selection_reads_real_stock_rows(scratch: Scratch) -> None:
    # Every field core/resolve.py reads is in the list rows of the pinned versions, `allocated` included: a row
    # without it raises rather than counting as unallocated.
    older = scratch.stock(5, batch="B-1")
    scratch.stock(3, batch="B-2")
    scratch.stock(9, status=50)  # ATTENTION: never picked
    async with make_reader() as reader:
        rows = await reader.stock_items(part=scratch.part, location=scratch.parent)
    items = stock_items(rows)
    assert {item.allocated for item in items} == {Decimal(0)}
    pick = pick_source(Decimal(4), items)
    assert pick.takes == (Take(older["pk"], Decimal(4)),)
    assert [(reason.code, reason.detail) for reason in pick.reasons] == [(ReasonCode.STOCK_CHOICE, DETAIL_BATCH)]


async def test_stock_allocated_to_a_sales_order_is_never_picked(api: RawApi, scratch: Scratch) -> None:
    part = api.create("/api/part/", {"name": f"Sold {uuid.uuid4().hex[:8]}", "description": "x", "salable": True})
    allocated = api.create("/api/stock/", {"part": part["pk"], "location": scratch.parent, "quantity": 5})
    free = api.create("/api/stock/", {"part": part["pk"], "location": scratch.parent, "quantity": 5})
    customer = api.create("/api/company/", {"name": f"Acme Fiber {uuid.uuid4().hex[:8]}", "is_customer": True})
    order = api.create("/api/order/so/", {"customer": customer["pk"]})
    line = api.create("/api/order/so-line/", {"order": order["pk"], "part": part["pk"], "quantity": 1})
    body = {"items": [{"line_item": line["pk"], "stock_item": allocated["pk"], "quantity": 1}]}
    assert api.post(f"/api/order/so/{order['pk']}/allocate/", body).status_code == 201
    async with make_reader() as reader:
        items = stock_items(await reader.stock_items(part=part["pk"], location=scratch.parent))
    assert {item.pk: item.allocated for item in items} == {allocated["pk"]: Decimal(1), free["pk"]: Decimal(0)}
    assert pick_source(Decimal(2), items).takes == (Take(free["pk"], Decimal(2)),)  # the older one is promised
