"""The dry-run reply in a post's thread: what would change, and every reason it needs a human.

Pure: it turns values into a `chat.postMessage` body (`text` and `blocks`). Text that came from
the post (item and location words) is escaped, so a post cannot make the connector write a
mention such as <!channel> or a disguised link. Catalog names are escaped too.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stockthread.core.model import (
    CandidateMovement,
    Match,
    MatchStatus,
    MovementType,
    Operation,
    ReasonCode,
    ReviewReason,
    operation_for,
)
from stockthread.core.questions import Ask, Choice, Question, choices
from stockthread.core.resolve import SourcePick

MAX_SECTION = 2900
"""Slack allows 3000 characters in a section block. Longer text is cut, with a marker."""
MAX_CANDIDATES = 5
"""How many candidates a line shows. The result keeps all of them; only the display is capped."""
MAX_LABEL = 75
"""Slack allows 75 characters in a button label."""
MAX_MOVEMENTS = 20
"""How many movements a reply shows. Slack refuses more than 50 blocks; each movement takes 2."""
MAX_BLOCKS = 50
"""Slack's limit of blocks in one message. A reply takes at most 42, and its question 3 more."""

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
    ReasonCode.PHOTO_NOT_READ: "The post has a photo, and the connector does not read photos.",
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
    ReasonCode.NO_TARGET_STOCK_ITEM: "No usable stock item of this part is at the place.",
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
    stock: Sequence[SourcePick | None] = ()
    """The stock items each line acts on, as read from InvenTree (None: that line's stock was not read). Empty:
    no line's stock was read."""

    def __post_init__(self) -> None:
        if len(self.items) != len(self.movement.line_items):
            raise ValueError("one match per line item")
        if self.stock and len(self.stock) != len(self.items):
            raise ValueError("one stock pick per line item, or none")


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


def _line(view: MovementView, index: int, parts: Mapping[int, str]) -> str:
    movement = view.movement
    item = movement.line_items[index]
    quantity = "?" if item.quantity is None else _number(item.quantity)
    unit = f" {escape(item.unit)}" if item.unit else ""
    pack = f" of {_number(item.pack_size)}" if item.pack_size is not None else ""
    named = _named(item.item_text, view.items[index], parts, "part")
    pick = view.stock[index] if view.stock else None
    return f"• {quantity}{unit}{pack} \N{MULTIPLICATION SIGN} {named}{_stock(movement, pick)}"


def _stock(movement: CandidateMovement, pick: SourcePick | None) -> str:
    """The stock items a line acts on, by InvenTree id: what a write would send, so a person can check it there."""
    if pick is None or not pick.takes:
        return ""
    operation = operation_for(movement.movement_type)
    if operation in (Operation.ADD, Operation.COUNT):
        verb = "into" if operation is Operation.ADD else "counts"
        return f" ({verb} stock item #{pick.takes[0].item})"
    if len(pick.takes) == 1:
        return f" (from stock item #{pick.takes[0].item})"
    split = ", ".join(f"#{take.item}: {_number(take.quantity)}" for take in pick.takes)
    return f" (from stock items {split})"


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
    views: Sequence[MovementView],
    parts: Mapping[int, str],
    locations: Mapping[int, str],
    *,
    revised: bool = False,
    answered: bool = False,
) -> dict[str, Any]:
    """The body of the dry-run reply: `text` for notifications, and `blocks`. Nothing was written. `revised`:
    the post was read again because the thread changed, and this reply takes the place of the one above.
    `answered`: what changed is only that a question was answered."""
    if not views:
        raise ValueError("a dry-run reply shows at least one movement")
    because = "a question was answered" if answered else "the thread changed"
    heading = f"*Revised dry run* ({because})." if revised else "*Dry run.*"
    blocks: list[dict[str, Any]] = [_section(f"{heading} Nothing was changed in inventory.")]
    for number, view in enumerate(views[:MAX_MOVEMENTS], start=1):
        movement = view.movement
        heading = TYPE_LABELS[movement.movement_type]
        if len(views) > 1:
            heading = f"{number}. {heading}"
        lines = [_line(view, i, parts) for i in range(len(view.items))]
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
    return {"text": f"{'Revised dry run' if revised else 'Dry run'}: {count}, nothing changed.", "blocks": blocks}


RETRACTION_REASONS = {
    "changed": "A message of this post was deleted, or edited in a way I cannot read.",
    "unreadable": "A message of this post can no longer be read.",
    "silent": "As it reads now, I can no longer read this thread as a movement.",
    "failed": "Reading this thread again failed.",
    "unread": "I could not read this thread again: the language model gave no answer.",
}


