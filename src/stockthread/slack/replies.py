"""The dry-run reply in a post's thread: what would change, and every reason it needs a human.

Pure: it turns values into a `chat.postMessage` body (`text` and `blocks`). Text that came from
the post (item and location words) is escaped, so a post cannot make the connector write a
mention such as <!channel> or a disguised link. Catalog names are escaped too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockthread.core.model import CandidateMovement, Match, MatchStatus, MovementType, ReasonCode, ReviewReason

MAX_SECTION = 2900
"""Slack allows 3000 characters in a section block. Longer text is cut, with a marker."""
MAX_CANDIDATES = 5
"""How many candidates a line shows. The result keeps all of them; only the display is capped."""
MAX_MOVEMENTS = 20
"""How many movements a reply shows. Slack refuses more than 50 blocks; each movement takes 2."""

TYPE_LABELS = {
    MovementType.TAKE_TO_VEHICLE: "Take to a vehicle",
    MovementType.TAKE_TO_SITE: "Take to a site",
    MovementType.RETURN: "Return",
    MovementType.TRANSFER: "Transfer",
    MovementType.RECEIVE: "Receive",
    MovementType.CONSUME: "Use up",
    MovementType.COUNT: "Count",
    MovementType.UNCLEAR: "Unclear movement",
}

REASONS = {
    ReasonCode.IMPLIED_SOURCE: "No source location was stated.",
    ReasonCode.IMPLIED_DESTINATION: "No destination was stated.",
    ReasonCode.QUANTITY_MISSING: "A quantity is missing.",
    ReasonCode.QUANTITY_VAGUE: "A quantity is approximate or vague.",
    ReasonCode.PACK_UNIT_AMBIGUOUS: "A box, bag or pack: how many pieces is not clear.",
    ReasonCode.ITEMS_MISSING: "No item was named.",
    ReasonCode.PHOTO_ONLY: "The post is a photo without text.",
    ReasonCode.QUANTITY_FROM_PHOTO: "A quantity was read from a photo.",
    ReasonCode.SERIAL_UNCONFIRMED: "A serial number is not confirmed.",
    ReasonCode.PHOTO_TEXT_MISMATCH: "The photo and the text disagree.",
    ReasonCode.CORRECTED_IN_THREAD: "A value was corrected.",
    ReasonCode.ACTOR_NOT_POSTER: "Someone other than the poster did this.",
    ReasonCode.SUSPICIOUS_INSTRUCTIONS: "The post contains instructions to the connector. They were ignored.",
    ReasonCode.MOVEMENT_UNCLEAR: "What kind of movement this is, or its direction, is not clear.",
    ReasonCode.CONTRADICTION: "The movement contradicts itself.",
    ReasonCode.ITEM_NOT_IN_TEXT: "An item is not in the post as written.",
    ReasonCode.LOCATION_NOT_IN_TEXT: "A place is not in the post as written.",
    ReasonCode.QUANTITY_NOT_IN_TEXT: "A quantity is not stated next to its item.",
    ReasonCode.QUANTITY_ABOVE_LIMIT: "A quantity is above the configured limit.",
    ReasonCode.QUANTITY_ABOVE_STOCK: "A quantity is more than the stock on hand.",
    ReasonCode.PART_UNMATCHED: "No part matches an item.",
    ReasonCode.PART_AMBIGUOUS: "Several parts match an item.",
    ReasonCode.LOCATION_UNMATCHED: "No stock location matches a place.",
    ReasonCode.LOCATION_AMBIGUOUS: "Several stock locations match a place.",
    ReasonCode.FUZZY_MATCH: "A part or location was found by a loose match and needs a check.",
    ReasonCode.VEHICLE_NOT_MAPPED: "The poster has no vehicle mapped.",
    ReasonCode.STOCK_CHOICE: "Stock has to be picked from several stock items.",
    ReasonCode.NO_TARGET_STOCK_ITEM: "There is no stock of this part at the source.",
    ReasonCode.EXTRACTORS_DISAGREE: "The two extractors read the post differently.",
    ReasonCode.LATE_INGEST: "The post was read late: the connector was down, disconnected or waiting.",
    ReasonCode.PRECONDITION_FAILED: "Stock changed since the plan was made.",
}


@dataclass(frozen=True)
class MovementView:
    """One movement with its match results and reasons, as the reply shows it."""

    movement: CandidateMovement
    items: Sequence[Match]
    """One match per line item, in order."""
    source: Match | None
    destination: Match | None
    reasons: Sequence[ReviewReason]

    def __post_init__(self) -> None:
        if len(self.items) != len(self.movement.line_items):
            raise ValueError("one match per line item")


def escape(text: str) -> str:
    """Escape text for Slack mrkdwn: no mentions, links or line breaks can come from it."""
    text = " ".join(text.split())
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.to_integral_value() else format(value, "f")


def _named(text: str | None, match: Match | None, names: Mapping[int, str], what: str) -> str:
    written = f"“{escape(text)}”" if text else "(not stated)"
    if match is None:
        return written
    if match.status is MatchStatus.MATCHED and match.record is not None:
        return f"*{escape(names.get(match.record, f'#{match.record}'))}* ({written})"
    if match.status is MatchStatus.AMBIGUOUS:
        shown = [escape(names.get(pk, f"#{pk}")) for pk in match.candidates[:MAX_CANDIDATES]]
        more = len(match.candidates) - len(shown)
        tail = f", and {more} more" if more > 0 else ""
        return f"{written}: {len(match.candidates)} {what}s match ({', '.join(shown)}{tail})"
    return f"{written}: no {what} found"


def _line(movement: CandidateMovement, index: int, match: Match, parts: Mapping[int, str]) -> str:
    item = movement.line_items[index]
    quantity = "?" if item.quantity is None else _number(item.quantity)
    unit = f" {escape(item.unit)}" if item.unit else ""
    pack = f" of {_number(item.pack_size)}" if item.pack_size is not None else ""
    return f"• {quantity}{unit}{pack} \N{MULTIPLICATION SIGN} {_named(item.item_text, match, parts, 'part')}"


_NEEDS_SOURCE = frozenset(MovementType) - {MovementType.RECEIVE, MovementType.COUNT}
_NEEDS_DESTINATION = frozenset(MovementType) - {MovementType.CONSUME}


def gate_reasons(view: MovementView) -> set[tuple[ReasonCode, str]]:
    """The caller's reasons, plus every reason the view itself shows.

    A line or place without a match that can authorize a write, a missing quantity or item, a needed place
    that is missing: so the reply never says "could apply" next to "no part found".
    """
    found = {(reason.code, reason.detail) for reason in view.reasons}
    movement = view.movement
    if movement.movement_type is MovementType.UNCLEAR:
        found.add((ReasonCode.MOVEMENT_UNCLEAR, ""))
    for n, (item, part) in enumerate(zip(movement.line_items, view.items, strict=True), start=1):
        if item.item_text is None:
            found.add((ReasonCode.ITEMS_MISSING, f"line {n}"))
        if item.quantity is None:
            found.add((ReasonCode.QUANTITY_MISSING, f"line {n}"))
        if reason := part.review_reason(kind="part"):
            found.add((reason.code, f"line {n}"))
    for side, text, match, needed, implied in (
        ("source", movement.source_text, view.source, _NEEDS_SOURCE, ReasonCode.IMPLIED_SOURCE),
        (
            "destination",
            movement.destination_text,
            view.destination,
            _NEEDS_DESTINATION,
            ReasonCode.IMPLIED_DESTINATION,
        ),
    ):
        if match is not None:
            if reason := match.review_reason(kind="location"):
                found.add((reason.code, side))
        elif text is not None:
            found.add((ReasonCode.LOCATION_UNMATCHED, side))
        elif movement.movement_type in needed:
            found.add((implied, ""))
    return found


def _reason_line(code: ReasonCode, details: list[str]) -> str:
    where = f" ({', '.join(escape(detail) for detail in details)})" if details else ""
    return f"• {REASONS[code]}{where}"


def _cut(text: str) -> str:
    return text if len(text) <= MAX_SECTION else text[: MAX_SECTION - 1].rstrip() + "…"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cut(text)}}


def dry_run_reply(
    views: Sequence[MovementView], parts: Mapping[int, str], locations: Mapping[int, str]
) -> dict[str, Any]:
    """The body of the dry-run reply: `text` for notifications, and `blocks`. Nothing was written."""
    if not views:
        raise ValueError("a dry-run reply shows at least one movement")
    blocks: list[dict[str, Any]] = [_section("*Dry run.* Nothing was changed in inventory.")]
    for number, view in enumerate(views[:MAX_MOVEMENTS], start=1):
        movement = view.movement
        heading = TYPE_LABELS[movement.movement_type]
        if len(views) > 1:
            heading = f"{number}. {heading}"
        lines = [_line(movement, i, match, parts) for i, match in enumerate(view.items)]
        places = (
            f"From {_named(movement.source_text, view.source, locations, 'location')}"
            f" to {_named(movement.destination_text, view.destination, locations, 'location')}"
        )
        blocks.append(_section("\n".join([f"*{heading}*", *lines, places])))
        details: dict[ReasonCode, list[str]] = {}
        for code, detail in sorted(gate_reasons(view), key=lambda reason: (reason[0].value, reason[1])):
            details.setdefault(code, [])
            if detail:
                details[code].append(detail)
        if details:
            needs = "\n".join(_reason_line(code, found) for code, found in details.items())
            blocks.append(_section(f"*Needs a human before it could apply:*\n{needs}"))
        else:
            blocks.append(_section("Nothing needs a human. With writes on, this could apply."))
    if len(views) > MAX_MOVEMENTS:
        hidden = len(views) - MAX_MOVEMENTS
        blocks.append(_section(f"…and {hidden} more movements, not shown here."))
    count = f"{len(views)} movements" if len(views) > 1 else "1 movement"
    return {"text": f"Dry run: {count}, nothing changed.", "blocks": blocks}
