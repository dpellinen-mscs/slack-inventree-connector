"""The replies in a post's thread: the dry run (what would change, and every reason it needs a human), its
questions, and the receipt of what was written.

Pure: it turns values into a `chat.postMessage` body (`text` and `blocks`). Text that came from
the post (item and location words) is escaped, so a post cannot make the connector write a
mention such as <!channel> or a disguised link. Catalog names are escaped too.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from stockthread.core.marker import is_marker_id
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
from stockthread.core.requests import UNDOABLE, RequestState
from stockthread.core.resolve import SourcePick, StockItem

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
MAX_LEARNED = 10
"""The learned aliases a receipt names; the rest are counted (`stockthread aliases list` shows them all)."""

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
    ReasonCode.PACK_UNIT_AMBIGUOUS: "A box, a pack or a length: how many in the part's stocking unit is not clear.",
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
    named: Mapping[int | str, str] = field(default_factory=dict)
    """The names typed in the thread, by line or side ("source", "destination"), shown beside the post's words."""
    units: Sequence[str | None] = ()
    """The units of each line's part (`Part.units`), as validate was given them: set for a line in its part's own
    length unit (`in_stocking_unit`), else None. Empty: no line's units were read. A request carries a length bare
    only for a line whose units are here, and checks them again before it writes (`precondition.check`)."""

    def __post_init__(self) -> None:
        if len(self.items) != len(self.movement.line_items):
            raise ValueError("one match per line item")
        if self.stock and len(self.stock) != len(self.items):
            raise ValueError("one stock pick per line item, or none")
        if self.units and len(self.units) != len(self.items):
            raise ValueError("one part's units per line item, or none")


def escape(text: str) -> str:
    """Escape text for Slack mrkdwn: no mentions, links or line breaks can come from it."""
    text = " ".join(text.split())
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.to_integral_value() else format(value, "f")


def _named(text: str | None, match: Match | None, names: Mapping[int, str], what: str, typed: str | None = None) -> str:
    written = f"“{escape(text)}”" if text else "(not stated)"
    if typed is not None:
        typed_as = f"named in the thread as “{escape(typed)}”"
        written = f"{written}, {typed_as}" if text else typed_as
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


def _stocking_unit(view: MovementView, line: int | None, parts: Mapping[int, str], units: Mapping[int, str]) -> str:
    """The unit a pack or length question asks the number in. Named only for a part matched surely enough to
    write: a loose match may be another part, counted in another unit."""
    match = view.items[line] if line is not None and line < len(view.items) else None
    record = match.record if match is not None and match.can_authorize_a_write else None
    if record is None or record not in units:
        return "the part's stocking unit"
    name = escape(parts.get(record, f"#{record}"))
    return f"{escape(units[record]) or 'pieces'} (the stocking unit of *{name}*)"


def _line(view: MovementView, index: int, parts: Mapping[int, str]) -> str:
    movement = view.movement
    item = movement.line_items[index]
    quantity = "?" if item.quantity is None else _number(item.quantity)
    unit = f" {escape(item.unit)}" if item.unit else ""
    pack = f" of {_number(item.pack_size)}" if item.pack_size is not None else ""
    named = _named(item.item_text, view.items[index], parts, "part", view.named.get(index))
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
    return _dry_run(views, parts, locations, revised=revised, answered=answered)[0]


def shows_everything(views: Sequence[MovementView], parts: Mapping[int, str], locations: Mapping[int, str]) -> bool:
    """Whether the dry-run reply of `views` shows every movement, line, stock item, place and reason in full. A reply
    cut to Slack's limits leaves out what a write would do, and an approver approves only what they were shown
    (ADR 0016), so such a reply gets no Apply button (`with_no_apply`)."""
    return _dry_run(views, parts, locations)[1]