def retraction_notice(why: str) -> dict[str, Any]:
    """The notice that the dry-run reply above no longer stands, because the thread changed after it."""
    return {
        "text": "The dry run above no longer stands. Nothing was changed in inventory.",
        "blocks": [
            _section(
                f"*The dry run above no longer stands.* {RETRACTION_REASONS[why]} Nothing was changed in inventory."
            )
        ],
    }


def rejection_notice() -> dict[str, Any]:
    """The reply once a "no" in the thread rejected the reading: nothing of it will be applied."""
    return {
        "text": "The reading above was rejected. Nothing will be applied.",
        "blocks": [
            _section(
                "*The reading above was rejected.* Nothing was changed in inventory, and nothing of this reading"
                " will be applied. To record the movement, edit the post or reply in this thread with what happened."
            )
        ],
    }


ANSWER_ACTION = "answer"
"""The action id of every question button. Its value is the id of the stored button row."""


def question_message(
    question: Question,
    views: Sequence[MovementView],
    parts: Mapping[int, str],
    locations: Mapping[int, str],
    values: Sequence[str],
) -> dict[str, Any]:
    """The body of a question in a post's thread. `values` holds one stored button id per `choices(question)`,
    in order; a question without choices is answered by a thread reply."""
    buttons = choices(question)
    if len(values) != len(buttons) or not all(values):
        raise ValueError("one stored button id per choice")
    view = views[question.movement]
    movement = view.movement
    item = movement.line_items[question.line] if question.line is not None else None
    written = f"“{escape(item.item_text)}”" if item is not None and item.item_text else None
    side = question.side or "destination"
    place_text = movement.source_text if side == "source" else movement.destination_text
    place = f"“{escape(place_text)}”" if place_text else f"the {side}"
    names = locations if question.side else parts
    record = escape(names.get(question.candidates[0], f"#{question.candidates[0]}")) if question.candidates else ""
    ask = question.ask
    if ask is Ask.CONFIRM:
        about = f"About {written}: " if written else ""
        # Which doubt of its kind: a "yes" clears this one only. The line is already named by its item.
        detail = re.sub(r"^line \d+:?\s*", "", question.detail)
        doubt = f"{REASONS[question.reason].rstrip('.')} ({escape(detail)})." if detail else REASONS[question.reason]
        text = f"{about}{doubt} Is this reading of the post right?"
    elif ask is Ask.MOVEMENT_TYPE:
        text = "What kind of movement is this?"
    elif ask is Ask.NAME_PART:
        text = (
            f"Which part is {written}? Reply in this thread with its name or IPN."
            if written
            else "Which item was moved? Reply in this thread with its name or IPN."
        )
    elif ask is Ask.CHOOSE_PART:
        text = f"Which part is {written or 'meant'}?"
    elif ask is Ask.CONFIRM_PART:
        text = f"Is {written or 'the item'} *{record}*?"
    elif ask is Ask.QUANTITY:
        text = (
            f"How many {written}? Reply in this thread with a number of pieces."
            if written
            else "How many of each item? Reply in this thread with a number of pieces for each."
        )
    elif ask is Ask.NAME_LOCATION:
        if question.reason is ReasonCode.VEHICLE_NOT_MAPPED:
            text = "Which vehicle? Reply in this thread with its stock location."
        elif place_text:
            text = f"Which stock location is {place}? Reply in this thread with its name."
        elif side == "source":
            text = "Where was it taken from? Reply in this thread with the stock location."
        else:
            text = "Where did it go? Reply in this thread with the stock location."
    elif ask is Ask.CHOOSE_LOCATION:
        text = f"Which stock location is {place}?"
    elif ask is Ask.CONFIRM_LOCATION:
        text = f"Is {place} *{record}*?"
    else:
        text = (
            f"Which serial numbers of {written}? Reply in this thread with one per piece."
            if written
            else "Which serial numbers? Reply in this thread with one per piece."
        )
    if len(views) > 1:
        text = f"Movement {question.movement + 1}: {text}"
    plain = text.replace(f"*{record}*", record) if record else text
    blocks: list[dict[str, Any]] = [_section(text)]
    labels = _labels(buttons, names, cut_left=question.side is not None)
    if buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"{ANSWER_ACTION}-{n}",
                        "text": {"type": "plain_text", "text": label, "emoji": False},
                        "value": value,
                    }
                    for n, (label, value) in enumerate(zip(labels, values, strict=True))
                ],
            }
        )
    return {"text": plain, "blocks": blocks}


