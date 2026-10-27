# SPDX-License-Identifier: MIT
"""Decided movement views as `precondition` and `readback` take them: line n is part n, the source is location
`SOURCE`, the destination `DESTINATION`."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from stockthread.core.model import CandidateMovement, LineItem, Match, MatchRule, MatchStatus, MovementType
from stockthread.core.resolve import SourcePick, StockItem, Take
from stockthread.slack.replies import MovementView

SOURCE, DESTINATION = 10, 11
"""Where `view` takes from and puts to; its lines are parts 1, 2, ..."""


def picked(*pairs: tuple[int, str]) -> SourcePick:
    return SourcePick(tuple(Take(item, Decimal(quantity)) for item, quantity in pairs))


def view(
    movement_type: MovementType = MovementType.TAKE_TO_VEHICLE,
    lines: tuple[tuple[str, str], ...] = (("Disc 40", "2"),),
    stock: tuple[SourcePick | None, ...] = (picked((57, "2")),),
    **fields: Any,
) -> MovementView:
    """As `tests/test_plan.py` builds it: line n is part n, the source is location 10, the destination 11."""
    movement = CandidateMovement(
        movement_type,
        tuple(LineItem(text, Decimal(quantity)) for text, quantity in lines),
        None if movement_type in (MovementType.RECEIVE, MovementType.COUNT) else "main stock",
        None if movement_type is MovementType.CONSUME else "my truck",
    )
    values: dict[str, Any] = {
        "movement": movement,
        "items": tuple(Match(MatchStatus.MATCHED, MatchRule.EXACT, 1 + n) for n in range(len(lines))),
        "source": None if movement.source_text is None else Match(MatchStatus.MATCHED, MatchRule.EXACT, SOURCE),
        "destination": (
            None if movement.destination_text is None else Match(MatchStatus.MATCHED, MatchRule.EXACT, DESTINATION)
        ),
        "reasons": (),
        "stock": stock,
    }
    values.update(fields)
    return MovementView(**values)


def item(pk: int, quantity: str, status: int = 10, allocated: str = "0", serial: str | None = None) -> StockItem:
    return StockItem(pk, Decimal(quantity), status, Decimal(allocated), serial)
