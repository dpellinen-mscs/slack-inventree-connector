# SPDX-License-Identifier: MIT
"""The views a write acts on, rebuilt from the dry-run record the thread was shown (ADR 0016).

`plan_hash` is a plan revision's `input_hash`: the SHA-256 of the canonical JSON of the record's movements.
`views_of` rebuilds the views of a record that could apply in full, and refuses anything else, so a write never
acts on a reading other than the one the reply showed. `plan_of` says whether a record makes a plan revision at
all, and what it holds. Nothing here reads InvenTree or Slack.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from stockthread.core.marker import AttemptKind, Marker, new_marker_id
from stockthread.core.model import (
    CandidateMovement,
    Flag,
    LineItem,
    Match,
    MatchRule,
    MatchStatus,
    MovementType,
    Operation,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.resolve import SourcePick, Take
from stockthread.plan import plan_requests
from stockthread.slack.replies import MovementView


class NotRebuilt(ValueError):
    """The record cannot be acted on: gone, changed since the plan, or not decided in full."""


def plan_hash(result: Mapping[str, Any]) -> str:
    """The `input_hash` of a plan revision made from the dry-run record `result`."""
    canonical = json.dumps(result.get("movements"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_of(result: Mapping[str, Any]) -> tuple[str, list[Operation]] | None:
    """The `input_hash` and the operations, one per movement, of the plan revision the dry-run record `result`
    makes, or None when it makes none: a rejected reading, or a record `views_of` refuses or `plan_requests` cannot
    build every request of. The requests are built here only to be checked; each write builds its own."""
    if result.get("rejected") is not None:
        return None
    input_hash = plan_hash(result)
    try:
        views = views_of(result, input_hash)
        markers = [Marker(new_marker_id(), AttemptKind.APPLY, 1) for _ in views]
        requests = plan_requests(views, markers, user=_str(result["poster"]))
    except (KeyError, TypeError, ValueError):  # NotRebuilt and NotDecided, or a poster that is no Slack user id
        return None
    return input_hash, [request.operation for request in requests]


def views_of(result: Mapping[str, Any] | None, input_hash: str) -> list[MovementView]:
    """The views of the record `result` of a plan revision whose `input_hash` is given, in the post's order.

    Raises NotRebuilt for a record retention removed, one whose movements hash to anything else, one with a
    movement that has a gate reason or a line without stock read, and a record that does not have the form
    `dry_run.dry_run_record` writes. Picks carry their takes only: a pick with reasons has a gate reason."""
    if result is None:
        raise NotRebuilt("the dry-run record is gone")
    if plan_hash(result) != input_hash:
        raise NotRebuilt("the dry-run record is not the one the plan was made from")
    movements = result.get("movements")
    if not isinstance(movements, list) or not movements:
        raise NotRebuilt("the record has no movements")
    try:
        return [_view(number, movement) for number, movement in enumerate(movements, start=1)]
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as error:
        if isinstance(error, NotRebuilt):
            raise
        raise NotRebuilt(f"the record does not have the form of a dry-run record: {type(error).__name__}") from None


def _view(number: int, stored: Mapping[str, Any]) -> MovementView:
    if _list(stored["gate"]):
        raise NotRebuilt(f"movement {number} has a gate reason")
    lines = _list(stored["items"])
    if not lines:
        raise NotRebuilt(f"movement {number} has no lines")
    picks: list[SourcePick] = []
    named: dict[int | str, str] = {}
    for line, item in enumerate(lines):
        if item.get("stock") is None:
            raise NotRebuilt(f"movement {number}: the stock of line {line + 1} was not read")
        picks.append(
            SourcePick(tuple(Take(_int(take["item"]), _taken(take["quantity"])) for take in _list(item["stock"])))
        )
        if "named" in item:
            named[line] = _str(item["named"])
    for side in ("source", "destination"):
        if f"{side}_named" in stored:
            named[side] = _str(stored[f"{side}_named"])
    movement = CandidateMovement(
        MovementType(stored["type"]),
        tuple(
            LineItem(
                _text(item["item_text"]),
                _decimal(item["quantity"]),
                _text(item["unit"]),
                _decimal(item["pack_size"]),
                tuple(_str(serial) for serial in _list(item["serials"])),
            )
            for item in lines
        ),
        _text(stored["source_text"]),
        _text(stored["destination_text"]),
        frozenset(Flag(flag) for flag in _list(stored["flags"])),
    )
    return MovementView(
        movement,
        tuple(_required(_match(item["part"])) for item in lines),
        _match(stored["source"]),
        _match(stored["destination"]),
        tuple(
            ReviewReason(ReasonCode(reason["code"]), ReasonSource(reason["source"]), _str(reason["detail"]))
            for reason in _list(stored["reasons"])
        ),
        tuple(picks),
        named,
        _units(lines),
    )


def _units(lines: Sequence[Mapping[str, Any]]) -> tuple[str | None, ...]:
    """The part units of each line, written only for a line in its part's own length unit; () when no line has
    any. A record written before units were kept has none, and its length lines are refused by `plan_requests`."""
    units = tuple(_text(item["part_units"]) if "part_units" in item else None for item in lines)
    return units if any(unit is not None for unit in units) else ()


def _match(stored: Mapping[str, Any] | None) -> Match | None:
    if stored is None:
        return None
    rule = stored["rule"]
    record = stored["record"]
    candidates: Sequence[Any] = _list(stored["candidates"])
    return Match(
        MatchStatus(stored["status"]),
        None if rule is None else MatchRule(rule),
        None if record is None else _int(record),
        tuple(_int(candidate) for candidate in candidates),
    )


def _required(match: Match | None) -> Match:
    if match is None:
        raise ValueError("a line has a part match")
    return match


def _int(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("a primary key is an integer")
    return value


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("a text field is a string")
    return value


def _text(value: Any) -> str | None:
    return None if value is None else _str(value)


def _taken(value: Any) -> Decimal:
    """A take's quantity: zero only for a count to 0, which `build_request` alone may refuse for the rest."""
    quantity = Decimal(_str(value))
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("a take is a finite quantity, zero or more")
    return quantity


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("a list field is a list")
    return value


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(_str(value))