QUESTION_BLOCK = "question"
"""The block id of the divider above a question: everything from it on is the question (`without_question`)."""
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{2,}")


def with_question(reply: Mapping[str, Any], question: Mapping[str, Any]) -> dict[str, Any]:
    """The dry-run reply with its question below it, so the reply and its buttons are one message: the buttons are
    stored with the reply's record, and no question is posted without the reply it belongs to. The notification
    text stays the reply's own."""
    blocks = [*reply["blocks"], {"type": "divider", "block_id": QUESTION_BLOCK}, *question["blocks"]]
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("a reply with its question has more blocks than Slack allows")
    return {"text": reply["text"], "blocks": blocks}


def _labels(buttons: Sequence[Choice], names: Mapping[int, str], *, cut_left: bool) -> list[str]:
    """Button labels. Records that would look alike get their primary key, so no two buttons read the same.
    A location path is cut from the left, since its last part names the place."""
    labels = [_label(choice, names, cut_left=cut_left) for choice in buttons]
    if len(set(labels)) < len(labels):
        labels = [
            _label(choice, names, cut_left=cut_left, suffix=f" #{choice.record}")
            if choice.record is not None
            else label
            for choice, label in zip(buttons, labels, strict=True)
        ]
    return labels


def _label(choice: Choice, names: Mapping[int, str], *, cut_left: bool, suffix: str = "") -> str:
    if choice.kind == "record" and choice.record is not None:
        label = " ".join(names.get(choice.record, "").split()) or f"#{choice.record}"
    elif choice.kind == "type" and choice.movement_type is not None:
        label = TYPE_LABELS[choice.movement_type]
    else:
        label = {"yes": "Yes", "no": "No", "none": "None of these"}[choice.kind]
    room = MAX_LABEL - len(suffix)
    if len(label) > room:
        label = "…" + label[-(room - 1) :].lstrip() if cut_left else label[: room - 1].rstrip() + "…"
    return label + suffix


_OUT_OF_DATE = "This button is out of date. Please use the newest reply in this thread."
CLICK_FEEDBACK = {
    "answered": "Thanks, your answer is recorded.",
    "unknown_button": _OUT_OF_DATE,
    "other_message": _OUT_OF_DATE,
    "stale": "A newer reply in this thread replaced this question. Please answer that one.",
    "already_answered": "This question was already answered, or a newer reply replaced it.",
    "expired": "This question has expired: a post can be changed for {hours} after it was made.",
    "not_allowed": "Only the person who posted this, or an approver, can answer this question.",
    "post_changed": "The post changed after this question was asked, so your answer was not recorded. "
    "Please try again in a moment, or answer the newer reply if one appears.",
}
"""What the clicker alone is told about each outcome of a click on a question's button. Plain text that
quotes neither the post nor the catalog."""
APPROVERS_ONLY = "Only an approver can answer this question."
NO_APPROVERS = "Only an approver can answer this question, and no approvers are set up."


def click_feedback(outcome: str, *, approver_only: bool, approvers: bool, expiry_hours: int) -> str | None:
    """The ephemeral note for a click finished with `outcome`, or None when the clicker is told nothing (a
    payload the ingest checks drop, or a button that does nothing yet)."""
    if outcome == "not_allowed" and approver_only:
        return APPROVERS_ONLY if approvers else NO_APPROVERS
    text = CLICK_FEEDBACK.get(outcome)
    hours = "1 hour" if expiry_hours == 1 else f"{expiry_hours} hours"
    return None if text is None else text.format(hours=hours)


def without_question(blocks: Sequence[Any]) -> list[dict[str, Any]] | None:
    """The blocks of a reply as Slack returns them, without the question asked in it: everything before the
    divider marked QUESTION_BLOCK. None when there is no such divider, or a block is not an object."""
    if not all(isinstance(block, Mapping) for block in blocks):
        return None
    for n, block in enumerate(blocks):
        if block.get("type") == "divider" and block.get("block_id") == QUESTION_BLOCK:
            return [dict(kept) for kept in blocks[:n]]
    return None


def question_closed(answered_by: str | None, *, expired: bool = False) -> dict[str, Any]:
    """The line that stands where a question was, once it is answered (by whom), replaced by a newer reply, or
    expired with its plan."""
    if expired:
        text = "This question expired, so nothing from this post will be applied. To record it, post it again."
    elif answered_by is None:
        text = "This question was replaced by a newer reply in this thread."
    elif _SLACK_USER.fullmatch(answered_by):
        text = f"This question was answered by <@{answered_by}>."
    else:
        text = "This question was answered."
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
