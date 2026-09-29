"""Characterization of InvenTree roles: what a dedicated connector user needs (design sections 4 and 11).

Each test creates its own group, user and API token on the disposable server.
The token of the restricted user stays in memory and is never printed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from conftest import RawApi, Scratch

MINIMUM_ROLES = {
    "part": {"can_view": True},
    "stock_location": {"can_view": True},
    "stock": {"can_view": True, "can_add": True},
}
"""The documented minimum for the connector's user (design section 4)."""

PERMISSIONS = ("can_view", "can_add", "can_change", "can_delete")


def _group(api: RawApi, grants: dict[str, dict[str, bool]]) -> tuple[int, dict[str, dict[str, Any]]]:
    """A new group with exactly `grants`; every other role is off. Returns the group and its stored rulesets."""
    group = api.create("/api/user/group/", {"name": f"stockthread-{uuid.uuid4().hex[:8]}"})["pk"]
    stored: dict[str, dict[str, Any]] = {}
    for ruleset in api.get("/api/user/ruleset/", group=group):
        body = {"name": ruleset["name"], "group": group, **dict.fromkeys(PERMISSIONS, False)}
        body.update(grants.get(ruleset["name"], {}))
        response = api.http.put(f"/api/user/ruleset/{ruleset['pk']}/", json=body)
        assert response.status_code == 200, response.text[:500]
        stored[ruleset["name"]] = api.get(f"/api/user/ruleset/{ruleset['pk']}/")
    assert set(grants) <= set(stored)
    return int(group), stored


@pytest.fixture
def restricted(api: RawApi) -> Iterator[Any]:
    """Build an httpx client that acts as a new user in a new group with the given roles."""
    clients: list[httpx.Client] = []

    def build(group: int) -> httpx.Client:
        tag = uuid.uuid4().hex[:8]
        body = {
            "username": f"connector-{tag}",
            "first_name": "Stock",
            "last_name": "Thread",
            "email": "bot@example.com",
        }
        user = api.create("/api/user/", body)["pk"]
        # Measured: `group_ids` in the create request answers 500 (1.4.3 and 1.5.6), so the group is set afterwards.
        response = api.http.patch(f"/api/user/{user}/", json={"group_ids": [group]})
        assert response.status_code == 200, response.text[:500]
        token = api.create("/api/user/tokens/", {"user": user, "name": "connector"})["token"]
        client = httpx.Client(base_url=api.http.base_url, headers={"Authorization": f"Token {token}"}, timeout=30)
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def _adjustments(item: int, destination: int, marker: str) -> dict[str, dict[str, Any]]:
    line = [{"pk": item, "quantity": 1}]
    return {
        "/api/stock/transfer/": {"items": line, "location": destination, "notes": marker},
        "/api/stock/add/": {"items": line, "notes": marker},
        "/api/stock/remove/": {"items": line, "notes": marker},
        "/api/stock/count/": {"items": [{"pk": item, "quantity": 7}], "notes": marker},
    }


def test_char_restricted_user_can_adjust(api: RawApi, scratch: Scratch, restricted: Any) -> None:
    """View on Part and Stock Location plus view and add on Stock Item are enough for every adjustment.

    Measured on 1.4.3 and 1.5.6: the server stores "change" together with "add" on the Stock Item role, so the
    user can also edit stock items. It cannot delete them.
    """
    group, stored = _group(api, MINIMUM_ROLES)
    assert stored["stock"]["can_change"] is True  # implied by add, not requested
    assert stored["stock"]["can_delete"] is False
    user = restricted(group)
    item = scratch.stock(10)
    for path in ("/api/part/", "/api/stock/location/", "/api/stock/", "/api/stock/track/"):
        assert user.get(path, params={"limit": 1}).status_code == 200, path
    marker = f"role-{uuid.uuid4().hex}"
    for path, body in _adjustments(item["pk"], scratch.other, marker).items():
        response = user.post(path, json=body)
        assert response.status_code == 201, f"{path}: HTTP {response.status_code} {response.text[:300]}"
    assert float(scratch.item(item["pk"])["quantity"]) == 7
    assert user.delete(f"/api/stock/{item['pk']}/").status_code == 403


def test_char_user_without_stock_add_cannot_adjust(api: RawApi, scratch: Scratch, restricted: Any) -> None:
    """Without "add" on Stock Item every adjustment is refused with 403 and changes nothing, even with "change"."""
    grants = {**MINIMUM_ROLES, "stock": {"can_view": True, "can_change": True}}
    group, stored = _group(api, grants)
    assert stored["stock"]["can_add"] is False
    user = restricted(group)
    item = scratch.stock(10)
    history = len(api.get("/api/stock/track/", item=item["pk"]))
    for path, body in _adjustments(item["pk"], scratch.other, f"denied-{uuid.uuid4().hex}").items():
        assert user.post(path, json=body).status_code == 403, path
    after = scratch.item(item["pk"])
    assert (float(after["quantity"]), after["location"]) == (10, scratch.parent)
    assert len(api.get("/api/stock/track/", item=item["pk"])) == history