def _dry_run(
    views: Sequence[MovementView],
    parts: Mapping[int, str],
    locations: Mapping[int, str],
    *,
    revised: bool = False,
    answered: bool = False,
) -> tuple[dict[str, Any], bool]:
    """The dry-run reply, and whether nothing of it was cut (`shows_everything`)."""
    if not views:
        raise ValueError("a dry-run reply shows at least one movement")
    because = "a question was answered" if answered else "the thread changed"
    heading = f"*Revised dry run* ({because})." if revised else "*Dry run.*"
    texts = [f"{heading} Nothing was changed in inventory."]
    for number, view in enumerate(views[:MAX_MOVEMENTS], start=1):
        movement = view.movement
        heading = TYPE_LABELS[movement.movement_type]
        if len(views) > 1:
            heading = f"{number}. {heading}"
        lines = [_line(view, i, parts) for i in range(len(view.items))]
        source = _named(movement.source_text, view.source, locations, "location", view.named.get("source"))
        destination = _named(
            movement.destination_text, view.destination, locations, "location", view.named.get("destination")
        )
        places = f"From {source} to {destination}"
        texts.append("\n".join([f"*{heading}*", *lines, places]))
        details: dict[ReasonCode, list[str]] = {}
        for code, detail in sorted(gate_reasons(view), key=lambda reason: (reason[0].value, reason[1])):
            details.setdefault(code, [])
            if detail:
                details[code].append(detail)
        if details:
            needs = "\n".join(_reason_line(code, found) for code, found in details.items())
            texts.append(f"*Needs a human before it could apply:*\n{needs}")
        else:
            texts.append("Nothing needs a human. With writes on, this could apply.")
    whole = len(views) <= MAX_MOVEMENTS and all(len(text) <= MAX_SECTION for text in texts)
    blocks = [_section(text) for text in texts]
    if len(views) > MAX_MOVEMENTS:
        hidden = len(views) - MAX_MOVEMENTS
        blocks.append(_section(f"…and {hidden} more movements, not shown here."))
    count = f"{len(views)} movements" if len(views) > 1 else "1 movement"
    text = f"{'Revised dry run' if revised else 'Dry run'}: {count}, nothing changed."
    return {"text": text, "blocks": blocks}, whole


