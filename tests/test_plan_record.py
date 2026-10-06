"""Views rebuilt from the dry-run record (ADR 0016): exactly what the record holds, or nothing."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from views import picked, view

from stockthread.core.model import (
    CandidateMovement,
    Extraction,
    LineItem,
    MovementType,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.plan_record import NotRebuilt, plan_hash, views_of
from stockthread.slack.replies import MovementView
from stockthread.worker import _record

TO_TRUCK = view(
    lines=(("Disc 40", "2"), ("U7 Lite", "1.5")),
    stock=(picked((57, "2")), picked((58, "1"), (59, "0.5"))),
    named={1: "the lite ones", "destination": "Sam's truck"},
)
RETURN = view(
    MovementType.RETURN,
    movement=CandidateMovement(
        MovementType.RETURN,
        (LineItem("PoE injector", Decimal(3), "box", Decimal(12), ("SN-001", "SN-002")),),
        "my truck",
        "receiving",
    ),
    stock=(picked((60, "3")),),
)


def record(*views: MovementView) -> dict[str, Any]:
    extraction = Extraction("rules", tuple(v.movement for v in views))
    stored = _record(extraction, views, late=False, poster="U0SAM0001", messages=(), max_quantity=500)
    returned: dict[str, Any] = json.loads(json.dumps(stored))  # as the store gives it back
    return returned


def test_a_decided_record_rebuilds_the_views_it_was_written_from() -> None:
    stored = record(TO_TRUCK, RETURN)
    rebuilt = views_of(stored, plan_hash(stored))
    assert rebuilt == [TO_TRUCK, RETURN]
    assert record(*rebuilt)["movements"] == stored["movements"]


def test_the_hash_covers_the_movements_only() -> None:
    stored = record(TO_TRUCK)
    assert plan_hash({**stored, "late": True}) == plan_hash(stored)
    changed = json.loads(json.dumps(stored))
    changed["movements"][0]["items"][0]["stock"][0]["quantity"] = "3"
    assert plan_hash(changed) != plan_hash(stored)


def test_a_record_that_is_gone_or_changed_since_the_plan_is_refused() -> None:
    stored = record(TO_TRUCK)
    with pytest.raises(NotRebuilt, match="gone"):
        views_of(None, plan_hash(stored))
    with pytest.raises(NotRebuilt, match="not the one"):
        views_of(stored, "0" * 64)


def test_a_movement_with_a_gate_reason_is_refused() -> None:
    reason = ReviewReason(ReasonCode.IMPLIED_SOURCE, ReasonSource.RULE)
    stored = record(TO_TRUCK, view(reasons=(reason,)))
    with pytest.raises(NotRebuilt, match="movement 2 has a gate reason"):
        views_of(stored, plan_hash(stored))


def test_a_line_without_stock_is_refused() -> None:
    stored = record(TO_TRUCK)
    stored["movements"][0]["gate"] = []
    stored["movements"][0]["items"][1]["stock"] = None
    with pytest.raises(NotRebuilt, match="line 2 was not read"):
        views_of(stored, plan_hash(stored))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("items", 0, "stock", 0, "item"), "57"),
        (("items", 0, "stock", 0, "item"), True),
        (("items", 0, "stock", 0, "quantity"), "NaN"),
        (("items", 0, "stock", 0, "quantity"), "0"),
        (("items", 0, "stock", 0, "quantity"), 2),
        (("items", 0, "part", "status"), "found"),
        (("items", 0, "part"), None),
        (("source", "record"), 10.0),
        (("type",), "teleport"),
        (("items",), []),
    ],
)
def test_a_record_not_of_the_dry_runs_form_is_refused(path: tuple[Any, ...], value: Any) -> None:
    stored = record(TO_TRUCK)
    target: Any = stored["movements"][0]
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    with pytest.raises(NotRebuilt):
        views_of(stored, plan_hash(stored))
