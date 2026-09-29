"""Validate: review reasons derived by rule from the post text and the candidate (design section 7).

Every reason an extractor can raise is also derived here by rule, where the text or the fields
allow it, so an extractor that leaves a flag out cannot remove a review reason. Extractor flags
still add reasons. Nothing here reads the catalog or live stock: matching and resolve add their
own reasons later.

A reason's detail names the check, never the message text, so reasons are safe to log.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from stockthread.core.model import (
    CandidateMovement,
    MovementType,
    PostFlag,
    ReasonCode,
    ReasonSource,
    ReviewReason,
    reason_for_flag,
)
from stockthread.core.signals import (
    CONTAINERS,
    HEDGE,
    NEGATION,
    NUMBER_WORDS,
    PLAN_OR_CONDITION,
    QUESTION_OR_REQUEST,
    SUSPICIOUS,
    VEHICLE_WORDS,
)

_NUMBER_TOKEN = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?=x\b|[^\w.]|$|-)", re.IGNORECASE)
"""A number on its own: "2", "2x", "12-piece", "2-3". Not the 3 in "van3" or the 40 in "d40"."""
_ONE = re.compile(r"\b(a|an|the|one|single|this|that)\b", re.IGNORECASE)
_WORD_QUANTITIES = (
    (re.compile(r"\bhalf a dozen\b", re.IGNORECASE), Decimal(6)),
    (re.compile(r"\bdozen\b", re.IGNORECASE), Decimal(12)),
    (re.compile(r"\b(pair|couple)\b", re.IGNORECASE), Decimal(2)),
    (re.compile(r"\bhalf\b", re.IGNORECASE), Decimal("0.5")),
)


def _rule(code: ReasonCode, detail: str = "") -> ReviewReason:
    return ReviewReason(code, ReasonSource.RULE, detail)


def validate(
    movement: CandidateMovement,
    text: str,
    *,
    max_quantity: int,
    post_flags: frozenset[PostFlag] = frozenset(),
) -> tuple[ReviewReason, ...]:
    """Every review reason for one candidate. `text` is the post's text by people, all messages joined."""
    reasons = {reason_for_flag(flag) for flag in movement.flags}
    reasons |= {reason_for_flag(flag) for flag in post_flags if flag is not PostFlag.CONTAINS_SENSITIVE}
    reasons |= _from_fields(movement, text)
    reasons |= _from_text(text)
    reasons |= _grounding(movement, text)
    reasons |= _contradictions(movement)
    reasons |= {
        _rule(ReasonCode.QUANTITY_ABOVE_LIMIT, f"line {n}: above policy.max_quantity")
        for n, item in enumerate(movement.line_items, start=1)
        if item.quantity is not None and item.quantity > max_quantity
    }
    return tuple(sorted(reasons, key=lambda reason: (reason.code.value, reason.source.value, reason.detail)))


def _from_fields(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """The flags that follow from the fields (eval/README.md, "Flags"), and an unclear type."""
    kind = movement.movement_type
    reasons = set()
    if movement.source_text is None and kind not in (MovementType.RECEIVE, MovementType.COUNT):
        reasons.add(_rule(ReasonCode.IMPLIED_SOURCE))
    if movement.destination_text is None and kind is not MovementType.CONSUME:
        reasons.add(_rule(ReasonCode.IMPLIED_DESTINATION))
    if not movement.line_items or any(not (item.item_text or "").strip() for item in movement.line_items):
        reasons.add(_rule(ReasonCode.ITEMS_MISSING))
    if any(item.quantity is None for item in movement.line_items):
        reasons.add(_rule(ReasonCode.QUANTITY_MISSING, "no number"))
    if any(item.unit in CONTAINERS or item.pack_size is not None for item in movement.line_items):
        reasons.add(_rule(ReasonCode.PACK_UNIT_AMBIGUOUS))
    if not text.strip():
        reasons.add(_rule(ReasonCode.PHOTO_ONLY, "no text"))
    if kind is MovementType.UNCLEAR:
        reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, "type unclear"))
    return reasons


def _from_text(text: str) -> set[ReviewReason]:
    reasons = set()
    if SUSPICIOUS.search(text):
        reasons.add(_rule(ReasonCode.SUSPICIOUS_INSTRUCTIONS))
    for pattern, detail in (
        (QUESTION_OR_REQUEST, "question or request"),
        (PLAN_OR_CONDITION, "plan or condition"),
        (NEGATION, "negation"),
        (HEDGE, "hedge"),
    ):
        if pattern.search(text):
            reasons.add(_rule(ReasonCode.MOVEMENT_UNCLEAR, detail))
    return reasons


def _folded(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _grounding(movement: CandidateMovement, text: str) -> set[ReviewReason]:
    """Each item text appears in the message, and each quantity matches a number or number word in it."""
    folded = _folded(text)
    numbers = set()
    for token in _NUMBER_TOKEN.findall(folded):
        try:
            numbers.add(Decimal(token))
        except InvalidOperation:  # pragma: no cover - the pattern only finds numbers
            continue
    numbers |= {Decimal(value) for word, value in NUMBER_WORDS.items() if re.search(rf"\b{word}\b", folded)}
    numbers |= {value for pattern, value in _WORD_QUANTITIES if pattern.search(folded)}
    if _ONE.search(folded):
        numbers.add(Decimal(1))
    reasons = set()
    for n, item in enumerate(movement.line_items, start=1):
        if item.item_text and _folded(item.item_text) not in folded:
            reasons.add(_rule(ReasonCode.ITEM_NOT_IN_TEXT, f"line {n}"))
        if item.quantity is not None and item.quantity not in numbers:
            reasons.add(_rule(ReasonCode.QUANTITY_NOT_IN_TEXT, f"line {n}"))
    return reasons


def _contradictions(movement: CandidateMovement) -> set[ReviewReason]:
    kind = movement.movement_type
    source, destination = movement.source_text, movement.destination_text
    onto_vehicle = destination is not None and VEHICLE_WORDS.search(destination) is not None
    found = []
    if kind in (MovementType.RETURN, MovementType.TRANSFER) and onto_vehicle:
        found.append(f"a {kind.value} whose destination is a vehicle")
    if kind is MovementType.TAKE_TO_VEHICLE and destination is not None and not onto_vehicle:
        found.append("take_to_vehicle whose destination names no vehicle")
    if kind is MovementType.RECEIVE and source is not None:
        found.append("a receive has no source location")
    if kind is not MovementType.COUNT and any(item.quantity == 0 for item in movement.line_items):
        found.append("zero quantity outside a count")
    if source is not None and destination is not None and _folded(source) == _folded(destination):
        found.append("source and destination are the same")
    return {_rule(ReasonCode.CONTRADICTION, detail) for detail in found}