RETRACTION_REASONS = {
    "changed": "A message of this post was deleted, or edited in a way I cannot read.",
    "unreadable": "A message of this post can no longer be read.",
    "silent": "As it reads now, I can no longer read this thread as a movement.",
    "failed": "Reading this thread again failed.",
    "unread": "I could not read this thread again: the language model gave no answer.",
    "too_late": "The answer came as its plan expired: nothing of it will be applied.",
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
    *,
    units: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """The body of a question in a post's thread. `values` holds one stored button id per `choices(question)`,
    in order; a question without choices is answered by a thread reply. `units`: each part's stocking unit
    (`PartRecord.units`), named when a length or a box is asked about a line whose part is surely known."""
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
    elif ask is Ask.QUANTITY and question.reason is ReasonCode.PACK_UNIT_AMBIGUOUS:
        stocked = _stocking_unit(view, question.line, parts, units or {})
        text = (
            f"How many {written}, in {stocked}? Reply in this thread with a number."
            if written
            else "How many of each item, in each part's stocking unit? Reply in this thread with a number for each."
        )
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
    elif ask in (Ask.CHOOSE_STOCK, Ask.CONFIRM_STOCK, Ask.READ_STOCK_AGAIN):
        text = _stock_question(question, view, written or "the item")
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
    if ask is Ask.CHOOSE_STOCK:
        target = operation_for(view.movement.movement_type) in (Operation.ADD, Operation.COUNT)
        labels = [_stock_label(choice, _offered(question, view), target) for choice in buttons]
    elif ask is Ask.READ_STOCK_AGAIN:
        labels = ["Read the stock again", "Drop it"]
    else:
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


def _stock_question(question: Question, view: MovementView, written: str) -> str:
    """The stock question's words (design section 4, "The stock question"). Stock items are named by their
    InvenTree ids; what is wrong is said in the rule's words, never the post's."""
    detail = escape(re.sub(r"^line \d+:?\s*", "", question.detail))
    why = f" ({detail})" if detail else ""
    if question.ask is Ask.CHOOSE_STOCK:
        if operation_for(view.movement.movement_type) in (Operation.ADD, Operation.COUNT):
            return f"Which stock item of {written} is it{why}?"
        return f"Which stock item is {written} taken from{why}?"
    if question.ask is Ask.CONFIRM_STOCK:
        pick = view.stock[question.line] if question.line is not None and question.line < len(view.stock) else None
        split = pick.split if pick is not None else ()
        if [take.item for take in split] != list(question.candidates):
            raise ValueError("the split asked about is not the line's")
        takes = " and ".join(f"{_number(take.quantity)} from #{take.item}" for take in split)
        return f"No single stock item covers {written}{why}. Take {takes}?"
    doubt = REASONS[question.reason].rstrip(".")
    return (
        f"About {written}: {doubt}{why}. Fix it in InvenTree or edit the post, then read the stock again, or drop it."
    )


def _offered(question: Question, view: MovementView) -> Mapping[int, StockItem]:
    pick = view.stock[question.line] if question.line is not None and question.line < len(view.stock) else None
    offered = {item.pk: item for item in pick.offer} if pick is not None else {}
    if any(pk not in offered for pk in question.candidates):
        raise ValueError("a stock item asked about is not offered")
    return offered


def _stock_label(choice: Choice, offered: Mapping[int, StockItem], target: bool) -> str:
    """`#57 · 5 left · batch B-1`: words from InvenTree, never from the post. To take from, the quantity is what the
    item holds once the lines of the post before this one have taken their share, so it can be less than InvenTree
    shows. A target (an add or a count) is offered as read, whatever the lines before it take: `5 on hand`."""
    if choice.kind != "record" or choice.record is None:
        return "None of these"
    item = offered[choice.record]
    parts = [f"#{item.pk}", f"{_number(item.quantity)} {'on hand' if target else 'left'}"]
    if item.batch:
        parts.append(f"batch {' '.join(item.batch.split())}")
    if item.supplier_part is not None:
        parts.append(f"supplier part #{item.supplier_part}")
    label = " · ".join(parts)
    return label if len(label) <= MAX_LABEL else label[: MAX_LABEL - 1].rstrip() + "…"


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


APPLY_ACTION = "apply"
"""The action id of the Apply button on a dry-run reply that made a plan. Its value is the dry-run revision of the
reply: the click counts only on that reply, while it is the newest in the thread (`Worker._handle_apply`)."""
APPLY_BLOCK = "apply"
APPLY_CONFIRM = {
    "title": {"type": "plain_text", "text": "Apply this movement?"},
    "text": {
        "type": "plain_text",
        "text": "The stock moves in InvenTree as this reply shows, if it is still there when checked again.",
    },
    "confirm": {"type": "plain_text", "text": "Apply"},
    "deny": {"type": "plain_text", "text": "Not now"},
}
"""Slack asks the clicker to confirm before an Apply click is sent: a stray tap writes nothing."""


def with_apply(reply: Mapping[str, Any], revision: int) -> dict[str, Any]:
    """The dry-run reply of revision `revision` with an Apply button below it, for an approver to approve the plan it
    made (ADR 0019: a plan the policy does not approve waits for Apply). Only an approver's click counts."""
    if revision < 1:
        raise ValueError("a dry-run revision is at least 1")
    button = {
        "type": "button",
        "action_id": APPLY_ACTION,
        "text": {"type": "plain_text", "text": "Apply", "emoji": False},
        "style": "primary",
        "value": str(revision),
        "confirm": APPLY_CONFIRM,
    }
    blocks = [
        *reply["blocks"],
        _section("An approver can apply it now."),
        {"type": "actions", "block_id": APPLY_BLOCK, "elements": [button]},
    ]
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("a reply with its Apply button has more blocks than Slack allows")
    return {"text": reply["text"], "blocks": blocks}


NO_APPLY_TEXT = (
    "This reply is too long to show everything a write would do, so it cannot be applied from here."
    " To record the movement, split the post into shorter ones."
)


def with_no_apply(reply: Mapping[str, Any]) -> dict[str, Any]:
    """The dry-run reply of a plan that `shows_everything` says the reply does not show in full, with the reason it
    has no Apply button below it. The plan waits, as one with no reply does, until it expires."""
    blocks = [*reply["blocks"], _section(NO_APPLY_TEXT)]
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("a reply with its note has more blocks than Slack allows")
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


RECEIPT_WORDS = {
    RequestState.VERIFIED: "applied, and read back as planned",
    RequestState.VERIFY_FAILED: "the check of this write found a problem. Look at it in InvenTree before relying on it",
    RequestState.FAILED: "not applied. InvenTree refused it, or showed no change from it when checked",
    RequestState.ACCEPTED: "written. An approver accepted the problem its check found",
    RequestState.UNDO_FAILED: "an undo of it failed. Look at it in InvenTree",
    RequestState.UNDONE: "undone",
    RequestState.PLANNED: "not applied. Stock changed since the plan, so it waits for review",
    RequestState.REJECTED: "not applied. It was rejected",
    RequestState.CANCELLED: "not applied. A newer reading of the post replaced it",
    RequestState.EXPIRED: "not applied. It expired",
}
"""What a request's state says in the receipt: every state but those in which a step is due or in flight (`WORKING`).
`verify_failed` and `failed` claim no more than their every path guarantees: a `verify_failed` request may have been
found by its marker check with nothing sent, and a `failed` one concluded by recovery may still land (ADR 0015)."""

NOT_WRITTEN = frozenset(
    {RequestState.FAILED, RequestState.PLANNED, RequestState.REJECTED, RequestState.CANCELLED, RequestState.EXPIRED}
)
"""States of a request no write of which InvenTree showed."""

_LINK_SAFE = ":/%[]-._~!$'()*+,;="
"""What a link keeps of the configured InvenTree URL as written. Anything else, such as `|`, `<`, `>` or `&`, is
percent-encoded, so the URL cannot end the link or add markup."""


def _link(inventree_url: str, kind: str, pk: int, label: str) -> str:
    """A link to a record's page in InvenTree's web interface (`/web/stock/item/<pk>`, `/web/stock/location/<pk>`)."""
    base = urllib.parse.quote(inventree_url.rstrip("/"), safe=_LINK_SAFE)
    return f"<{base}/web/stock/{kind}/{pk}|{escape(label)}>"


def _receipt_links(view: MovementView, locations: Mapping[int, str], inventree_url: str) -> str:
    """The links of a movement's places and stock items; none for a URL that is not ASCII (a host name would be
    percent-encoded into a wrong link)."""
    if not inventree_url.isascii():
        return ""
    places = [match.record for match in (view.source, view.destination) if match is not None]
    items = sorted({take.item for pick in view.stock if pick is not None for take in pick.takes})
    links = [
        _link(inventree_url, "location", pk, locations.get(pk, f"location #{pk}"))
        for pk in dict.fromkeys(pk for pk in places if pk is not None)
    ]
    links += [_link(inventree_url, "item", pk, f"stock item #{pk}") for pk in items]
    return f"In InvenTree: {' · '.join(links)}" if links else ""


UNDO_ACTION = "undo"
"""The action id of the Undo button on a receipt. Its value is the marker id of the request it undoes."""
UNDO_CONFIRM = {
    "title": {"type": "plain_text", "text": "Undo this movement?"},
    "text": {
        "type": "plain_text",
        "text": "The stock goes back to where it was before this movement, if nothing else changed it since.",
    },
    "confirm": {"type": "plain_text", "text": "Undo"},
    "deny": {"type": "plain_text", "text": "Keep it"},
}
"""Slack asks the clicker to confirm before an Undo click is sent: a stray tap writes nothing."""


def _undo_button(marker_id: str) -> dict[str, Any]:
    return {
        "type": "button",
        "action_id": UNDO_ACTION,
        "text": {"type": "plain_text", "text": "Undo", "emoji": False},
        "value": marker_id,
        "confirm": UNDO_CONFIRM,
    }


def receipt(
    views: Sequence[MovementView],
    states: Sequence[RequestState],
    markers: Sequence[str],
    parts: Mapping[int, str],
    locations: Mapping[int, str],
    *,
    inventree_url: str,
    approved_by: str | None,
    undo: Sequence[str | None] | None = None,
    learned: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """The receipt in the post's thread once no step of its plan's writes is due or in flight (design section 7, step
    6), and again once an undo of one of them concluded: for every movement, its state, what it was to change, links
    to its places and stock items in InvenTree, and the marker its history entries carry in their notes. One state
    and one marker per movement, in the post's order.

    `approved_by`: the approver whose Apply click approved the plan, or None when the write policy approved it.
    `undo`: per movement, the marker id of its request when an undo of it may be asked for now (`applier.undo_refusal`
    with no user), or None; such a movement's section carries an Undo button with that value. A movement beyond
    MAX_MOVEMENTS is not shown, so it gets no button. `learned`: the aliases the plan taught (ADR 0012), as (text,
    name of its part or location); named only on a receipt that reports every movement verified."""
    if not views:
        raise ValueError("a receipt shows at least one movement")
    if len(states) != len(views) or len(markers) != len(views):
        raise ValueError("one state and one marker per movement")
    offered = list(undo) if undo is not None else [None] * len(views)
    if len(offered) != len(views):
        raise ValueError("one undo value or None per movement")
    if any(value is not None and not is_marker_id(value) for value in offered):
        raise ValueError("an Undo button's value is a marker id")
    if any(value is not None and state not in UNDOABLE for value, state in zip(offered, states, strict=True)):
        raise ValueError("an Undo button is offered only on a movement an undo may start from")
    if any(state not in RECEIPT_WORDS for state in states):
        raise ValueError("a receipt reports no request whose write is due or in flight")
    if all(state is RequestState.VERIFIED for state in states):
        heading, text = "*Applied.* Inventory was changed as below, and read back to check it.", "Applied"
    elif all(state in NOT_WRITTEN for state in states):
        heading, text = "*Not applied.*", "Not applied"
    elif all(state in NOT_WRITTEN - {RequestState.PLANNED} | {RequestState.UNDONE} for state in states):
        heading, text = "*Undone.* What was applied below was put back, and read back to check it.", "Undone"
    elif all(state in {RequestState.VERIFIED, RequestState.UNDONE} for state in states):
        heading, text = (
            "*Partly undone.* What is marked undone below was put back; the rest stays applied.",
            "Partly undone",
        )
    else:
        heading, text = "*Check this.* Not everything below went as planned.", "Check this"
    if approved_by is None:
        who = "Approved by the write policy, with no click."
    elif _SLACK_USER.fullmatch(approved_by):
        who = f"Approved by <@{approved_by}>."
    else:
        who = "Approved by a click."
    blocks: list[dict[str, Any]] = [_section(f"{heading} {who}")]
    shown_views = zip(views[:MAX_MOVEMENTS], states, markers, offered, strict=False)
    for number, (view, state, marker, undo_value) in enumerate(shown_views, 1):
        movement = view.movement
        title = TYPE_LABELS[movement.movement_type]
        if len(views) > 1:
            title = f"{number}. {title}"
        lines = [_line(view, i, parts) for i in range(len(view.items))]
        source = _named(movement.source_text, view.source, locations, "location", view.named.get("source"))
        destination = _named(
            movement.destination_text, view.destination, locations, "location", view.named.get("destination")
        )
        shown = "\n".join([f"*{title}*: {RECEIPT_WORDS[state]}.", *lines, f"From {source} to {destination}"])
        links = _receipt_links(view, locations, inventree_url)
        if links and len(shown) + 1 + len(links) <= MAX_SECTION:  # never cut inside a link
            shown = f"{shown}\n{links}"
        section = _section(shown)
        if undo_value is not None:
            section["accessory"] = _undo_button(undo_value)
        blocks.append(section)
        trail = f"Marker `{escape(marker)}`: InvenTree's stock history entries of this request carry it in their notes."
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": trail}]})
    if len(views) > MAX_MOVEMENTS:
        blocks.append(_section(f"…and {len(views) - MAX_MOVEMENTS} more movements, not shown here."))
    if learned and all(state is RequestState.VERIFIED for state in states):
        taught = "; ".join(f"“{escape(text)}” means {escape(name)}" for text, name in learned[:MAX_LEARNED])
        more = f"; and {len(learned) - MAX_LEARNED} more" if len(learned) > MAX_LEARNED else ""
        note = f"Learned from this Apply: {taught}{more}."
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _cut(note)}]})
    count = f"{len(views)} movements" if len(views) > 1 else "1 movement"
    return {"text": f"{text}: {count}.", "blocks": blocks}


def receipt_replaced() -> dict[str, Any]:
    """What a receipt is edited to when the movements changed while it was on its way to Slack: the newer receipt
    below reports them. Its Undo button goes with it (a click on it is refused anyway: it is not the plan's newest
    receipt)."""
    text = "This receipt was replaced by a newer one below."
    return {"text": text, "blocks": [_section(f"_{text}_")]}
