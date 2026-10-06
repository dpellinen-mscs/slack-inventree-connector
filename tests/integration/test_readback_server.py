"""Read-back against a real server: plan, precondition, write, then `readback.verify` with what the reader finds. The
history deltas `readback` expects are InvenTree's, on each pinned version (`test_char_history_deltas`)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from views import picked, view

from stockthread.config import Settings, parse_settings
from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import Match, MatchRule, MatchStatus, MovementType
from stockthread.inventree import InvenTreeReader, MarkedEntry
from stockthread.plan import plan_requests
from stockthread.precondition import Snapshot, check, pairs
from stockthread.readback import verify
from stockthread.run import inventree_stock
from stockthread.slack.replies import MovementView

if TYPE_CHECKING:
    from conftest import RawApi, Scratch


def settings() -> Settings:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    data = {
        "slack": {"workspace_id": "T0ACME001", "app_id": "A0ACME001", "channels": ["C0ACME001"]},
        "inventree": {"url": os.environ["STOCKTHREAD_IT_URL"]},
    }
    env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
    return parse_settings(data, env)


def entries(marker_id: str) -> list[MarkedEntry]:
    async def search() -> list[MarkedEntry]:
        found = settings()
        async with InvenTreeReader(found.inventree, found.secrets.inventree_token) as reader:
            return await reader.marker_entries(marker_id)

    return anyio.run(search)


def planned(scratch: Scratch, movement_type: MovementType, pk: int, quantity: str) -> MovementView:
    place = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent)
    other = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other)
    acts_at_source = movement_type in (MovementType.TAKE_TO_VEHICLE, MovementType.CONSUME)
    return view(
        movement_type,
        lines=(("Disc 40", quantity),),
        stock=(picked((pk, quantity)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.part),),
        source=place if acts_at_source else None,
        destination=None if movement_type is MovementType.CONSUME else (other if acts_at_source else place),
    )


def apply(api: RawApi, movement: MovementView, between: Any = None) -> tuple[str, ...]:
    """Steps 3 to 5 as the apply protocol runs them, with `between` run after the snapshot, before the write."""
    marker = Marker(new_marker_id(), AttemptKind.APPLY, 1)
    (request,) = plan_requests([movement], [marker], user="U0SAM0001")
    read = inventree_stock(settings())
    snapshot = check(movement, request, read(pairs(movement)))
    assert isinstance(snapshot, Snapshot), snapshot
    if between is not None:
        between()
    response = api.post(request.path, dict(request.body))
    assert response.status_code == 201, response.text[:500]
    named = [int(line["pk"]) for line in request.body["items"]]
    gone = [pk for pk in named if api.status(f"/api/stock/{pk}/") == 404]
    version = int(api.get("/api/")["apiVersion"])
    found = entries(marker.marker_id)
    return verify(movement, request, marker, snapshot, found, read(pairs(movement)), gone=gone, api_version=version)


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [
        (MovementType.TAKE_TO_VEHICLE, "2.5"),  # a split
        (MovementType.TAKE_TO_VEHICLE, "5"),  # the whole item moves
        (MovementType.RECEIVE, "3"),
        (MovementType.CONSUME, "1.25"),
        (MovementType.COUNT, "7"),
        (MovementType.COUNT, "5"),  # the count it already had: InvenTree still writes the entry
    ],
)
def test_char_history_deltas(api: RawApi, scratch: Scratch, movement_type: MovementType, quantity: str) -> None:
    """Each request the connector builds writes the entries `readback` expects, and the totals add up."""
    item = scratch.stock(5)
    assert apply(api, planned(scratch, movement_type, item["pk"], quantity)) == ()


@pytest.mark.parametrize("delete_on_deplete", [True, False])
@pytest.mark.parametrize(("movement_type", "quantity"), [(MovementType.CONSUME, "3"), (MovementType.COUNT, "0")])
def test_char_a_write_that_empties_an_item(
    api: RawApi, scratch: Scratch, delete_on_deplete: bool, movement_type: MovementType, quantity: str
) -> None:
    """A remove of all 3, or a count to 0. Deleted: its entry has no item, and below API 530 the search misses it.
    Kept: it holds 0, entry and all."""
    item = scratch.stock(3, delete_on_deplete=delete_on_deplete)
    assert apply(api, planned(scratch, movement_type, item["pk"], quantity)) == ()
    assert api.status(f"/api/stock/{item['pk']}/") == (404 if delete_on_deplete else 200)


def test_another_writer_between_the_snapshot_and_the_write_is_seen(api: RawApi, scratch: Scratch) -> None:
    """Someone takes 4 of the 5 after step 3: the transfer of 2 moves the 1 left, whole (design section 7)."""
    item = scratch.stock(5)

    def take() -> None:
        assert (
            api.post("/api/stock/remove/", {"items": [{"pk": item["pk"], "quantity": 4}], "notes": "x"}).status_code
            == 201
        )

    problems = apply(api, planned(scratch, MovementType.TAKE_TO_VEHICLE, item["pk"], "2"), between=take)
    assert problems[0].startswith(f"stock item {item['pk']}: history entry")
    assert f"part {scratch.part} at location {scratch.parent} holds 0, the plan says 3" in problems
    assert Decimal(str(scratch.item(item["pk"])["quantity"])) == 1
