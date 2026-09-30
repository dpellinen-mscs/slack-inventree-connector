"""The read-only client against a real server, seeded from eval/catalog."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from stockthread.catalog import load_catalog
from stockthread.config import InvenTreeSettings
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


async def test_stock_items_leave_out_sub_locations(scratch: Scratch) -> None:
    here = scratch.stock(4)
    scratch.stock(6, location=scratch.child)
    async with make_reader() as reader:
        rows = await reader.stock_items(part=scratch.part, location=scratch.parent)
    assert [row["pk"] for row in rows] == [here["pk"]]


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
