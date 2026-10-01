"""Questions: the one focused question a post with review reasons gets in its thread (design section 4).

A post can hold many reasons, but the thread asks one question at a time, and the answer re-runs
validation before the next one. The question is the first in this order, across all movements of the post:
doubts about the whole reading, the movement type, the items, the quantities, the source, the destination,
serials, stock, and last the reasons only an approver clears. Within a tier, earlier movements and lines go
first. Every reason code belongs to a tier, so every reason leads to a question.

Pure: the question is data. Wording and buttons belong to the Slack adapter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Literal, Protocol

from stockthread.core.model import CandidateMovement, Match, MatchStatus, ReasonCode, ReviewReason
from stockthread.core.validate import TYPE_UNCLEAR

MAX_CHOICES = 5
"""At most this many candidates become buttons; more are asked as a thread reply."""


class Ask(StrEnum):
    CONFIRM = "confirm"
    """Yes or no: is the reading right. For reasons that no single answer can clear."""
    MOVEMENT_TYPE = "movement_type"
    """Buttons: what kind of movement it was."""
    NAME_PART = "name_part"
    """A thread reply: which part."""
    CHOOSE_PART = "choose_part"
    """Buttons: one of the candidate parts."""
    CONFIRM_PART = "confirm_part"
    """Yes or no: is the loosely matched part the one meant."""
    QUANTITY = "quantity"
    """A thread reply: how many."""
    NAME_LOCATION = "name_location"
    """A thread reply: which place."""
    CHOOSE_LOCATION = "choose_location"
    """Buttons: one of the candidate locations."""
    CONFIRM_LOCATION = "confirm_location"
    """Yes or no: is the loosely matched location the one meant."""
    SERIALS = "serials"
    """A thread reply: the serial numbers."""


class Tier(IntEnum):
    READING = 1
    TYPE = 2
    ITEMS = 3
    QUANTITY = 4
    SOURCE = 5
    DESTINATION = 6
    SERIALS = 7
    STOCK = 8
    APPROVAL = 9


Side = Literal["source", "destination"]

# The tier and the question of each reason code that the validator or the worker raises. The match codes
# are asked from the match itself (with its candidates), so they are listed only as a fallback.
_BY_REASON: dict[ReasonCode, tuple[Tier, Ask, Side | None]] = {
    ReasonCode.SUSPICIOUS_INSTRUCTIONS: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.CONTRADICTION: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.EXTRACTORS_DISAGREE: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.PHOTO_TEXT_MISMATCH: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.CORRECTED_IN_THREAD: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.ITEM_NOT_IN_TEXT: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.QUANTITY_NOT_IN_TEXT: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.LOCATION_NOT_IN_TEXT: (Tier.READING, Ask.CONFIRM, None),
    ReasonCode.MOVEMENT_UNCLEAR: (Tier.READING, Ask.CONFIRM, None),  # but TYPE_UNCLEAR is asked as TYPE
    ReasonCode.ITEMS_MISSING: (Tier.ITEMS, Ask.NAME_PART, None),
    ReasonCode.PHOTO_ONLY: (Tier.ITEMS, Ask.NAME_PART, None),
    ReasonCode.PART_UNMATCHED: (Tier.ITEMS, Ask.NAME_PART, None),
    ReasonCode.PART_AMBIGUOUS: (Tier.ITEMS, Ask.NAME_PART, None),
    ReasonCode.FUZZY_MATCH: (Tier.ITEMS, Ask.CONFIRM, None),
    ReasonCode.QUANTITY_MISSING: (Tier.QUANTITY, Ask.QUANTITY, None),
    ReasonCode.QUANTITY_VAGUE: (Tier.QUANTITY, Ask.QUANTITY, None),
    ReasonCode.PACK_UNIT_AMBIGUOUS: (Tier.QUANTITY, Ask.QUANTITY, None),
    ReasonCode.QUANTITY_FROM_PHOTO: (Tier.QUANTITY, Ask.QUANTITY, None),
    ReasonCode.IMPLIED_SOURCE: (Tier.SOURCE, Ask.NAME_LOCATION, "source"),
    ReasonCode.IMPLIED_DESTINATION: (Tier.DESTINATION, Ask.NAME_LOCATION, "destination"),
    ReasonCode.VEHICLE_NOT_MAPPED: (Tier.DESTINATION, Ask.NAME_LOCATION, "destination"),
    ReasonCode.LOCATION_UNMATCHED: (Tier.DESTINATION, Ask.CONFIRM, None),  # a match question names the side
    ReasonCode.LOCATION_AMBIGUOUS: (Tier.DESTINATION, Ask.CONFIRM, None),
    ReasonCode.SERIAL_UNCONFIRMED: (Tier.SERIALS, Ask.SERIALS, None),
    # Resolve (source selection) will ask these as a choice of stock items; until then a human confirms.
    ReasonCode.STOCK_CHOICE: (Tier.STOCK, Ask.CONFIRM, None),
    ReasonCode.NO_TARGET_STOCK_ITEM: (Tier.STOCK, Ask.CONFIRM, None),
    ReasonCode.QUANTITY_ABOVE_STOCK: (Tier.STOCK, Ask.CONFIRM, None),
    ReasonCode.ACTOR_NOT_POSTER: (Tier.APPROVAL, Ask.CONFIRM, None),
    ReasonCode.QUANTITY_ABOVE_LIMIT: (Tier.APPROVAL, Ask.CONFIRM, None),
    ReasonCode.LATE_INGEST: (Tier.APPROVAL, Ask.CONFIRM, None),
    ReasonCode.PRECONDITION_FAILED: (Tier.APPROVAL, Ask.CONFIRM, None),
}

_FROM_MATCHES = frozenset(
    {
        ReasonCode.PART_UNMATCHED,
        ReasonCode.PART_AMBIGUOUS,
        ReasonCode.LOCATION_UNMATCHED,
        ReasonCode.LOCATION_AMBIGUOUS,
        ReasonCode.FUZZY_MATCH,
    }
)
"""Asked from the match, with its line, side and candidates. Found among the reasons, they come last in their tier."""
_LAST = 1 << 30

_LINE = re.compile(r"line (\d+)(?::|$)")


class Reviewed(Protocol):
    """One movement with its matches and reasons (the reply's MovementView fits)."""

    @property
    def movement(self) -> CandidateMovement: ...
    @property
    def items(self) -> Sequence[Match]: ...
    @property
    def source(self) -> Match | None: ...
    @property
    def destination(self) -> Match | None: ...
    @property
    def reasons(self) -> Sequence[ReviewReason]: ...


@dataclass(frozen=True)
class Question:
    ask: Ask
    reason: ReasonCode
    """The reason the answer is meant to clear."""
    movement: int
    """Index of the movement in the post, from 0."""
    line: int | None = None
    """Index of the line item, from 0, or None for the whole movement."""
    side: Side | None = None
    candidates: tuple[int, ...] = ()
    """InvenTree primary keys to choose from (CHOOSE_*), or the one loosely matched record (CONFIRM_*)."""


def pick_question(views: Sequence[Reviewed]) -> Question | None:
    """The one question to ask about a post, or None when nothing needs a human."""
    found: list[tuple[Tier, int, int, Question]] = []
    for index, view in enumerate(views):
        found.extend((tier, index, order, q) for tier, order, q in _from_reasons(index, view))
        found.extend((tier, index, order, q) for tier, order, q in _from_matches(index, view))
    if not found:
        return None
    return min(found, key=lambda entry: entry[:3])[3]


def _from_reasons(index: int, view: Reviewed) -> list[tuple[Tier, int, Question]]:
    movement = view.movement
    found = []
    for reason in view.reasons:
        tier, ask, side = _BY_REASON[reason.code]
        if reason.code is ReasonCode.MOVEMENT_UNCLEAR and reason.detail == TYPE_UNCLEAR:
            tier, ask = Tier.TYPE, Ask.MOVEMENT_TYPE
        if reason.code is ReasonCode.PHOTO_ONLY and any((item.item_text or "").strip() for item in movement.line_items):
            tier, ask = Tier.READING, Ask.CONFIRM  # items were read from the photo: is that reading right
        line = _line(reason, movement)
        order = _LAST if reason.code in _FROM_MATCHES else -1 if line is None else line
        found.append((tier, order, Question(ask, reason.code, index, line, side)))
    return found


def _line(reason: ReviewReason, movement: CandidateMovement) -> int | None:
    """The line a reason is about: named in its detail, the first line it fits, the only line, or None."""
    lines = movement.line_items
    named = _LINE.match(reason.detail)
    if named:
        line = int(named.group(1)) - 1
        return line if 0 <= line < len(lines) else None
    if reason.code is ReasonCode.QUANTITY_MISSING:
        return next((n for n, item in enumerate(lines) if item.quantity is None), None)
    if reason.code is ReasonCode.ITEMS_MISSING:
        return next((n for n, item in enumerate(lines) if not (item.item_text or "").strip()), None)
    if _BY_REASON[reason.code][0] in (Tier.QUANTITY, Tier.SERIALS) and len(lines) == 1:
        return 0
    return None


def _from_matches(index: int, view: Reviewed) -> list[tuple[Tier, int, Question]]:
    found = []
    for line, (item, match) in enumerate(zip(view.movement.line_items, view.items, strict=True)):
        if not (item.item_text or "").strip():
            continue  # ITEMS_MISSING asks for it
        question = _for_match(match, index, line, None)
        if question:
            found.append((Tier.ITEMS, line, question))
    places: tuple[tuple[Side, Match | None, Tier], ...] = (
        ("source", view.source, Tier.SOURCE),
        ("destination", view.destination, Tier.DESTINATION),
    )
    for side, place, tier in places:
        text = view.movement.source_text if side == "source" else view.movement.destination_text
        if place is None and text is not None:  # stated, but not matched: as the reply shows it
            found.append((tier, 0, Question(Ask.NAME_LOCATION, ReasonCode.LOCATION_UNMATCHED, index, None, side)))
        question = _for_match(place, index, None, side) if place else None
        if question:
            found.append((tier, 0, question))
    return found


def _for_match(match: Match, index: int, line: int | None, side: Side | None) -> Question | None:
    reason = match.review_reason(kind="part" if side is None else "location")
    if reason is None:
        return None
    part = side is None
    if match.record is not None:  # matched, by a rule that cannot authorize a write
        ask = Ask.CONFIRM_PART if part else Ask.CONFIRM_LOCATION
        return Question(ask, reason.code, index, line, side, (match.record,))
    if match.status is MatchStatus.AMBIGUOUS and len(match.candidates) <= MAX_CHOICES:
        ask = Ask.CHOOSE_PART if part else Ask.CHOOSE_LOCATION
        return Question(ask, reason.code, index, line, side, match.candidates)
    return Question(Ask.NAME_PART if part else Ask.NAME_LOCATION, reason.code, index, line, side)
