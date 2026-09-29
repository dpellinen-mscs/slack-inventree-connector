"""Integration tests against a real InvenTree server.

They run only when STOCKTHREAD_IT_URL and STOCKTHREAD_IT_TOKEN_FILE are set, as
`tests/integration/start-inventree.sh` sets them in CI. The server is disposable:
the tests create stock and move it.

`RawApi` is the only code in the repository that can write to InvenTree. It exists
to seed data and to observe server behavior, and it lives in the test suite so
that no code path in the package can write stock (ROADMAP, M0).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

CATALOG = Path(__file__).resolve().parents[2] / "eval" / "catalog"
URL = os.environ.get("STOCKTHREAD_IT_URL", "")
TOKEN_FILE = os.environ.get("STOCKTHREAD_IT_TOKEN_FILE", "")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if URL and TOKEN_FILE:
        return
    skip = pytest.mark.skip(reason="needs a disposable InvenTree: set STOCKTHREAD_IT_URL and STOCKTHREAD_IT_TOKEN_FILE")
    here = Path(__file__).parent
    for item in items:
        if here in Path(item.path).parents:
            item.add_marker(skip)


class RawApi:
    """Plain HTTP access to the test server, including writes."""

    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Token {token}"}, timeout=30)

    def get(self, path: str, **params: Any) -> Any:
        response = self.http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.http.post(path, json=body)

    def create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.post(path, body)
        assert response.status_code == 201, f"POST {path}: HTTP {response.status_code} {response.text[:500]}"
        data = response.json()
        # Creating stock can answer with a list (one entry per serial number) or with the item.
        return dict(data[0] if isinstance(data, list) else data)

    def status(self, path: str) -> int:
        return self.http.get(path).status_code

    def items(self, part: int, location: int, cascade: bool = False) -> list[dict[str, Any]]:
        rows = self.get("/api/stock/", part=part, location=location, cascade=str(cascade).lower())
        return list(rows["results"] if isinstance(rows, dict) else rows)


@dataclass(frozen=True)
class Catalog:
    locations: dict[str, int]
    """Location path -> pk."""
    parts: dict[str, int]
    """IPN -> pk."""


@dataclass(frozen=True)
class Scratch:
    """Fresh locations and a fresh part for one test, so tests never share stock."""

    api: RawApi
    part: int
    trackable_part: int
    parent: int
    child: int
    other: int

    def stock(self, quantity: float, location: int | None = None, **extra: Any) -> dict[str, Any]:
        body = {"part": self.part, "location": location or self.parent, "quantity": quantity, **extra}
        return self.api.create("/api/stock/", body)

    def item(self, pk: int) -> dict[str, Any]:
        return dict(self.api.get(f"/api/stock/{pk}/"))


@pytest.fixture(scope="session")
def api() -> Iterator[RawApi]:
    raw = RawApi(URL, Path(TOKEN_FILE).read_text(encoding="utf-8").strip())
    yield raw
    raw.http.close()


def _location(api: RawApi, name: str, parent: int | None, description: str = "", structural: bool = False) -> int:
    # Filtered again here, so a filter the server ignores cannot pick the wrong location.
    rows = api.get("/api/stock/location/", name=name)
    found = [row for row in rows if row["name"] == name and row.get("parent") == parent]
    if found:
        return int(found[0]["pk"])
    body = {"name": name, "parent": parent, "description": description, "structural": structural}
    return int(api.create("/api/stock/location/", body)["pk"])


@pytest.fixture(scope="session")
def catalog(api: RawApi) -> Catalog:
    """Seed the synthetic catalog from eval/catalog. Safe to run twice against one server."""
    locations: dict[str, int] = {}
    for entry in json.loads((CATALOG / "locations.json").read_text(encoding="utf-8"))["locations"]:
        parent_path, _, name = entry["path"].rpartition("/")
        parent = locations[parent_path] if parent_path else None
        locations[entry["path"]] = _location(api, name, parent, entry["description"], entry["structural"])
    parts: dict[str, int] = {}
    for entry in json.loads((CATALOG / "parts.json").read_text(encoding="utf-8"))["parts"]:
        found = [row for row in api.get("/api/part/", IPN=entry["ipn"]) if row.get("IPN") == entry["ipn"]]
        if found:
            parts[entry["ipn"]] = int(found[0]["pk"])
            continue
        # Units are not seeded: the catalog uses pack names such as "box", which are not physical units.
        body = {
            "name": entry["name"],
            "IPN": entry["ipn"],
            "description": entry["description"],
            "keywords": entry["keywords"],
            "trackable": entry["trackable"],
            "active": True,
            "component": True,
        }
        parts[entry["ipn"]] = int(api.create("/api/part/", body)["pk"])
    return Catalog(locations=locations, parts=parts)


@pytest.fixture
def scratch(api: RawApi) -> Scratch:
    tag = uuid.uuid4().hex[:8]
    root = _location(api, "Characterization", None, "Scratch space for integration tests")
    parent = _location(api, f"parent-{tag}", root)
    child = _location(api, f"child-{tag}", parent)
    other = _location(api, f"other-{tag}", root)
    part = api.create("/api/part/", {"name": f"Scratch part {tag}", "description": "test", "component": True})
    trackable = api.create(
        "/api/part/", {"name": f"Scratch serialized {tag}", "description": "test", "trackable": True}
    )
    return Scratch(api, int(part["pk"]), int(trackable["pk"]), parent, child, other)
